import io
import json
import stat
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest

from orchestragent import access as access_module
from orchestragent import deepseek_api
from orchestragent.access import Access
from orchestragent.orch_cli import keys_lines, main
from orchestragent.providers import ProviderClients, dsf_transport

KEY = "sk-fake-deepseek-test-key"
SCHEMA: dict[str, object] = {"type": "object"}


def message(text: str, stop: str = "end_turn") -> dict[str, object]:
    return {"id": "msg_1", "type": "message", "model": "deepseek-flash", "stop_reason": stop,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 4000, "output_tokens": 300,
                      "cache_read_input_tokens": 1000}}


@dataclass
class FakeDeepSeek:
    """A local stand-in for the Anthropic-compatible endpoint; records every request."""

    status: int = 200
    reply: dict[str, object] = field(default_factory=lambda: message('{"status": "DONE"}'))
    requests: list[tuple[dict[str, str], dict[str, object]]] = field(default_factory=list)
    hold: threading.Event | None = None


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDeepSeek]:
    state = FakeDeepSeek()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server naming
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            state.requests.append(({k.lower(): v for k, v in self.headers.items()}, body))
            if state.hold is not None:
                state.hold.wait(5)
            payload = json.dumps(state.reply).encode()
            self.send_response(state.status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    monkeypatch.setenv("ORCHESTRA_DEEPSEEK_URL",
                       f"http://127.0.0.1:{server.server_port}/anthropic/v1/messages")
    yield state
    server.shutdown()


def test_one_pinned_request_with_the_json_contract(fake: FakeDeepSeek, tmp_path: Path) -> None:
    result = deepseek_api.call(KEY, "deepseek-flash", "Implement add.", SCHEMA, lambda: False,
                               30, tmp_path / "guard.log")
    [(headers, body)] = fake.requests
    assert (result.outcome, result.data, result.session_id) == ("ok", {"status": "DONE"}, "msg_1")
    assert result.usage == {"forwarded": 1, "input_tokens": 4000, "output_tokens": 300,
                            "cache_read_input_tokens": 1000, "served_model": "deepseek-flash",
                            "stop_reason": "end_turn", "wire_max_tokens": 8192,
                            "wire_thinking": "disabled", "wire_effort": "absent"}
    assert (body["model"], body["max_tokens"]) == ("deepseek-flash", 8192)
    assert "tools" not in body and "Respond with ONE JSON object" in str(body["messages"])
    assert headers["x-api-key"] == KEY
    assert (tmp_path / "guard.log").read_text().startswith("api /v1/messages 200 ")


@pytest.mark.parametrize(("effort", "sent", "wire"), [
    ("", {"max_tokens": 8192, "thinking": {"type": "disabled"}},
     {"wire_max_tokens": 8192, "wire_thinking": "disabled", "wire_effort": "absent"}),
    ("high", {"max_tokens": 32768, "thinking": {"type": "enabled"},
              "output_config": {"effort": "high"}},
     {"wire_max_tokens": 32768, "wire_thinking": "enabled", "wire_effort": "high"}),
    ("high-extended", {"max_tokens": 131072, "thinking": {"type": "enabled"},
                       "output_config": {"effort": "high"}},
     {"wire_max_tokens": 131072, "wire_thinking": "enabled", "wire_effort": "high"}),
])
def test_direct_api_sends_the_route_effort_and_records_what_it_sent(
    fake: FakeDeepSeek, effort: str, sent: dict[str, object], wire: dict[str, object],
) -> None:
    result = deepseek_api.call(KEY, "deepseek-flash", "x", SCHEMA, lambda: False, 30,
                               effort=effort)
    [(_, body)] = fake.requests
    assert {key: body[key] for key in sent} == sent and ("output_config" in body) is (
        effort != "")
    assert {key: result.usage[key] for key in wire} == wire


@pytest.mark.parametrize("prompt", ["x" * 65536, "中文代码\n" * 100000])
def test_large_context_reaches_transport_without_byte_to_token_conversion(
    fake: FakeDeepSeek, prompt: str,
) -> None:
    result = deepseek_api.call(KEY, "deepseek-flash", prompt, SCHEMA, lambda: False, 30)
    assert result.outcome == "ok" and len(fake.requests) == 1
    messages = cast(list[dict[str, str]], fake.requests[0][1]["messages"])
    assert messages[0]["content"].startswith(prompt)


@pytest.mark.parametrize(
    ("status", "outcome", "rejected"),
    [(401, "auth_required", True), (402, "quota_exhausted", True), (429, "rate_limited", True),
     (400, "failed", True), (408, "failed", False), (409, "failed", False),
     (500, "failed", False)],
)
def test_http_errors_map_to_outcomes_and_mark_unbilled_rejections(
    fake: FakeDeepSeek, status: int, outcome: str, rejected: bool
) -> None:
    fake.status, fake.reply = status, {"error": {"message": "nope"}}
    result = deepseek_api.call(KEY, "deepseek-flash", "x", SCHEMA, lambda: False, 30)
    assert result.outcome == outcome and ("rejected" in result.usage) is rejected


@pytest.mark.parametrize(
    ("reply", "outcome", "message"),
    [
        (message('{"status": "DONE"}', stop="max_tokens"), "failed", "answer cut at 8192"),
        (message("I cannot do that."), "failed", "Invalid DeepSeek JSON result"),
        (message('```json\n{"status": "BLOCKED"}\n```'), "ok", ""),
    ],
)
def test_answers_must_be_one_json_object(
    fake: FakeDeepSeek, reply: dict[str, object], outcome: str, message: str
) -> None:
    fake.reply = reply
    result = deepseek_api.call(KEY, "deepseek-flash", "x", SCHEMA, lambda: False, 30)
    assert result.outcome == outcome and message in result.message
    assert result.usage["output_tokens"] == 300  # billed tokens are reported either way
    assert result.text == ("I cannot do that." if "JSON" in message else "")


def test_unpinned_models_and_bad_keys_never_leave_the_process(fake: FakeDeepSeek) -> None:
    blocked = deepseek_api.call(KEY, "deepseek-chat", "x", SCHEMA, lambda: False, 30)
    keyless = deepseek_api.call("", "deepseek-flash", "x", SCHEMA, lambda: False, 30)
    assert (blocked.outcome, keyless.outcome, fake.requests) == (
        "policy_blocked", "auth_required", []
    )


def test_cancelling_keeps_the_exposure(fake: FakeDeepSeek) -> None:
    fake.hold = threading.Event()
    flag = threading.Timer(0.3, lambda: None)
    flag.start()
    result = deepseek_api.call(KEY, "deepseek-flash", "x", SCHEMA, lambda: not flag.is_alive(),
                               30)
    fake.hold.set()
    assert result.outcome == "cancelled" and result.usage == {"forwarded": 1}


@pytest.fixture(params=["macos", "linux"])
def credential_platform(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def byok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, credential_platform: str,
) -> Iterator[ProviderClients]:
    # Exercise restoration on both platforms without touching the real login keychain.
    keychain: dict[str, str] = {}
    monkeypatch.setattr(access_module, "is_macos", lambda: credential_platform == "macos")
    monkeypatch.setattr(Access, "_keychain_store", staticmethod(keychain.__setitem__))
    monkeypatch.setattr(Access, "_keychain_load", lambda self, provider: keychain.get(provider, ""))
    monkeypatch.setenv("ORCHESTRA_KEYCHAIN", "1")
    monkeypatch.setenv("ORCHESTRA_DSF_TRANSPORT", "api")
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    lines = keys_lines(Access(tmp_path / "data"), "dsh", True)
    assert lines[0] == ("dsh: key stored on this machine; no local spending cap "
                        "(spent ¥0.00 so far)")
    clients = ProviderClients(tmp_path / "data")
    yield clients
    clients.close()


def test_byok_keys_settle_by_tokens_through_the_direct_api(
    fake: FakeDeepSeek, byok: ProviderClients, tmp_path: Path, credential_platform: str,
) -> None:
    result = byok.call("dsh", "deepseek-flash", "Implement add.", SCHEMA, "orch-r1-T-001-a1",
                       lambda: False)
    status = byok.access.status("dsh")
    assert dsf_transport() == "api"
    assert cast(dict[str, object], byok.status()["dsh"])["ready"] is True
    assert (result.outcome, result.usage["settled_cents"]) == ("ok", 2)
    assert (status["spent_cents"], status["reserved_cents"]) == (2, 0)
    if credential_platform == "linux":
        mode = (tmp_path / "data" / "secrets" / "dsh.key").stat().st_mode
        assert stat.S_IMODE(mode) == 0o600


def test_a_rejected_request_releases_its_reservation(
    fake: FakeDeepSeek, byok: ProviderClients
) -> None:
    fake.status, fake.reply = 401, {"error": {"message": "invalid key"}}
    result = byok.call("dsh", "deepseek-flash", "x", SCHEMA, "orch-r1-T-001-a1", lambda: False)
    status = byok.access.status("dsh")
    assert (result.outcome, len(fake.requests)) == ("auth_required", 1)
    assert (status["spent_cents"], status["reserved_cents"]) == (0, 0)


def test_keys_store_the_key_without_asking_for_a_budget(tmp_path: Path) -> None:
    access = Access(tmp_path / "data")
    lines = keys_lines(access, "jev", False, secret=lambda prompt: "fake-gateway-key-123")
    assert lines[0].startswith("jev: key stored on this machine; no local spending cap")
    assert access.status("jev")["authorized"] is True


def test_keys_cancelled_at_the_prompt_store_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    data = tmp_path / "data"
    assert main(["--data-dir", str(data), "keys", "jev"]) == 1
    assert "cancelled; nothing was stored" in capsys.readouterr().err
    assert Access(data).status("jev")["authorized"] is False
