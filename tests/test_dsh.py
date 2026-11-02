import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import cast

import pytest

from orchestragent import dsh as dsh_module
from orchestragent.dsh import DshClient, json_failure
from orchestragent.dsh_guard import Guard
from orchestragent.native import NativeClients
from orchestragent.providers import ProviderClients
from orchestragent.serialization import dumps

SESSION = {"type": "session", "sessionId": "session-test"}
END = {"type": "status", "phase": "turn_end", "reason": {"kind": "completed"}}
FINAL = {"type": "final", "text": '{"answer":42}'}
SUCCESS = [
    SESSION,
    {"type": "status", "phase": "turn_start"},
    {"type": "status", "phase": "step_start"},
    {
        "type": "status",
        "phase": "step_end",
        "usage": {"inputTokens": 13, "outputTokens": 7, "cacheReadTokens": 3},
    },
    END,
    FINAL,
]


@pytest.fixture
def default_profile() -> bytes:
    entries = {
        **DshClient.core,
        **{
            name: "@deepseek-ai/dsh-" + name
            for name in (
                "tool-bash",
                "tool-fs",
                "tool-new-future-tool",
                "skill-filesystem",
                "agent-instructions",
                "mcp-resources",
                "web",
                "subagent",
                "session-title-llm",
                "otel",
                "session-telemetry-otel",
                "jobs",
                "llm-retry",
                "plugin-manager",
                "config-editor",
                "credentials",
                "llm-deepseek-account",
            )
        },
    }
    return "\n".join(f"- id: {key}\n  name: '{value}'" for key, value in entries.items()).encode()


@pytest.fixture
def dsh_fake(tmp_path: Path, default_profile: bytes) -> tuple[DshClient, Path, Path]:
    response = tmp_path / "response.json"
    response.write_text(dumps({"events": SUCCESS, "code": 0}), encoding="utf-8")
    record = tmp_path / "record.jsonl"
    executable = tmp_path / "fake-dsh"
    executable.write_text(
        f"#!{Path(sys.executable).resolve()}\n"
        "import json, os, pathlib, sys\n"
        f"record = pathlib.Path({str(record)!r})\n"
        "args = sys.argv[1:]\n"
        "entry = {'args':args, 'environment':dict(os.environ), 'input':sys.stdin.read()}\n"
        "if '--patch' in args:\n"
        "    entry['patch'] = json.loads(pathlib.Path(args[args.index('--patch')+1]).read_text())\n"
        "with record.open('a') as stream: stream.write(json.dumps(entry)+'\\n')\n"
        "if '--dump-default-config' in args:\n"
        f"    sys.stdout.buffer.write({default_profile!r})\n"
        "else:\n"
        f"    response = json.loads(pathlib.Path({str(response)!r}).read_text())\n"
        "    for event in response['events']: print(json.dumps(event))\n"
        "    print(response.get('stderr',''), file=sys.stderr)\n"
        "    sys.exit(response['code'])\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    native = NativeClients(tmp_path / "native", timeout=10)
    client = DshClient(native, tmp_path / "dsh")
    client.executable = executable
    return client, response, record


@pytest.mark.parametrize(("model", "prompt"), [
    ("deepseek-flash", "Solve"), ("deepseek-v4-pro", "Solve"),
    ("deepseek-flash", "context\n" * 160000),
])
def test_isolated_call(
    dsh_fake: tuple[DshClient, Path, Path], monkeypatch: pytest.MonkeyPatch,
    model: str, prompt: str,
) -> None:
    client, _, record = dsh_fake
    for key in ("DEEPSEEK_BASE_URL", "HTTP_PROXY", "NODE_OPTIONS", "DSH_HOME", "SSH_AUTH_SOCK"):
        monkeypatch.setenv(key, "untrusted-parent")
    result = client.call(
        model, prompt, {"type": "object"}, "safe-run", lambda: False, api_key="fake-authorized-key"
    )
    entries = [json.loads(line) for line in record.read_text().splitlines()]
    discovery, inference = entries
    patches = inference["patch"]
    disabled = {patch["id"]: patch["disabled"] for patch in patches if "disabled" in patch}
    configs = {patch["id"]: patch["config"] for patch in patches if "config" in patch}
    assert result.outcome == "ok"
    assert result.data == {"answer": 42}
    assert result.session_id == "session-test"
    assert result.usage == {
        "input_tokens": 13, "output_tokens": 7, "cache_read_input_tokens": 3, "forwarded": 0,
        "turn_end": "completed",
    }
    assert discovery["args"] == ["headless", "--dump-default-config"]
    assert discovery["input"] == ""
    assert "DEEPSEEK_API_KEY" not in discovery["environment"]
    assert inference["args"][:2] == ["headless", "--patch"]
    assert inference["args"][-2:] == ["--json", "-"]
    assert dumps({"type": "object"}) in inference["input"]
    assert inference["input"].startswith(prompt)
    assert inference["environment"]["DEEPSEEK_API_KEY"] == "fake-authorized-key"
    assert "untrusted-parent" not in dumps(inference)
    assert not Path(inference["environment"]["DSH_HOME"]).exists()
    assert not any(client.root.iterdir())
    assert all(disabled[key] is (key not in DshClient.core) for key in disabled)
    assert configs["agent-default-model"] == {"provider": "deepseek-official", "model": model}
    assert configs["llm-deepseek"]["maxTokens"] == 8192
    assert configs["llm-deepseek"]["thinking"] == "disabled"
    # An explicit retryPolicy must be omitted: an empty retryableCodes list is rejected
    # at plugin load, and llm-retry is disabled by the whitelist.
    assert "retryPolicy" not in configs["llm-deepseek"]
    assert configs["llm-deepseek"]["reasoningEffort"] == "off"
    assert configs["tools"] == {"mode": "native"}


@pytest.mark.parametrize(
    ("model", "prompt", "schema", "run_id", "key", "expected"),
    [
        ("other", "hello", {}, "run", "key", "failed"),
        ("deepseek-flash", "", {}, "run", "key", "failed"),
        ("deepseek-flash", "文" * 14000, {}, "run", "key", "failed"),
        ("deepseek-flash", "hello", {"description": "x" * 40960}, "run", "key", "failed"),
        ("deepseek-flash", "hello", {}, "../run", "key", "failed"),
        ("deepseek-flash", "hello", {}, "run", "", "auth_required"),
        ("deepseek-flash", "hello", {}, "run", "key\nsecret", "auth_required"),
        ("deepseek-flash", "hello", {}, "run", "key\0secret", "failed"),
    ],
)
def test_invalid_request_never_launches(
    dsh_fake: tuple[DshClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    prompt: str,
    schema: dict[str, object],
    run_id: str,
    key: str,
    expected: str,
) -> None:
    monkeypatch.setattr(DshClient, "input_limit", 40 * 1024)
    client, _, record = dsh_fake
    result = client.call(model, prompt, schema, run_id, lambda: False, api_key=key)
    assert result.outcome == expected
    assert not record.exists()


@pytest.mark.parametrize(
    "events",
    [
        [SESSION, FINAL],
        [SESSION, END],
        [END, FINAL],
        [SESSION, {"type": "tool_call", "tool": "bash"}, END, FINAL],
        [SESSION, {"type": "tool_result"}, END, FINAL],
        [SESSION, END, FINAL, FINAL],
        [SESSION, END, {"type": "final", "text": "[]"}],
        [SESSION, END, {"type": "final", "text": "not json"}],
        [SESSION, END, {"type": "final", "text": '{"x":NaN}'}],
        [SESSION, {"type": "status", "phase": "turn_end", "reason": {"kind": "max-tokens"}}, FINAL],
        [SESSION, END, {"type": "status", "phase": "turn_start"}, FINAL],
        [SESSION, SESSION, END, FINAL],
        [
            SESSION,
            {"type": "status", "phase": "step_end"},
            {"type": "status", "phase": "step_end"},
            END,
            FINAL,
        ],
    ],
)
def test_rejects_incomplete_or_unsafe_streams(
    dsh_fake: tuple[DshClient, Path, Path], events: list[dict[str, object]]
) -> None:
    client, _, _ = dsh_fake
    output = "\n".join(dumps(event) for event in events).encode()
    assert client._decode(0, output, b"").outcome == "failed"


BAD_ESCAPE = '{"status":"DONE","edits":[{"replace":"return "a""}]}'
CUT_AND_CLOSED = '{"status":"DONE","edits":[{"replace":"return 1"}}'


@pytest.mark.parametrize(("text", "diagnosis", "excerpt"), [
    (BAD_ESCAPE, "object_chars=52; error_char=46; trailing_chars=0", 'return "a"'),
    (CUT_AND_CLOSED, "object_chars=49; error_char=48; trailing_chars=0", "return 1"),
    ("no object", "final_chars=9; no JSON object", "no object"),
    ('{"ok":1}', "final_chars=8; object_chars=8", ""),
])
def test_json_failure_locates_the_break_without_repairing_it(
    text: str, diagnosis: str, excerpt: str,
) -> None:
    found = json_failure(text)
    assert diagnosis in found[0] and excerpt in found[1]


@pytest.mark.parametrize(("events", "message", "text"), [
    ([SESSION, END, {"type": "final", "text": BAD_ESCAPE}],
     "Invalid DSH JSON result: Expecting ',' delimiter", BAD_ESCAPE),
    ([SESSION, {"type": "status", "phase": "turn_end", "reason": {"kind": "max-tokens"}}],
     "stop=max-tokens", ""),
])
def test_unusable_answers_keep_parser_position_stop_reason_and_raw_text(
    dsh_fake: tuple[DshClient, Path, Path], events: list[dict[str, object]], message: str,
    text: str,
) -> None:
    client, _, _ = dsh_fake
    result = client._decode(0, "\n".join(dumps(event) for event in events).encode(), b"")
    assert result.outcome == "failed" and message in result.message and result.text == text
    assert ("error_char=46" in result.message) is bool(text)


@pytest.mark.parametrize(("code", "recorded"), [
    ("TIMEOUT", "TIMEOUT"), ("stream idle: secret detail", "OTHER")])
def test_failed_turn_is_classified_with_its_error_code_not_as_a_malformed_answer(
    dsh_fake: tuple[DshClient, Path, Path], code: str, recorded: str,
) -> None:
    client, _, _ = dsh_fake
    error = {"type": "status", "phase": "turn_end", "reason": {
        "kind": "error", "error": {"code": code, "message": "SECRET_ERROR_TEXT"}}}
    events = [SESSION, error, {"type": "final", "text": ""}]
    result = client._decode(0, "\n".join(dumps(event) for event in events).encode(), b"")
    assert result.outcome == "failed" and not result.message.startswith("Invalid DSH JSON")
    assert f"stop=error; error_code={recorded}" in result.message
    assert (result.usage["turn_end"], result.usage["turn_error"], result.text) == (
        "error", recorded, "")
    assert "SECRET_ERROR_TEXT" not in dumps(asdict(result))


def test_cut_answer_keeps_its_stop_reason_when_the_partial_json_fails(
    dsh_fake: tuple[DshClient, Path, Path],
) -> None:
    client, _, _ = dsh_fake
    events = [SESSION, {"type": "status", "phase": "turn_end", "reason": {"kind": "max-tokens"}},
              {"type": "final", "text": CUT_AND_CLOSED}]
    result = client._decode(0, "\n".join(dumps(event) for event in events).encode(), b"")
    assert result.message.startswith("Invalid DSH JSON result") and result.text == CUT_AND_CLOSED
    assert "stop=max-tokens" in result.message and result.usage["turn_end"] == "max-tokens"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("insufficient_quota", "quota_exhausted"),
        ("429", "rate_limited"),
        ("unauthorized 401", "auth_required"),
        ("approval required", "waiting_approval"),
        ("other failure", "failed"),
    ],
)
def test_safe_shared_error_classification(
    dsh_fake: tuple[DshClient, Path, Path], error: str, expected: str
) -> None:
    client, _, _ = dsh_fake
    result = client._decode(1, b"", (error + " secret-value").encode())
    assert result.outcome == expected
    assert "secret-value" not in dumps(asdict(result))


def test_usage_filters_non_counts(dsh_fake: tuple[DshClient, Path, Path]) -> None:
    client, _, _ = dsh_fake
    events = [
        SESSION,
        {
            "type": "status",
            "phase": "step_end",
            "usage": {
                "inputTokens": True,
                "outputTokens": -1,
                "cacheReadTokens": "secret",
                "totalTokens": 19,
                "invoice": 1,
                "reasoningTokens": float("inf"),
            },
        },
        END,
        FINAL,
    ]
    result = client._decode(0, "\n".join(json.dumps(event) for event in events).encode(), b"")
    assert result.outcome == "ok"
    assert result.usage == {"total_tokens": 19, "turn_end": "completed"}


@pytest.mark.parametrize(("effort", "settings"), [
    ("", (8192, "disabled", "off")),
    ("off", (8192, "disabled", "off")),
    ("medium", (8192, "disabled", "off")),
    ("low", (32768, "enabled", "low")),
    ("high", (32768, "enabled", "high")),
    ("max", (32768, "enabled", "max")),
    ("high-extended", (131072, "enabled", "high")),
    ("-extended", (8192, "disabled", "off")),
    ("off-extended", (131072, "disabled", "off")),
])
def test_cli_profile_uses_the_shared_generation_settings(
    default_profile: bytes, effort: str, settings: tuple[int, str, str],
) -> None:
    configs = {item["id"]: item.get("config") for item in DshClient._patch(
        default_profile, "deepseek-flash", effort=effort) if "config" in item}
    llm = cast(dict[str, object], configs["llm-deepseek"])
    assert DshClient.output_settings(effort) == settings
    assert (llm["maxTokens"], llm["thinking"], llm["reasoningEffort"]) == settings


@pytest.mark.parametrize("change", ["missing", "renamed", "duplicate", "nested"])
def test_unrecognized_profile_fails_closed(default_profile: bytes, change: str) -> None:
    if change == "missing":
        default_profile = default_profile.replace(b"- id: agent\n", b"- id: unknown\n")
    elif change == "renamed":
        default_profile = default_profile.replace(b"@deepseek-ai/dsh-agent'", b"@evil/agent'")
    elif change == "duplicate":
        default_profile += b"\n- id: agent\n  name: '@deepseek-ai/dsh-agent'"
    else:
        default_profile += b"\n- name: unsafe-nested-group\n"
    with pytest.raises(ValueError):
        DshClient._patch(default_profile, "deepseek-flash")


def test_cancelled_request_never_launches(dsh_fake: tuple[DshClient, Path, Path]) -> None:
    client, _, record = dsh_fake
    result = client.call("deepseek-flash", "hello", {}, "run", lambda: True, api_key="key")
    assert result.outcome == "cancelled"
    assert not record.exists()


def test_nonzero_exit_cannot_be_success(dsh_fake: tuple[DshClient, Path, Path]) -> None:
    client, response, _ = dsh_fake
    response.write_text(dumps({"events": SUCCESS, "code": 1}), encoding="utf-8")
    result = client.call("deepseek-flash", "hello", {}, "run", lambda: False, api_key="key")
    assert result.outcome == "failed"


@pytest.mark.parametrize("outcome", ["timed_out", "cancelled", "failed"])
def test_runner_failure_propagates(
    dsh_fake: tuple[DshClient, Path, Path], monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    client, _, _ = dsh_fake

    def stopped(
        argv: list[str],
        cwd: Path,
        input_text: str,
        timeout: float,
        cancelled: Callable[[], bool],
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, int, bytes, bytes]:
        return outcome, -1, b"", b"secret-value"

    monkeypatch.setattr(client.native, "_run", stopped)
    result = client.call("deepseek-flash", "hello", {}, "run", lambda: False, api_key="key")
    assert result.outcome == outcome
    assert "secret-value" not in dumps(asdict(result))


def test_request_for_another_model_is_blocked_before_any_upstream_connection(
    dsh_fake: tuple[DshClient, Path, Path], default_profile: bytes, tmp_path: Path
) -> None:
    client, _, _ = dsh_fake
    script = tmp_path / "wrong-model-dsh"
    script.write_text(
        f"#!{Path(sys.executable).resolve()}\n"
        "import json, pathlib, sys, urllib.request\n"
        "args = sys.argv[1:]\n"
        "if '--dump-default-config' in args:\n"
        f"    sys.stdout.buffer.write({default_profile!r}); sys.exit(0)\n"
        "patch = json.loads(pathlib.Path(args[args.index('--patch') + 1]).read_text())\n"
        "base = next(p['config']['baseURL'] for p in patch if 'baseURL' in p.get('config', {}))\n"
        "body = json.dumps({'model': 'deepseek-v4-pro'}).encode()\n"
        "request = urllib.request.Request(base + '/v1/messages', body, method='POST')\n"
        "try: urllib.request.urlopen(request, timeout=5)\n"
        "except Exception as error: print(error, file=sys.stderr)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    client.executable = script
    result = client.call(
        "deepseek-flash", "Solve", {"type": "object"}, "pin-run", lambda: False, api_key="k" * 12
    )
    assert (result.outcome, result.message) == (
        "policy_blocked", "DSH asked for model 'deepseek-v4-pro'; pinned to deepseek-flash"
    )
    assert (client.root / "guard.log").read_text() == "/anthropic/v1/messages 400 28\n"


@pytest.fixture
def observed_guard(
    dsh_fake: tuple[DshClient, Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> Guard:
    guard = Guard("http://upstream.invalid")
    guard.count = 1

    @contextmanager
    def transport(
        upstream: str, allowed: int = 1, log_path: Path | None = None,
        models: frozenset[str] = frozenset(),
    ) -> Iterator[tuple[str, Guard]]:
        yield "http://127.0.0.1:1", guard

    monkeypatch.setattr(dsh_module, "bound_transport", transport)
    _, response, _ = dsh_fake
    response.write_text(dumps({"events": [SESSION, FINAL], "code": 1,
                               "stderr": "transport-secret-sentinel"}))
    return guard


@pytest.fixture
def guarded_clients(
    dsh_fake: tuple[DshClient, Path, Path], observed_guard: Guard,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ProviderClients]:
    monkeypatch.setenv("ORCHESTRA_DSH_ACCOUNT", "0")
    clients = ProviderClients(tmp_path / "providers")
    clients.dsh = dsh_fake[0]
    operation = clients.access.operation("dsh", 900, "budget")
    clients.access.authorize("dsh", 900, "budget", str(operation["operation_digest"]),
                             "fake-authorized-test-key")
    assert clients.access.reserve("dsh", "previous-uncertain-attempt", 300)
    yield clients
    clients.close()


@pytest.mark.parametrize(
    ("status", "outcome", "rejected"),
    [
        (400, "failed", True), (401, "auth_required", True), (402, "quota_exhausted", True),
        (403, "auth_required", True), (404, "failed", True), (413, "failed", True),
        (415, "failed", True), (422, "failed", True), (429, "rate_limited", True),
        (408, "failed", False), (409, "failed", False), (418, "failed", False),
        (499, "failed", False), (500, "failed", False), (502, "failed", False),
        (503, "failed", False), (504, "failed", False), (None, "failed", False),
    ],
)
def test_authoritative_http_rejections_release_only_the_current_reservation(
    guarded_clients: ProviderClients, observed_guard: Guard,
    status: int | None, outcome: str, rejected: bool,
) -> None:
    observed_guard.upstream_status = status
    result = guarded_clients.call("dsh", "deepseek-flash", "Solve", {}, "new-attempt",
                                  lambda: False)
    ledger = guarded_clients.access.status("dsh")
    assert result.outcome == outcome
    assert result.usage["forwarded"] == 1
    assert ("rejected" in result.usage) is rejected
    if rejected:
        assert result.usage["rejected"] == status
    assert (ledger["spent_cents"], ledger["reserved_cents"]) == (0, 300 if rejected else 600)
    assert "transport-secret-sentinel" not in dumps(asdict(result))
    if status is not None:
        assert result.message == f"DSH upstream HTTP {status}"
    assert not (guarded_clients.dsh.root / "last-stderr.txt").exists()
    assert not (guarded_clients.dsh.root / "last-stdout.txt").exists()


@pytest.mark.parametrize("status", [401, 403])
def test_authoritative_auth_rejection_overrides_a_misleading_success_stream(
    dsh_fake: tuple[DshClient, Path, Path], observed_guard: Guard, status: int,
) -> None:
    client, response, _ = dsh_fake
    observed_guard.upstream_status = status
    response.write_text(dumps({"events": SUCCESS, "code": 0}))
    result = client.call("deepseek-flash", "Solve", {}, "auth-rejected", lambda: False,
                         api_key="fake-authorized-test-key")
    assert result.outcome == "auth_required"
    assert result.usage == {"forwarded": 1, "rejected": status}


def test_local_bound_after_successful_upstream_does_not_release_exposure(
    guarded_clients: ProviderClients, observed_guard: Guard,
    dsh_fake: tuple[DshClient, Path, Path],
) -> None:
    observed_guard.upstream_status = 200
    _, response, _ = dsh_fake
    response.write_text(dumps({"events": [], "code": 1, "stderr": "402 quota exceeded"}))
    result = guarded_clients.call("dsh", "deepseek-flash", "Solve", {}, "new-attempt",
                                  lambda: False)
    ledger = guarded_clients.access.status("dsh")
    assert result.outcome == "quota_exhausted"
    assert result.usage["forwarded"] == 1 and "rejected" not in result.usage
    assert (ledger["spent_cents"], ledger["reserved_cents"]) == (0, 600)
