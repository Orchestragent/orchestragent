import json
from pathlib import Path

import pytest

from orchestragent.cursor import CursorClient
from orchestragent.native import NativeClients


class FakeNative(NativeClients):
    def __init__(self, root: Path, authenticated: bool = True) -> None:
        super().__init__(root)
        self.authenticated = authenticated
        self.calls: list[list[str]] = []

    def _run(
        self,
        argv: list[str],
        cwd: Path,
        input_text: str,
        timeout: float,
        cancelled: object,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, int, bytes, bytes]:
        self.calls.append(list(argv))
        if "status" in argv:
            body = {"isAuthenticated": self.authenticated}
            return "ok", 0, json.dumps(body).encode(), b""
        packet = {
            "is_error": False,
            "result": json.dumps({"answer": "ok"}),
            "session_id": "sess-1",
            "usage": {"inputTokens": 3},
        }
        return "ok", 0, json.dumps(packet).encode(), b""


@pytest.fixture
def client(tmp_path: Path) -> tuple[CursorClient, Path]:
    native = FakeNative(tmp_path / "native")
    cursor = CursorClient(native, tmp_path / "providers")
    cursor.executable = tmp_path / "agent"
    cursor.executable.write_text("#!/bin/sh\nexit 0\n")
    cursor.executable.chmod(0o700)
    return cursor, tmp_path


@pytest.mark.parametrize(
    ("authenticated", "ready", "auth"),
    [(True, True, "cursor_subscription"), (False, False, "logged_out")],
)
def test_status_reports_subscription_readiness(
    client: tuple[CursorClient, Path], authenticated: bool, ready: bool, auth: str
) -> None:
    cursor, _ = client
    native = cursor.native
    assert isinstance(native, FakeNative)
    native.authenticated = authenticated
    status = cursor.status()
    assert (status["installed"], status["ready"], status["auth"]) == (True, ready, auth)
    assert status["billing"] == "subscription_pool"


@pytest.mark.parametrize(
    ("model", "outcome"),
    [
        ("composer-2.5", "ok"),
        ("cursor-grok-4.5-high", "ok"),
        ("grok-4.7-xhigh-fast", "ok"),
        ("claude-opus-5-thinking-high", "policy_blocked"),
        ("gpt-5.3-codex", "policy_blocked"),
        ("auto", "failed"),
    ],
)
def test_only_subscription_pool_models_launch(
    client: tuple[CursorClient, Path], model: str, outcome: str
) -> None:
    cursor, _ = client
    result = cursor.call(model, "prompt", {"type": "object"}, "run-1", lambda: False)
    assert result.outcome == outcome


def test_pool_model_runs_in_ask_mode_without_force(client: tuple[CursorClient, Path]) -> None:
    cursor, _ = client
    result = cursor.call("composer-2.5", "prompt", {"type": "object"}, "run-plan", lambda: False)
    assert (result.outcome, result.data, result.session_id, result.usage) == (
        "ok",
        {"answer": "ok"},
        "sess-1",
        {"inputTokens": 3},
    )
    native = cursor.native
    assert isinstance(native, FakeNative)
    argv = native.calls[-1]
    assert ["--mode", "ask"] == argv[argv.index("--mode") : argv.index("--mode") + 2]
    assert "--sandbox" in argv and "enabled" in argv
    assert not {"--force", "-f", "--yolo"} & set(argv)
    assert argv[1:3] == ["--print", "--output-format"]


@pytest.mark.parametrize(
    ("model", "prompt", "run_id"),
    [
        ("", "prompt", "run"),
        ("x" * 129, "prompt", "run"),
        ("composer-2.5", "", "run"),
        ("composer-2.5", "x" * (NativeClients.prompt_limit + 1), "run"),
        ("composer-2.5", "prompt", "../escape"),
    ],
)
def test_invalid_requests_never_launch(
    client: tuple[CursorClient, Path], model: str, prompt: str, run_id: str
) -> None:
    cursor, _ = client
    result = cursor.call(model, prompt, {}, run_id, lambda: False)
    assert (result.outcome, result.message) == (
        "failed",
        "Explicit Cursor model and valid request required",
    )


def test_cancelled_request_is_not_reported_as_running(client: tuple[CursorClient, Path]) -> None:
    cursor, _ = client
    result = cursor.call("composer-2.5", "prompt", {}, "run", lambda: True)
    assert (result.outcome, result.data) == ("cancelled", {})


def test_non_json_result_is_reported_as_failure(client: tuple[CursorClient, Path]) -> None:
    cursor, _ = client
    native = cursor.native
    assert isinstance(native, FakeNative)

    def broken(*_args: object, **_kwargs: object) -> tuple[str, int, bytes, bytes]:
        return "ok", 0, b"not json", b""

    native._run = broken  # type: ignore[method-assign]
    result = cursor.call("composer-2.5", "prompt", {}, "run", lambda: False)
    assert (result.outcome, result.message) == ("failed", "Invalid Cursor JSON result")
