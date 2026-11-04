"""HTTP + Server-Sent Events for `orch studio` (contract: docs/STUDIO.md).

Every request needs the token: `Authorization: Bearer`, or for GETs a one-time `?token=`
swapped for an HttpOnly cookie. A POST additionally needs a header token (Bearer or
X-Orch-Token): a cookie alone never authorises a change, so another site open in the same
browser cannot drive the studio. Loopback binds only answer loopback Host headers (no DNS
rebinding). Keys are write-only: no response ever carries one.
"""

from __future__ import annotations

import hmac
import json
import socket
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

from orchestragent.orch_watch_server import LOOPBACK_HOSTS, is_loopback, new_token

API = 1
COOKIE = "orch_studio"
MAX_BODY = 1_000_000
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


class StudioError(Exception):
    """An error a person can read; `code` and the HTTP status follow docs/STUDIO.md."""

    STATUS = {"bad_request": 400, "unauthorized": 401, "not_found": 404, "conflict": 409,
              "invalid": 422, "internal": 500}

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code if code in self.STATUS else "internal"
        self.message = message


class Backend(Protocol):
    """What the server needs; RepoBackend (a real repository) and DemoBackend implement it."""

    demo: bool

    def boot(self) -> dict[str, Any]: ...
    def runs(self) -> list[dict[str, Any]]: ...
    def graph(self, run: str) -> dict[str, Any]: ...
    def node(self, run: str, task: str) -> dict[str, Any]: ...
    def seq(self, run: str) -> int: ...
    def runs_seq(self) -> int: ...
    def message(self, run: str, task: str, text: str, rework: bool) -> dict[str, Any]: ...
    def route(self, run: str, task: str, route: str) -> dict[str, Any]: ...
    def retry(self, run: str, task: str) -> dict[str, Any]: ...
    def start(self, spec: str) -> dict[str, Any]: ...
    def resume(self, run: str) -> dict[str, Any]: ...
    def cancel(self, run: str) -> dict[str, Any]: ...
    def settings(self) -> dict[str, Any]: ...
    def set_key(self, provider: str, key: str) -> dict[str, Any]: ...
    def set_role(self, role: str, route: str) -> dict[str, Any]: ...


@dataclass
class Settings:
    token: str
    poll: float = 0.4
    ping: float = 10.0
    clock: Callable[[], float] = time.time


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], backend: Backend, settings: Settings) -> None:
        if not settings.token:
            raise ValueError("orch studio always needs a token")
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        self.backend = backend
        self.settings = settings
        self.stopping = threading.Event()
        self.loopback = is_loopback(address[0])
        super().__init__(address, Handler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)

    def server_close(self) -> None:
        self.stopping.set()
        super().server_close()


def _text(body: dict[str, Any], key: str, *, required: bool = True, limit: int = 20_000) -> str:
    value = body.get(key, "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise StudioError("bad_request", f"`{key}` must be a non-empty string")
    if len(value) > limit:
        raise StudioError("bad_request", f"`{key}` is longer than {limit} characters")
    return value


class Handler(BaseHTTPRequestHandler):
    server: StudioServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        """Silent: request lines can carry the one-time token."""

    # --- auth -----------------------------------------------------------------------
    def _host_ok(self) -> bool:
        if not self.server.loopback:
            return True
        host = self.headers.get("Host", "")
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        return name.lower() in LOOPBACK_HOSTS

    def _header_token(self) -> bool:
        token = self.server.settings.token.encode()
        bearer = self.headers.get("Authorization", "")
        if bearer.startswith("Bearer ") and hmac.compare_digest(bearer[7:].strip().encode(),
                                                                token):
            return True
        other = self.headers.get("X-Orch-Token", "")
        return bool(other) and hmac.compare_digest(other.strip().encode(), token)

    def _cookie_token(self) -> bool:
        try:
            jar = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        morsel = jar.get(COOKIE)
        return morsel is not None and hmac.compare_digest(
            morsel.value.encode(), self.server.settings.token.encode())

    # --- responses --------------------------------------------------------------------
    def _send(self, status: int, body: bytes, kind: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in {**HEADERS, **(extra or {})}.items():
            self.send_header(key, value)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, value: object, status: int = 200) -> None:
        self._send(status, json.dumps(value, allow_nan=False, default=str).encode(),
                   "application/json; charset=utf-8")

    def _error(self, code: str, message: str) -> None:
        self._json({"error": {"code": code, "message": message}}, StudioError.STATUS[code])

    # --- routing --------------------------------------------------------------------
    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parts.query)
        if not self._host_ok():
            self._error("unauthorized", "this server only answers loopback host names")
            return
        offered = query.get("token", [""])[0]
        token = self.server.settings.token
        if offered and hmac.compare_digest(offered.encode(), token.encode()):
            rest = {k: v for k, v in query.items() if k != "token"}
            target = parts.path + ("?" + urllib.parse.urlencode(rest, doseq=True) if rest else "")
            self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {
                "Location": target or "/",
                "Set-Cookie": f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"})
            return
        if not (self._header_token() or self._cookie_token()):
            self._error("unauthorized", "missing or wrong token")
            return
        one = {key: values[0] for key, values in query.items()}
        backend = self.server.backend
        try:
            if parts.path == "/":
                self._send(200, LANDING.encode(), "text/html; charset=utf-8")
            elif parts.path == "/api/boot":
                self._json({"api": API, **backend.boot()})
            elif parts.path == "/api/runs":
                self._json(backend.runs())
            elif parts.path == "/api/graph":
                self._json(backend.graph(self._need(one, "run")))
            elif parts.path == "/api/node":
                self._json(backend.node(self._need(one, "run"), self._need(one, "task")))
            elif parts.path == "/api/settings":
                self._json(backend.settings())
            elif parts.path == "/api/events":
                self._stream(one.get("run", ""))
            else:
                self._error("not_found", f"no such endpoint {parts.path}")
        except StudioError as error:
            self._error(error.code, error.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:  # noqa: BLE001 - reported to the client, server keeps going
            self._error("internal", f"{type(error).__name__}: {error}"[:300])

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        if not self._host_ok():
            self._error("unauthorized", "this server only answers loopback host names")
            return
        if not self._header_token():
            self._error("unauthorized", "a change needs the token in a header")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise StudioError("bad_request", "request body too large")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                raise StudioError("bad_request", "the body must be JSON") from None
            if not isinstance(body, dict):
                raise StudioError("bad_request", "the body must be a JSON object")
            self._json(self._post(parts.path, body))
        except StudioError as error:
            self._error(error.code, error.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:  # noqa: BLE001
            self._error("internal", f"{type(error).__name__}: {error}"[:300])

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        backend = self.server.backend
        if path == "/api/node/message":
            rework = body.get("rework", False)
            if not isinstance(rework, bool):
                raise StudioError("bad_request", "`rework` must be true or false")
            return {"ok": True, "node": backend.message(
                _text(body, "run"), _text(body, "task"), _text(body, "text"), rework)}
        if path == "/api/node/route":
            return {"ok": True, "node": backend.route(
                _text(body, "run"), _text(body, "task"), _text(body, "route", required=False))}
        if path == "/api/node/retry":
            return {"ok": True, "node": backend.retry(_text(body, "run"), _text(body, "task"))}
        if path == "/api/run/start":
            return {"ok": True, "run": backend.start(_text(body, "spec", limit=200_000))}
        if path == "/api/run/resume":
            return {"ok": True, "run": backend.resume(_text(body, "run"))}
        if path == "/api/run/cancel":
            return {"ok": True, "run": backend.cancel(_text(body, "run"))}
        if path == "/api/settings/key":
            return {"ok": True, "provider": backend.set_key(
                _text(body, "provider"), _text(body, "key", limit=4096))}
        if path == "/api/settings/role":
            backend.set_role(_text(body, "role"), _text(body, "route", required=False))
            return {"ok": True}
        raise StudioError("not_found", f"no such endpoint {path}")

    @staticmethod
    def _need(query: dict[str, str], key: str) -> str:
        value = query.get(key, "")
        if not value:
            raise StudioError("bad_request", f"query parameter `{key}` is required")
        return value

    # --- events -----------------------------------------------------------------------
    def _stream(self, run: str) -> None:
        settings, backend = self.server.settings, self.server.backend
        self.send_response(200)
        for key, value in HEADERS.items():
            self.send_header(key, value)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        seen = backend.seq(run) if run else 0
        seen_runs = backend.runs_seq()
        self._event("hello", {"seq": seen})
        quiet = settings.clock()
        while not self.server.stopping.is_set():
            current = backend.seq(run) if run else 0
            if current != seen:
                seen = current
                self._event("changed", {"run": run, "seq": current})
                quiet = settings.clock()
            runs_now = backend.runs_seq()
            if runs_now != seen_runs:
                seen_runs = runs_now
                self._event("runs", {})
                quiet = settings.clock()
            if settings.clock() - quiet >= settings.ping:
                self._event("ping", {})
                quiet = settings.clock()
            self.server.stopping.wait(settings.poll)

    def _event(self, name: str, value: object) -> None:
        data = json.dumps(value, separators=(",", ":"), allow_nan=False, default=str)
        self.wfile.write(f"event: {name}\ndata: {data}\n\n".encode())
        self.wfile.flush()


LANDING = """<!doctype html><meta charset="utf-8"><title>orch studio</title>
<body style="font:14px -apple-system,system-ui,sans-serif;margin:3em;max-width:42em">
<h1>orch studio</h1><p>This server speaks the JSON API in docs/STUDIO.md to the Orch Studio
app. Open the app, or call <code>/api/boot</code> with the token.</p></body>"""


def serve(backend: Backend, host: str, port: int, *, token: str = "", machine: bool = False,
          version: str = "", notify: Callable[[str], None] | None = None) -> int:
    """Serve until Ctrl-C (or, with `machine`, until stdin closes)."""
    token = token or new_token()
    server = StudioServer((host, port), backend, Settings(token))
    bound = server.server_address[1]
    shown = f"[{host}]" if ":" in host else host
    url = f"http://{'127.0.0.1' if host in {'', '0.0.0.0'} else shown}:{bound}/"
    say = notify or (lambda line: print(line, file=sys.stderr, flush=True))
    if machine:
        import os
        print(json.dumps({"url": url, "token": token, "pid": os.getpid(),
                          "version": version, "api": API}), flush=True)

        def watch_stdin() -> None:
            try:
                while sys.stdin.read(65536):
                    pass
            except (OSError, ValueError):
                pass
            server.shutdown()

        threading.Thread(target=watch_stdin, daemon=True).start()
    else:
        say(f"orch studio: serving {url}" + (" (demo)" if backend.demo else ""))
        say(f"token: {token}")
        say(f"open once in a browser: {url}?token={token}")
        if not is_loopback(host):
            say("Not loopback: prefer `ssh -L PORT:localhost:PORT host` on shared networks.")
        say("Ctrl-C to stop.")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        say("orch studio: stopped")
    finally:
        server.server_close()
    return 0
