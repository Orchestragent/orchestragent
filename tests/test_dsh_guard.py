import http.client
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import urlsplit

import pytest

from orchestragent.dsh_guard import Guard, bound_transport, request_shape


class Upstream(BaseHTTPRequestHandler):
    calls = 0
    status = 200

    def log_message(self, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).calls += 1
        payload = json.dumps({"received": json.loads(body), "path": self.path}).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def upstream() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Upstream.calls = 0
    Upstream.status = 200
    yield server
    server.shutdown()
    server.server_close()


def post(
    url: str, body: dict[str, object], headers: dict[str, str] | None = None
) -> tuple[int, dict[str, object]]:
    parts = urlsplit(url)
    assert parts.hostname is not None
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    merged = {"Content-Type": "application/json", **(headers or {})}
    connection.request("POST", parts.path, json.dumps(body), merged)
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()
    return response.status, payload


def test_second_request_is_refused_before_upstream(upstream: ThreadingHTTPServer) -> None:
    base = f"http://127.0.0.1:{upstream.server_port}"
    with bound_transport(base) as (url, guard):
        first = post(url + "/v1/messages", {"model": "deepseek-flash"})
        second = post(url + "/v1/messages", {"model": "deepseek-flash"})
    assert first[0] == 200
    assert first[1]["path"] == "/v1/messages"
    assert second[0] == 402
    error = cast(dict[str, object], second[1]["error"])
    assert error["type"] == "request_bound_exceeded"
    assert Upstream.calls == 1
    assert guard.forwarded == 1
    assert guard.upstream_status == 200  # the local second-request 402 cannot overwrite it


def test_pinned_model_is_refused_before_upstream_and_keeps_the_single_request(
    upstream: ThreadingHTTPServer,
) -> None:
    base = f"http://127.0.0.1:{upstream.server_port}"
    other: list[dict[str, object]] = [{"model": "deepseek-v4-pro"}, {"messages": []}]
    with bound_transport(base, models=frozenset({"deepseek-flash"})) as (url, guard):
        refused = [post(url + "/v1/messages", body)[0] for body in other]
        assert guard.upstream_status is None
        allowed = post(url + "/v1/messages", {"model": "deepseek-flash"})
    assert (refused, allowed[0], Upstream.calls, guard.forwarded) == ([400, 400], 200, 1, 1)
    assert guard.served_model == "deepseek-flash"


@pytest.mark.parametrize(("body", "shape"), [
    (b'{"max_tokens":8192,"thinking":{"type":"disabled"}}',
     {"wire_max_tokens": 8192, "wire_thinking": "disabled", "wire_effort": "absent"}),
    (b'{"max_tokens":16384,"thinking":{"type":"enabled"},"output_config":{"effort":"high"}}',
     {"wire_max_tokens": 16384, "wire_thinking": "enabled", "wire_effort": "high"}),
    (b'{"max_tokens":true,"thinking":"on","output_config":{"effort":"x"}}',
     {"wire_thinking": "invalid", "wire_effort": "invalid"}),
    (b'{"messages":[]}', {"wire_thinking": "absent", "wire_effort": "absent"}),
    (b'{"thinking":{"type":[]},"output_config":{"effort":{}}}',
     {"wire_thinking": "invalid", "wire_effort": "invalid"}),
    (b'{"thinking":{"type":{"x":1}},"output_config":{"effort":["high"]}}',
     {"wire_thinking": "invalid", "wire_effort": "invalid"}),
    (b'{"thinking":{"type":true},"output_config":{"effort":null}}',
     {"wire_thinking": "invalid", "wire_effort": "invalid"}),
    (b'{"thinking":{"type":null},"output_config":[]}',
     {"wire_thinking": "invalid", "wire_effort": "invalid"}),
    (b"not json", {}),
])
def test_request_shape_keeps_only_whitelisted_limit_and_reasoning_settings(
    body: bytes, shape: dict[str, object],
) -> None:
    assert request_shape(body) == shape


def test_forwarded_request_shape_and_final_stop_reason_are_recorded(
    upstream: ThreadingHTTPServer,
) -> None:
    body: dict[str, object] = {"model": "deepseek-flash", "max_tokens": 8192,
                               "thinking": {"type": "disabled"}, "stop_reason": "max_tokens",
                               "messages": [{"role": "user", "content": "SECRET_TASK_TEXT"}]}
    with bound_transport(f"http://127.0.0.1:{upstream.server_port}") as (url, guard):
        assert post(url + "/v1/messages", body)[0] == 200
    assert (guard.request, guard.stop_reason) == (
        {"wire_max_tokens": 8192, "wire_thinking": "disabled", "wire_effort": "absent"},
        "max_tokens")
    assert "SECRET_TASK_TEXT" not in repr(vars(guard))


@pytest.mark.parametrize(("payload", "stop"), [
    (b'{"stop_reason":null}\n{"delta":{"stop_reason":"end_turn"}}', "end_turn"),
    (b'{"text":"\\"stop_reason\\":\\"max_tokens\\""}', ""),  # inside generated text
    (b'{"delta":{"stop_reason":"weird_reason"}}', "other"),
])
def test_stop_reason_is_whitelisted_and_ignores_escaped_text(payload: bytes, stop: str) -> None:
    guard = Guard("http://127.0.0.1:1")
    guard.observe(payload)
    assert guard.stop_reason == stop


def test_guard_counts_independently(upstream: ThreadingHTTPServer) -> None:
    guard = Guard(f"http://127.0.0.1:{upstream.server_port}", allowed=2)
    assert (guard.claim(), guard.claim(), guard.claim()) == (True, True, False)
    assert guard.forwarded == 2


def test_guard_rejects_non_http_upstream() -> None:
    with pytest.raises(ValueError):
        Guard("ftp://example.com")


@pytest.mark.parametrize("status", [200, 400, 401, 403, 408, 429, 499, 500, 503])
def test_guard_remembers_only_authoritative_upstream_status(
    upstream: ThreadingHTTPServer, status: int,
) -> None:
    Upstream.status = status
    base = f"http://127.0.0.1:{upstream.server_port}"
    with bound_transport(base) as (url, guard):
        reply, _ = post(url + "/v1/messages", {"secret": "body-sentinel"},
                        {"Authorization": "header-sentinel"})
        post(url + "/v1/messages", {})  # local 402 must not replace upstream evidence
    assert reply == guard.upstream_status == status
    assert "body-sentinel" not in repr(vars(guard))
    assert "header-sentinel" not in repr(vars(guard))


def test_guard_network_failure_does_not_invent_an_upstream_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = http.client.HTTPConnection.request

    def unreachable(
        connection: http.client.HTTPConnection, *args: object, **kwargs: object,
    ) -> None:
        if connection.host == "unreachable.invalid":
            raise OSError("network unavailable")
        original(connection, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(http.client.HTTPConnection, "request", unreachable)
    with bound_transport("http://unreachable.invalid") as (url, guard):
        status, _ = post(url + "/v1/messages", {})
    assert (status, guard.upstream_status, guard.forwarded) == (502, None, 1)


class Streaming(BaseHTTPRequestHandler):
    """SSE upstream: the first part at once, the rest only after `gate`, then keepalives."""

    parts: list[bytes] = []
    length = ""
    posts = 0
    gate = threading.Event()
    broken = threading.Event()

    def log_message(self, *args: object) -> None:
        return

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).posts += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if type(self).length:
            self.send_header("Content-Length", type(self).length)
        self.end_headers()
        try:
            for index, part in enumerate(type(self).parts):
                if index == 1:
                    type(self).gate.wait(10)
                self.wfile.write(part)
                self.wfile.flush()
                time.sleep(0.02)
            for _ in range(200):  # an upstream still generating sees the guard hang up
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(0.01)
        except OSError:
            type(self).broken.set()


@pytest.fixture
def streaming() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Streaming)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Streaming.parts, Streaming.length, Streaming.posts = [], "", 0
    Streaming.gate, Streaming.broken = threading.Event(), threading.Event()
    yield server
    Streaming.gate.set()
    server.shutdown()
    server.server_close()


def open_stream(url: str) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    parts = urlsplit(url)
    assert parts.hostname is not None
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    connection.request("POST", "/v1/messages", json.dumps({"model": "deepseek-flash"}),
                       {"Content-Type": "application/json"})
    return connection, connection.getresponse()


START = b'event: message_start\ndata: {"message":{"model":"deepseek-flash"}}\n\n'
STOP = b'event: message_delta\ndata: {"delta":{"stop_reason":"end_turn"}}\n\n'


def test_stream_reaches_the_client_before_the_upstream_finishes(
    streaming: ThreadingHTTPServer,
) -> None:
    Streaming.parts = [START, STOP]
    with bound_transport(f"http://127.0.0.1:{streaming.server_port}") as (url, guard):
        connection, response = open_stream(url)
        first = response.read1(65536)  # the upstream is still blocked on the gate
        assert first == START and not Streaming.gate.is_set()
        Streaming.gate.set()
        rest = response.read()
        connection.close()
    assert STOP in rest and (guard.served_model, guard.stop_reason) == (
        "deepseek-flash", "end_turn")
    assert (Streaming.posts, guard.forwarded, guard.interrupted) == (1, 1, False)


@pytest.mark.parametrize("split", [b'"stop_re', b'"stop_reason":"max', b'"model":"deepseek-fl'])
def test_markers_split_across_chunks_are_seen_and_no_body_bytes_are_kept(
    streaming: ThreadingHTTPServer, split: bytes,
) -> None:
    stream = b'data: {"model":"deepseek-flash","SECRET_BODY":1}\n\n' + (
        b'data: {"delta":{"stop_reason":"max_tokens"}}\n\n')
    cut = stream.index(split) + len(split)
    Streaming.parts = [stream[:cut], stream[cut:]]
    Streaming.gate.set()
    with bound_transport(f"http://127.0.0.1:{streaming.server_port}") as (url, guard):
        connection, response = open_stream(url)
        assert stream in response.read()
        connection.close()
    assert (guard.served_model, guard.stop_reason) == ("deepseek-flash", "max_tokens")
    assert "SECRET_BODY" not in repr(vars(guard))


@pytest.mark.parametrize("ending", ["client-disconnects", "transport-exits"])
def test_an_abandoned_stream_closes_its_upstream_and_never_posts_again(
    streaming: ThreadingHTTPServer, ending: str,
) -> None:
    Streaming.parts = [START, STOP]
    with bound_transport(f"http://127.0.0.1:{streaming.server_port}") as (url, guard):
        connection, response = open_stream(url)
        assert response.read1(65536) == START
        if ending == "client-disconnects":
            response.close()
            connection.close()
            Streaming.gate.set()
            assert Streaming.broken.wait(5)
    if ending == "transport-exits":
        Streaming.gate.set()
        assert Streaming.broken.wait(5)
        response.close()
        connection.close()
    assert (Streaming.posts, guard.forwarded, guard.active) == (1, 1, set())
    assert guard.interrupted


def test_upstream_break_after_headers_is_not_followed_by_a_second_response(
    streaming: ThreadingHTTPServer,
) -> None:
    Streaming.parts, Streaming.length = [START], "100000"  # promises more than it sends
    Streaming.gate.set()
    with bound_transport(f"http://127.0.0.1:{streaming.server_port}") as (url, guard):
        connection, response = open_stream(url)
        with pytest.raises(http.client.IncompleteRead) as cut:
            response.read()
        connection.close()
    assert cut.value.partial.startswith(START) and b"upstream_unreachable" not in cut.value.partial
    assert response.status == 200 and guard.interrupted and Streaming.posts == 1
