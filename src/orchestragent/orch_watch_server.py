"""HTTP + Server-Sent Events for `orch watch`.

Loopback binds need no token but only answer loopback Host headers (no DNS rebinding).
Any other bind requires a token on every request: `?token=` once (swapped for an
HttpOnly cookie and redirected away), the cookie, or `Authorization: Bearer`. The
comparison is constant-time. The state database is only ever opened read-only.
"""

import getpass
import hmac
import ipaddress
import json
import secrets
import socket
import sqlite3
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from orchestragent.orch_watch import Source, connect, derive, latest_run, page, read, runs

COOKIE = "orch_watch"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


def is_loopback(host: str) -> bool:
    name = host.strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def new_token() -> str:
    return secrets.token_urlsafe(24)


@dataclass(frozen=True)
class Settings:
    db: Path
    run: str = ""  # empty: follow the newest run
    mode: str = "live"  # live | replay
    speed: float = 8.0
    token: str = ""  # empty only on loopback
    poll: float = 0.5
    clock: Callable[[], float] = time.time


class WatchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], settings: Settings) -> None:
        if not settings.token and not is_loopback(address[0]):
            raise ValueError("binding a non-loopback address requires a token")
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        self.settings = settings
        self.stopping = threading.Event()
        super().__init__(address, Handler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Browsers drop keep-alive and event-stream sockets all the time; stay quiet then."""
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)

    def server_close(self) -> None:
        self.stopping.set()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: WatchServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        """Silent: request lines can carry the one-time token."""

    # --- auth ---------------------------------------------------------------
    def _authorized(self, query: dict[str, list[str]]) -> bool | str:
        """True, False, or a redirect target after accepting a query token."""
        token = self.server.settings.token
        if not token:
            host = self.headers.get("Host", "")
            name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
            return name.lower() in LOOPBACK_HOSTS
        offered = query.get("token", [""])[0]
        if offered and hmac.compare_digest(offered.encode(), token.encode()):
            rest = {k: v for k, v in query.items() if k != "token"}
            path = urllib.parse.urlsplit(self.path).path
            return path + ("?" + urllib.parse.urlencode(rest, doseq=True) if rest else "")
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and hmac.compare_digest(
                header[7:].strip().encode(), token.encode()):
            return True
        try:
            jar = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        morsel = jar.get(COOKIE)
        return morsel is not None and hmac.compare_digest(morsel.value.encode(), token.encode())

    # --- responses ------------------------------------------------------------
    def _send(self, status: int, body: bytes, kind: str, extra: dict[str, str] | None = None
              ) -> None:
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

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parts.query)
        allowed = self._authorized(query)
        if isinstance(allowed, str):
            cookie = (f"{COOKIE}={self.server.settings.token}; HttpOnly; SameSite=Strict; "
                      "Path=/")
            self._send(HTTPStatus.SEE_OTHER, b"", "text/plain",
                       {"Location": allowed or "/", "Set-Cookie": cookie})
            return
        if not allowed:
            self._send(HTTPStatus.UNAUTHORIZED, b"unauthorized\n", "text/plain; charset=utf-8")
            return
        run = query.get("run", [""])[0] or self.server.settings.run
        settings = self.server.settings
        if parts.path == "/":
            boot = {"mode": settings.mode, "speed": settings.speed, "run": run}
            self._send(200, page(boot).encode(), "text/html; charset=utf-8")
        elif parts.path == "/api/runs":
            with self._database() as db:
                self._json(runs(db) if db else [])
        elif parts.path == "/api/model":
            with self._database() as db:
                if db is None:
                    self._json({"error": "no orchestration state yet"}, 404)
                    return
                chosen = run or latest_run(db)
                self._json(derive(read(db, chosen, settings.clock())) if chosen else None)
        elif parts.path == "/api/events":
            self._stream(run)
        elif parts.path == "/favicon.ico":
            self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")

    def _database(self) -> "Database":
        return Database(self.server.settings.db)

    def _stream(self, pinned: str) -> None:
        settings = self.server.settings
        self.send_response(200)
        for key, value in HEADERS.items():
            self.send_header(key, value)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        source = Source(settings.db, pinned, settings.clock)
        quiet = 0.0
        try:
            while not self.server.stopping.is_set():
                model = source.poll()
                if model is not None:
                    self._event("model", model)
                    quiet = settings.clock()
                elif settings.clock() - quiet > 10:
                    self._event("waiting" if source.seen is None else "ping",
                                {"db": settings.db.name})
                    quiet = settings.clock()
                self.server.stopping.wait(settings.poll)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            source.close()

    def _event(self, name: str, value: object) -> None:
        data = json.dumps(value, separators=(",", ":"), allow_nan=False, default=str)
        self.wfile.write(f"event: {name}\ndata: {data}\n\n".encode())
        self.wfile.flush()


class Database:
    """A per-request read-only connection; None while no state file exists yet."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.db: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection | None:
        if self.path.is_file():
            self.db = connect(self.path)
        return self.db

    def __exit__(self, *exc: object) -> None:
        if self.db is not None:
            self.db.close()


def hints(host: str, port: int, token: str) -> list[str]:
    """What to print: the URL and the exact ssh tunnel line for a laptop."""
    user, machine = getpass.getuser(), socket.getfqdn() or socket.gethostname()
    shown = machine if host in {"", "0.0.0.0", "::"} else f"[{host}]" if ":" in host else host
    if is_loopback(host):
        local = f"http://localhost:{port}/"
        lines = [f"orch watch: serving {local} (loopback only)",
                 "From a laptop, tunnel over SSH, then open " + local + ":",
                 f"  ssh -L {port}:localhost:{port} {user}@{machine}"]
        if token:
            lines.insert(1, f"token required: {local}?token={token}")
        return lines
    return [f"orch watch: serving http://{shown}:{port}/ (token required)",
            f"open once: http://{shown}:{port}/?token={token}",
            "or send 'Authorization: Bearer <token>'. Prefer the SSH tunnel on shared networks:",
            f"  ssh -L {port}:localhost:{port} {user}@{machine}"]


def say(line: str) -> None:
    print(line, flush=True)


def serve(settings: Settings, host: str, port: int,
          notify: Callable[[str], None] = say) -> int:
    server = WatchServer((host, port), settings)
    try:
        for line in hints(host, server.server_address[1], settings.token):
            notify(line)
        notify("Ctrl-C to stop.")
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        notify("orch watch: stopped")
    finally:
        server.server_close()
    return 0
