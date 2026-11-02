"""Hard request bound for one DSH run: the upstream model endpoint is reached once.

The official agent loop can continue after an unexpected tool call, so a budget
reservation alone cannot bound spend. This guard sits on the model transport: the
first inference request is forwarded, every later one is refused locally and never
reaches the provider, so a single reservation is a real ceiling.

Responses stream through as they arrive: the DSH SDK aborts a Messages stream after
300 s without data, so buffering a long generation would turn it into a client timeout.
"""

import http.client
import json
import re
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

FORWARD_HEADERS = ("content-type", "accept", "authorization", "x-api-key", "user-agent")
REPLY_HEADERS = ("content-type", "content-encoding")
SERVED_MODEL = re.compile(rb'"model"\s*:\s*"([A-Za-z0-9._:-]{1,80})"')
STOP_REASON = re.compile(rb'"stop_reason"\s*:\s*"([a-z_]{1,32})"')
STREAM_CHUNK = 64 * 1024
OBSERVE_CARRY = 256  # longer than any whitelisted marker, so one split across chunks is seen
STOP_REASONS = frozenset({"end_turn", "stop_sequence", "tool_use", "max_tokens", "refusal",
                          "pause_turn"})


def request_shape(body: bytes) -> dict[str, object]:
    """The forwarded request's output limit and reasoning settings; nothing else.

    Labels are enums and the limit a positive integer; any other value is "invalid".
    """
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        value = None
    if not isinstance(value, dict):
        return {}
    shape: dict[str, object] = {}
    limit = value.get("max_tokens")
    if type(limit) is int and limit > 0:
        shape["wire_max_tokens"] = limit
    thinking = value.get("thinking")
    kind = thinking.get("type") if isinstance(thinking, dict) else None
    shape["wire_thinking"] = (
        "absent" if thinking is None else
        kind if isinstance(kind, str) and kind in {"enabled", "disabled"} else "invalid")
    config = value.get("output_config")
    effort = config.get("effort") if isinstance(config, dict) else None
    shape["wire_effort"] = (
        "absent" if config is None else
        effort if isinstance(effort, str) and effort in {"low", "high", "max"} else "invalid")
    return shape


class Guard:
    def __init__(
        self,
        upstream: str,
        allowed: int = 1,
        log_path: Path | None = None,
        models: frozenset[str] = frozenset(),
    ) -> None:
        parts = urlsplit(upstream)
        if parts.scheme not in {"https", "http"} or not parts.hostname:
            raise ValueError("Guard requires an http(s) upstream")
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.allowed = allowed
        self.log_path = log_path
        # Empty means any model; otherwise the request body must name one of these.
        self.models = models
        self.served_model = ""
        self.stop_reason = ""
        self.request: dict[str, object] = {}
        self.refused_model = ""
        self.upstream_status: int | None = None
        self.count = 0
        self.lock = threading.Lock()
        self.interrupted = False  # the stream broke after headers reached the client
        self.closing = False
        self.active: set[socket.socket] = set()  # open upstream sockets of this transport
        self._carry = b""

    def permits(self, body: bytes) -> bool:
        if not self.models:
            return True
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError):
            value = None
        model = value.get("model") if isinstance(value, dict) else None
        if model in self.models:
            return True
        name = model if isinstance(model, str) else "(none)"
        with self.lock:
            self.refused_model = self.refused_model or re.sub(r"[^A-Za-z0-9._:-]", "?", name)[:80]
        return False

    def observe(self, payload: bytes) -> None:
        """Keep only the served model and stop reason from streamed bytes; never the body.

        A short carry from the previous chunk joins markers split across chunks.
        """
        with self.lock:
            payload, self._carry = self._carry + payload, (self._carry + payload)[-OBSERVE_CARRY:]
        match = SERVED_MODEL.search(payload)
        stops = STOP_REASON.findall(payload)  # the final message_delta carries the reason
        with self.lock:
            if match is not None:
                self.served_model = self.served_model or match.group(1).decode("ascii")
            if stops:
                stop = stops[-1].decode("ascii")
                self.stop_reason = stop if stop in STOP_REASONS else "other"

    def record(self, path: str, status: int, size: int) -> None:
        """Persist request shape only: method path and upstream status. No bodies."""
        if self.log_path is None:
            return
        line = f"{path} {status} {size}\n"
        with self.lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def claim(self) -> bool:
        with self.lock:
            if self.count >= self.allowed:
                return False
            self.count += 1
            return True

    def close_upstream(self) -> None:
        """Stop any upstream stream still open, so no request outlives this transport.

        shutdown() closes the socket even while a response file still references it and
        wakes a reader blocked on it; close() alone would leave the stream running.
        """
        with self.lock:
            self.closing = True
            sockets = tuple(self.active)
        for upstream in sockets:
            try:
                upstream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    @property
    def forwarded(self) -> int:
        with self.lock:
            return self.count


class Handler(BaseHTTPRequestHandler):
    server: "GuardServer"

    def log_message(self, *args: object) -> None:
        return

    def _reply(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        guard = self.server.guard
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        if not guard.permits(body):
            # 400 is not retryable either; the pinned model is the only one that may bill.
            guard.record(self.path, 400, len(body))
            payload = (
                b'{"error":{"type":"model_not_allowed","message":'
                b'"this run is pinned to another model"}}'
            )
            self._reply(400, payload, "application/json")
            return
        if not guard.claim():
            payload = (
                b'{"error":{"type":"request_bound_exceeded","message":'
                b'"this run already used its single model request"}}'
            )
            # 402 maps to QUOTA, which is not a retryable code in the DSH adapter,
            # so the loop stops instead of retrying against the bound.
            self._reply(402, payload, "application/json")
            return
        factory = (
            http.client.HTTPSConnection if guard.scheme == "https" else http.client.HTTPConnection
        )
        connection = factory(guard.host, guard.port, timeout=180)
        upstream: socket.socket | None = None
        started = False
        status, size = 502, 0
        response: http.client.HTTPResponse | None = None
        try:
            headers = {
                key: self.headers[key]
                for key in FORWARD_HEADERS
                if self.headers.get(key) is not None
            }
            headers["content-length"] = str(len(body))
            with guard.lock:
                guard.request = request_shape(body)
            connection.request("POST", self.path, body, headers)
            # getresponse() may detach the socket from the connection; keep our own handle.
            upstream = connection.sock
            with guard.lock:
                closing = guard.closing
                if upstream is not None:
                    guard.active.add(upstream)
            if closing and upstream is not None:
                upstream.shutdown(socket.SHUT_RDWR)
            response = connection.getresponse()
            status = response.status
            with guard.lock:
                guard.upstream_status = response.status
            self.send_response(response.status)
            for key in REPLY_HEADERS + ("content-length",):
                value = response.getheader(key)
                if value is not None:
                    self.send_header(key, value)
            self.send_header("Connection", "close")  # close-delimited when length is unknown
            self.end_headers()
            started = True
            while chunk := response.read1(STREAM_CHUNK):
                guard.observe(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
                size += len(chunk)
            declared = response.getheader("content-length", "")
            short = declared.isdigit() and size < int(declared)
            with guard.lock:  # EOF is normal only when no length was promised and not shut
                guard.interrupted = guard.interrupted or short or guard.closing
        except (OSError, http.client.HTTPException):
            if started:
                # Headers already went out: never append a second response. The client sees
                # a truncated stream; the upstream connection is closed below.
                with guard.lock:
                    guard.interrupted = True
            else:
                try:
                    self._reply(502, b'{"error":{"type":"upstream_unreachable"}}',
                                "application/json")
                except OSError:
                    pass
        finally:
            guard.record(self.path, status, size)
            with guard.lock:
                if upstream is not None:
                    guard.active.discard(upstream)
                guard._carry = b""  # no response bytes outlive the stream
            if response is not None:
                response.close()
            connection.close()

    def do_GET(self) -> None:
        self._reply(404, b"{}", "application/json")


class GuardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, guard: Guard) -> None:
        self.guard = guard
        super().__init__(("127.0.0.1", 0), Handler)


@contextmanager
def bound_transport(
    upstream: str,
    allowed: int = 1,
    log_path: Path | None = None,
    models: frozenset[str] = frozenset(),
) -> Iterator[tuple[str, Guard]]:
    """Yield (base_url, guard); the base_url replaces the provider endpoint."""
    guard = Guard(upstream, allowed, log_path, models)
    server = GuardServer(guard)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", guard
    finally:
        guard.close_upstream()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
