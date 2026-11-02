import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from orchestragent import byok
from orchestragent.orch_usage import settled_json_failure, transient

SCHEMA: dict[str, object] = {"type": "object", "properties": {"x": {"type": "integer"}}}


class Fake(BaseHTTPRequestHandler):
    reply: tuple[int, Any] = (200, {})
    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.seen.append({"path": self.path, "auth": self.headers["Authorization"], **body})
        status, payload = Fake.reply
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[dict[str, Any]]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    Fake.seen = []
    yield {"type": "openai-compatible", "base_url": f"http://127.0.0.1:{httpd.server_port}/v1"}
    httpd.shutdown()


def completion(content: str, finish: str = "stop") -> dict[str, Any]:
    return {"model": "served-x", "choices": [{"message": {"content": content},
                                              "finish_reason": finish}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                      "prompt_tokens_details": {"cached_tokens": 3}}}


@pytest.mark.parametrize("content", ['{"x": 1}', '```json\n{"x": 1}\n```'])
def test_a_json_answer_with_usage(server: dict[str, Any], content: str) -> None:
    Fake.reply = (200, completion(content))
    result = byok.call(server, "sk-test-123456", "m", "p", SCHEMA, lambda: False, 10, "high")
    assert (result.outcome, result.data) == ("ok", {"x": 1})
    assert {k: result.usage[k] for k in ("input_tokens", "output_tokens",
                                          "cache_read_input_tokens", "served_model")} == {
        "input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 3,
        "served_model": "served-x"}
    sent = Fake.seen[0]
    assert (sent["path"], sent["auth"], sent["model"], sent["reasoning_effort"]) == (
        "/v1/chat/completions", "Bearer sk-test-123456", "m", "high")
    assert sent["response_format"] == {"type": "json_object"} and "tools" not in sent


@pytest.mark.parametrize(("status", "outcome", "is_transient"), [
    (401, "auth_required", False), (402, "quota_exhausted", False),
    (429, "rate_limited", False), (400, "failed", False), (503, "failed", True),
])
def test_http_errors_map_onto_orch_outcomes(server: dict[str, Any], status: int,
                                            outcome: str, is_transient: bool) -> None:
    Fake.reply = (status, {"error": {"message": "secret detail sk-abc"}})
    result = byok.call(server, "sk-test-123456", "m", "p", SCHEMA, lambda: False, 10)
    assert result.outcome == outcome
    assert transient(result.outcome, result.message, result.usage) is is_transient
    assert "sk-abc" not in result.message  # provider text is never echoed


def test_cut_or_invalid_answers_are_format_failures(server: dict[str, Any]) -> None:
    Fake.reply = (200, completion('{"x": 1', finish="length"))
    result = byok.call(server, "sk-test-123456", "m", "p", SCHEMA, lambda: False, 10)
    assert result.outcome == "failed" and result.usage["stop_reason"] == "max_tokens"
    Fake.reply = (200, completion("not json"))
    result = byok.call(server, "sk-test-123456", "m", "p", SCHEMA, lambda: False, 10)
    assert settled_json_failure(result.outcome, result.message,
                                {**result.usage, "settled_cents": 0})


def test_missing_key_and_unreachable_server_never_hang(tmp_path: Path) -> None:
    entry = {"base_url": "http://127.0.0.1:9/v1"}
    assert byok.call(entry, "", "m", "p", SCHEMA, lambda: False, 5).outcome == "auth_required"
    result = byok.call(entry, "sk-test-123456", "m", "p", SCHEMA, lambda: False, 5)
    assert result.outcome.startswith("exception:") and transient(result.outcome, "", {})


def test_registry_validates_names_urls_and_keys(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    assert "orch keys qwen" in byok.add(tmp_path, "qwen", "https://dashscope.example/v1")
    byok.add(tmp_path, "local", "http://localhost:11434/v1", key_env="LOCAL_KEY")
    with pytest.raises(ValueError):
        byok.add(tmp_path, "claude", "https://x.example/v1")  # built-in name
    with pytest.raises(ValueError):
        byok.add(tmp_path, "remote", "http://api.example.com/v1")  # plain http off-host
    entries = byok.registry(tmp_path)
    assert set(entries) == {"qwen", "local"}
    monkeypatch.setenv("LOCAL_KEY", "sk-from-env-123")
    assert byok.api_key(entries["local"], lambda: "stored") == "sk-from-env-123"
    assert byok.api_key(entries["qwen"], lambda: "sk-stored-1234") == "sk-stored-1234"
    byok.remove(tmp_path, "qwen")
    assert set(byok.registry(tmp_path)) == {"local"}
