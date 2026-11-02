import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest

from orchestragent.native import ContextPrompt, NativeClients
from orchestragent.serialization import dumps

SCHEMA: dict[str, object] = {"type": "object", "properties": {"answer": {"type": "string"}}}
CODEX_EVENTS = [
    {"type": "thread.started", "thread_id": "session-123"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": '{"answer":"yes"}'}},
    {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 4}},
]
CLAUDE_EVENTS = [
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "claude-123",
        "structured_output": {"answer": "yes"},
        "usage": {"input_tokens": 12, "output_tokens": 4},
        "total_cost_usd": 0.012,
    }
]


@pytest.mark.parametrize("models,expected", [
    ({"claude-sonnet-5-5": {}}, "claude-sonnet-5-5"),
    ({"claude-sonnet-5-5": {}, "claude-opus-5-5": {}}, None),
    ({"not-a-model-secret": {}}, None),
    ({"claude-sonnet-5-5\nsecret": {}}, None),
    ({}, None),
    (None, None),
])
def test_claude_served_model_requires_one_well_formed_model(
    models: object, expected: str | None,
) -> None:
    event = {**CLAUDE_EVENTS[0], "modelUsage": models}
    result = NativeClients._decode("claude", 0, dumps(event).encode(), b"")
    assert result.usage.get("served_model") == expected


FAKE_PROGRAM = r"""
import json, os, pathlib, subprocess, sys, time
root = pathlib.Path(__file__).parent
config = json.loads((root / "config.json").read_text())
provider = pathlib.Path(__file__).name
if "status" in sys.argv:
    if provider == "codex":
        print(config.get("codex_auth", "Logged in using ChatGPT"))
    else:
        print(json.dumps(config.get("claude_auth", {
            "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
            "subscriptionType": "max", "email": "FAKE_PRIVATE_EMAIL",
        })))
    sys.exit(config.get("auth_code", 0))
(root / "invocation.json").write_text(json.dumps({
    "args": sys.argv[1:], "env": dict(os.environ), "prompt": sys.stdin.read(),
    "shared_context": pathlib.Path(sys.argv[sys.argv.index("--append-system-prompt-file") + 1])
    .read_text() if "--append-system-prompt-file" in sys.argv else "",
}))
if config.get("stall"):
    marker = str(root / "child-stopped")
    ready = str(root / "child-started")
    child = subprocess.Popen([sys.executable, "-c",
        "import signal,time,pathlib,os; "
        "signal.signal(signal.SIGTERM, lambda *a: (pathlib.Path(" + repr(marker) +
        ").write_text('stopped'),exit(0))); pathlib.Path(" + repr(ready) +
        ").write_text(str(os.getpid())); time.sleep(30)"])
    time.sleep(30)
if config.get("overflow"):
    print("x" * 200000)
    time.sleep(30)
time.sleep(config.get("delay", 0))
for event in config.get(provider + "_events", []):
    print(json.dumps(event), flush=True)
print(config.get("stderr", ""), file=sys.stderr)
sys.exit(config.get("code", 0))
"""


@pytest.fixture
def native(tmp_path: Path) -> NativeClients:
    clients = NativeClients(tmp_path / "native", timeout=2)
    for provider in clients.executables:
        path = tmp_path / provider
        path.write_text(f"#!{sys.executable}\n" + FAKE_PROGRAM)
        path.chmod(0o700)
        clients.executables[provider] = path
    return clients


@pytest.fixture
def configure(native: NativeClients) -> Callable[..., None]:
    def write(**overrides: object) -> None:
        config = {"codex_events": CODEX_EVENTS, "claude_events": CLAUDE_EVENTS, **overrides}
        (native.root.parent / "config.json").write_text(dumps(config))

    write()
    return write


@pytest.mark.parametrize("provider,effort", [("codex", "high"), ("codex", "max"),
                                           ("claude", "high")])
def test_requested_effort_is_the_only_cli_setting(
    native: NativeClients, configure: Callable[..., None], provider: str, effort: str,
) -> None:
    result = native.call(provider, "model-test", "prompt", SCHEMA, "effort-test",
                         lambda: False, effort=effort)
    args = json.loads((native.root.parent / "invocation.json").read_text())["args"]
    applied = ([arg for arg in args if arg.startswith("model_reasoning_effort=")]
               if provider == "codex" else [args[args.index("--effort") + 1]])
    expected = [f'model_reasoning_effort="{effort}"'] if provider == "codex" else [effort]
    assert (result.outcome, applied, result.usage.get("effort")) == ("ok", expected, effort)


@pytest.mark.parametrize("provider,effort,budget", [
    ("codex", "low", 2400), ("codex", "medium", 3360),
    ("codex", "high", 4800), ("codex", "xhigh", 7200),
    ("codex", "max", 9600), ("claude", "max", 9600),
])
def test_effort_timeout_reaches_the_process_guard(
    native: NativeClients, configure: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch, provider: str, effort: str, budget: float,
) -> None:
    native.timeout = 2400
    runner = Mock(wraps=native._run)
    monkeypatch.setattr(native, "_run", runner)
    result = native.call(provider, "model-test", "prompt", SCHEMA, "budget",
                         lambda: False, effort=effort)
    assert (runner.call_args.args[3], result.outcome) == (budget, "ok")


def test_four_megabyte_context_reaches_native_transport(
    native: NativeClients, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native, "_status", Mock(return_value={"ready": True}))
    runner = Mock(return_value=("timed_out", -1, b"", b""))
    monkeypatch.setattr(native, "_run", runner)
    prompt = "x" * 4_194_304
    result = native.call("codex", "model-test", prompt, SCHEMA, "large-context", lambda: False)
    assert (runner.call_count, result.outcome) == (1, "timed_out")


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_context_boundary_preserves_text_and_uses_native_system_cache_block(
    native: NativeClients, configure: Callable[..., None], provider: str,
) -> None:
    prompt = ContextPrompt("Stable source and requirements.\n", "Fix this failure.")
    result = native.call(provider, "model-test", prompt, SCHEMA, "context", lambda: False)
    invocation = json.loads((native.root.parent / "invocation.json").read_text())
    assert (result.outcome, invocation["prompt"]) == (
        "ok", prompt.dynamic if provider == "claude" else str(prompt),
    )
    assert (prompt.shared in invocation["shared_context"]) == (provider == "claude")
    assert (str(prompt), list(native.root.iterdir())) == (prompt.shared + prompt.dynamic, [])


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_subscription_call_has_no_tools_and_clean_environment(
    native: NativeClients,
    configure: Callable[..., None],
    provider: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "OPENAI_API_KEY",
        "HTTPS_PROXY",
        "SSH_AUTH_SOCK",
        "CODEX_HOME",
    ):
        monkeypatch.setenv(name, "FAKE_SECRET_NEVER_INHERIT")
    # Claude Code's own API-key setting reaches the claude process only (its documented
    # way to run without a subscription); every other vendor's secret stays out.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE_CLAUDE_KEY_FOR_CLAUDE_ONLY")
    result = native.call(provider, "model-test", "Literal prompt", SCHEMA, "run-1", lambda: False)
    invocation = json.loads((native.root.parent / "invocation.json").read_text())
    args = invocation["args"]
    assert invocation["env"].get("ANTHROPIC_API_KEY") == (
        "FAKE_CLAUDE_KEY_FOR_CLAUDE_ONLY" if provider == "claude" else None)
    assert (result.outcome, result.data, result.usage["input_tokens"]) == (
        "ok",
        {"answer": "yes"},
        12,
    )
    assert "FAKE_SECRET_NEVER_INHERIT" not in dumps(invocation)
    if provider == "codex":
        assert all(
            flag in args
            for flag in (
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "shell_tool",
                "plugins",
                "hooks",
                "multi_agent",
                "view_image",
                "unbounded_connection_retries",
                "shell_snapshot",
                "code_mode_host",
                "browser_use_external",
                "browser_use_full_cdp_access",
                "workspace_dependencies",
                "skill_mcp_dependency_install",
                "remote_plugin",
                "memories",
                'forced_login_method="chatgpt"',
            )
        )
    else:
        assert args[args.index("--tools") + 1] == ""
        assert all(flag in args for flag in ("--safe-mode", "--restricted", "--strict-mcp-config"))
        assert ("--bare" in args, result.usage["provider_estimate_usd"]) == (False, 0.012)
    assert list(native.root.iterdir()) == []


@pytest.mark.parametrize(
    ("provider", "auth"),
    [
        ("codex", "Logged in using an API key: FAKE_SECRET"),
        ("codex", "Not logged in"),
        ("claude", {"loggedIn": True, "authMethod": "unknown"}),
        ("claude", {"loggedIn": False}),
    ],
)
def test_auth_rejection_happens_before_inference(
    native: NativeClients,
    configure: Callable[..., None],
    provider: str,
    auth: object,
) -> None:
    configure(**{provider + "_auth": auth})
    result = native.call(provider, "model", "prompt", SCHEMA, "run", lambda: False)
    assert (result.outcome, (native.root.parent / "invocation.json").exists()) == (
        "auth_required",
        False,
    )
    assert "FAKE_SECRET" not in dumps(native.status())


def test_status_is_sanitized(native: NativeClients, configure: Callable[..., None]) -> None:
    assert native.status() == {
        "codex": {"ready": True, "auth": "chatgpt"},
        "claude": {"ready": True, "auth": "claude_subscription"},
    }


@pytest.mark.parametrize(
    ("events", "code", "expected"),
    [
        (CODEX_EVENTS[:-1], 0, "failed"),
        (CODEX_EVENTS, 1, "failed"),
        (
            [{"type": "turn.failed", "error": {"message": "insufficient_quota FAKE_SECRET"}}],
            1,
            "quota_exhausted",
        ),
        ([{"type": "error", "message": "429 rate limit FAKE_SECRET"}], 1, "rate_limited"),
        ([{"type": "error", "message": "401 unauthorized FAKE_SECRET"}], 1, "auth_required"),
        ([{"type": "error", "message": "permission approval FAKE_SECRET"}], 1, "waiting_approval"),
        (CODEX_EVENTS + [CODEX_EVENTS[-1]], 0, "failed"),
        (
            [{"type": "item.completed", "item": {"type": "agent_message", "text": "Thinking"}}]
            + CODEX_EVENTS,
            0,
            "ok",
        ),
    ],
)
def test_terminal_validation_and_safe_error_classes(
    native: NativeClients,
    configure: Callable[..., None],
    events: object,
    code: int,
    expected: str,
) -> None:
    configure(codex_events=events, code=code)
    result = native.call("codex", "model", "prompt", SCHEMA, "run", lambda: False)
    assert result.outcome == expected
    assert "FAKE_SECRET" not in result.message


@pytest.mark.parametrize(
    "provider,events",
    [
        ("codex", [{"type": "item.completed", "item": {"type": "agent_message", "text": "bad"}}]),
        ("claude", [{"type": "result", "subtype": "success", "is_error": False}]),
        ("claude", [{"type": "result", "subtype": "error", "is_error": True}]),
    ],
)
def test_invalid_structured_results_fail_closed(
    native: NativeClients,
    configure: Callable[..., None],
    provider: str,
    events: object,
) -> None:
    configure(**{provider + "_events": events})
    assert (
        native.call(provider, "model", "prompt", SCHEMA, "run", lambda: False).outcome == "failed"
    )


@pytest.mark.parametrize("cancel,timeout", [(False, 2), (True, 2), (True, 0)])
def test_timeout_and_cancel_terminate_descendants(
    native: NativeClients,
    configure: Callable[..., None],
    cancel: bool, timeout: float,
) -> None:
    configure(stall=True)
    native.timeout = timeout
    started = time.monotonic()
    result = native.call(
        "codex",
        "model",
        "prompt",
        SCHEMA,
        "run",
        lambda: cancel and ((native.root.parent / "child-started").exists()
                            or time.monotonic() - started > 5),
    )
    assert (result.outcome, (native.root.parent / "child-stopped").exists()) == (
        "cancelled" if cancel else "timed_out",
        True,
    )


def test_output_cap_stops_process(native: NativeClients, configure: Callable[..., None]) -> None:
    configure(overflow=True)
    native.output_limit = 10000
    assert native.call("codex", "model", "prompt", SCHEMA, "run", lambda: False).outcome == "failed"


@pytest.mark.parametrize("provider,run_id", [("kimi", "run"), ("codex", "../escape")])
def test_invalid_request_never_launches(
    native: NativeClients,
    provider: str,
    run_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native, "_run", lambda *args: pytest.fail("must not launch"))
    assert (
        native.call(provider, "model", "prompt", SCHEMA, run_id, lambda: False).outcome == "failed"
    )


def test_cancelled_before_auth_never_launches(
    native: NativeClients,
    configure: Callable[..., None],
) -> None:
    result = native.call("codex", "model", "prompt", SCHEMA, "run", lambda: True)
    assert (result.outcome, (native.root.parent / "invocation.json").exists()) == (
        "cancelled",
        False,
    )


def test_close_cancels_active_calls_and_prevents_more_launches(
    native: NativeClients,
    configure: Callable[..., None],
) -> None:
    configure(stall=True)
    native.timeout = 0
    results = []
    workers = [
        threading.Thread(
            target=lambda: results.append(
                native.call("codex", "model", "prompt", SCHEMA, "run", lambda: False),
            )
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + 3
    while not (native.root.parent / "child-started").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    native.close()
    for worker in workers:
        worker.join(timeout=3)
    assert (
        [result.outcome for result in results],
        any(worker.is_alive() for worker in workers),
    ) == (
        ["cancelled", "cancelled"],
        False,
    )
    assert (
        native.call("codex", "model", "prompt", SCHEMA, "later", lambda: False).outcome
        == "cancelled"
    )


def test_runner_explicit_environment_does_not_merge_parent_secrets(
    native: NativeClients,
    configure: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNTRUSTED_SECRET", "DO_NOT_INHERIT")
    outcome, code, _, _ = native._run(
        [str(native.executables["codex"])],
        native.root,
        "prompt",
        2,
        lambda: False,
        environment={"PATH": os.defpath, "DSH_HOME": "approved-test-path"},
    )
    invocation = json.loads((native.root.parent / "invocation.json").read_text())
    assert (
        outcome,
        code,
        invocation["env"]["DSH_HOME"],
        "UNTRUSTED_SECRET" in invocation["env"],
    ) == ("ok", 0, "approved-test-path", False)


@pytest.mark.parametrize("provider,effort", [("codex", "low"), ("codex", "max"),
                                           ("claude", "low"), ("claude", "max")])
def test_unlimited_call_waits_for_result_and_bounds_only_auth_check(
    native: NativeClients, configure: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch, provider: str, effort: str,
) -> None:
    native.timeout = 0
    configure(delay=0.2)
    runner = Mock(wraps=native._run)
    monkeypatch.setattr(native, "_run", runner)
    result = native.call(provider, "model", "prompt", SCHEMA, "unlimited",
                         lambda: False, effort=effort)
    assert (result.outcome, result.data, [call.args[3] for call in runner.call_args_list]) == (
        "ok", {"answer": "yes"}, [15, 0],
    )


@pytest.mark.parametrize(("auth", "mode"), [
    ({"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty",
      "apiKeySource": "ANTHROPIC_API_KEY"}, "claude_api_key"),
    ({"loggedIn": True, "authMethod": "none", "apiProvider": "bedrock"}, "claude_bedrock"),
])
def test_claude_api_key_and_cloud_auth_are_accepted(
    native: NativeClients, configure: Callable[..., None], auth: object, mode: str,
) -> None:
    configure(claude_auth=auth)
    assert native.status()["claude"] == {"ready": True, "auth": mode}
    result = native.call("claude", "model", "prompt", SCHEMA, "run", lambda: False)
    assert result.outcome == "ok"

