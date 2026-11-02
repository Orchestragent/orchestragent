"""Temporary loopback form, exposed through HTTPS Serve or user-authorized Funnel.

Codes go directly to the official Claude CLI's stdin; API keys go to Access's
credential store. Never log request paths/bodies, subprocess output, or secrets.
This is a manually started, expiring setup session, not the product server.
"""

import argparse
import html
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from orchestragent.access import Access
from orchestragent.native_guard import stop_group
from orchestragent.platform import claude_binary


class Session:
    def __init__(self, root: Path, origin: str, lifetime: int = 1200, *,
                 keys_only: str | None = None, cap_cents: int = 500) -> None:
        parsed = urlparse(origin)
        allowed_host = bool(parsed.hostname and (
            parsed.hostname.endswith(".ts.net") or (keys_only is not None
            and parsed.hostname.endswith(".trycloudflare.com"))))
        if (parsed.scheme != "https" or not allowed_host or parsed.path
                or parsed.query or parsed.fragment or parsed.username):
            raise ValueError("An approved temporary HTTPS origin is required")
        self.origin = origin
        self.token = secrets.token_urlsafe(32)
        self.deadline = time.monotonic() + lifetime
        self.root = root
        self.provider = keys_only or "jev"
        Access.operation(self.provider, cap_cents, "budget")
        self.cap_cents = cap_cents
        self.url = ""
        self.code_sent = False
        self.key_saved = False
        self.lock = threading.Lock()
        self.process: subprocess.Popen[bytes] | None = None
        self.reader: threading.Thread | None = None
        if keys_only is not None:
            return
        self.process = subprocess.Popen(
            [str(claude_binary()), "auth", "login", "--claudeai"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.reader = threading.Thread(target=self._read_url, daemon=True)
        self.reader.start()

    def _read_url(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        # Discard everything except the public authorization URL; never relay output.
        for raw in self.process.stdout:
            line = raw.decode(errors="replace")
            match = re.search(r"https://claude\.(?:com|ai)/[^\s\x1b\x07]+", line)
            if match and not self.url:
                self.url = match.group(0)

    def submit(self, values: dict[str, list[str]]) -> str:
        with self.lock:
            if time.monotonic() >= self.deadline:
                raise ValueError("Session expired")
            return self._submit(values)

    def _submit(self, values: dict[str, list[str]]) -> str:
        if not secrets.compare_digest(values.get("csrf", [""])[0], self.token):
            raise ValueError("Invalid session")
        action = values.get("action", [""])[0]
        if action == "claude":
            code = values.get("code", [""])[0].strip()
            if (self.process is None or self.code_sent or self.process.poll() is not None
                    or not code
                    or len(code) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in code)):
                raise ValueError("Code unavailable or already submitted")
            assert self.process.stdin is not None
            self.process.stdin.write((code + "\n").encode())
            self.process.stdin.flush()
            self.code_sent = True
            return "授权码已交给官方 Claude 登录程序。稍后刷新查看状态。"
        if action == self.provider:
            if self.key_saved:
                raise ValueError("Key already saved")
            key = values.get("key", [""])[0].strip()
            access = Access(self.root)
            operation = access.operation(self.provider, self.cap_cents, "budget")
            access.authorize(self.provider, self.cap_cents, "budget",
                             str(operation["operation_digest"]), key)
            restored = Access(self.root)
            restored.restore_keys()
            self.key_saved = secrets.compare_digest(restored.key(self.provider), key)
            if not self.key_saved:
                raise ValueError("Credential persistence failed")
            return "密钥已保存到这台 Mac，尚未验证服务端认证。请返回聊天通知我开始真实验证。"
        raise ValueError("Unknown action")

    def page(self, message: str = "") -> bytes:
        result = self.process.poll() if self.process is not None else None
        status = ("登录程序已完成" if result == 0 else "登录失败，请通知我重新启动"
                  if result is not None else "等待授权码" if not self.code_sent else "正在登录")
        link = (f'<a href="{html.escape(self.url, quote=True)}" target="_blank" '
                'rel="noreferrer noopener">打开 Claude 官方授权</a>' if self.url
                else "官方登录链接准备中，请稍后刷新。")
        claude_form = f'''<h2>Claude 订阅登录</h2><p>{status}</p><p>{link}</p>
<p>授权完成后，将官方页面显示的一次性授权码粘贴到这里。</p>
<form method="post"><input type="hidden" name="csrf" value="{self.token}">
<input type="hidden" name="action" value="claude">
<label>一次性授权码 <input name="code" type="password" autocomplete="off" required></label>
<button type="submit">提交授权码</button></form>''' if self.process is not None else ""
        label = ("DeepSeek API key（DSF）" if self.provider == "dsh"
                 else "Vercel AI Gateway key（Jev）")
        content = f'''<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Orchestragent 临时配置</title>
<body><h1>Orchestragent 临时配置</h1>
<p>临时加密配置页。20 分钟后关闭。输入不会进入聊天或请求日志，请勿转发链接。</p>
<p>{html.escape(message)}</p>{claude_form}
<h2>{label}</h2><p>{'已保存，尚未验证认证' if self.key_saved else '尚未保存'}</p>
<p>保存只更新本机凭据，不发送模型请求。沿用已授权评测预算，历史预留不清除。</p>
<form method="post"><input type="hidden" name="csrf" value="{self.token}">
<input type="hidden" name="action" value="{self.provider}">
<label>{label} <input name="key" type="password" autocomplete="off" required></label>
<button type="submit">保存到 Mac</button></form><p><a href="">刷新状态</a></p></body></html>'''
        return content.encode()

    def close(self) -> None:
        if self.process is None:
            return
        stop_group(self.process, grace=1)
        if self.reader is not None:
            self.reader.join(timeout=2)
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()


def handler(session: Session) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            self.request.settimeout(5)
            super().setup()

        def log_message(self, format: str, *args: object) -> None:
            pass

        def reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self) -> bool:
            return (time.monotonic() < session.deadline
                    and secrets.compare_digest(self.path, "/" + session.token)
                    and self.headers.get("Host") == urlparse(session.origin).netloc)

        def do_GET(self) -> None:
            if not self.allowed():
                self.reply(404, b"Not found or expired")
                return
            self.reply(200, session.page())

        def do_POST(self) -> None:
            # Mobile browsers may omit Origin or serialize it as null for form POSTs.
            # Host + unguessable path + CSRF are still mandatory. Reject explicit
            # foreign origins and cross-site fetch metadata, including null origins.
            origin = self.headers.get("Origin")
            site = self.headers.get("Sec-Fetch-Site")
            if (not self.allowed() or origin not in {None, "null", session.origin}
                    or site not in {None, "none", "same-origin"}):
                self.reply(403, b"Forbidden")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 12000:
                    raise ValueError("Invalid size")
                self.connection.settimeout(5)
                values = parse_qs(self.rfile.read(size).decode(), max_num_fields=4)
                message = session.submit(values)
            except (ValueError, OSError):
                message = "提交未成功。请检查字段或通知我；不要把 key 发到聊天。"
                self.reply(400, session.page(message))
                return
            self.reply(200, session.page(message))

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--port", type=int, default=64195)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--keys-only", choices=("dsh", "jev"))
    parser.add_argument("--cap-cents", type=int, default=500)
    args = parser.parse_args()
    session = Session(args.data_dir, args.origin, keys_only=args.keys_only,
                      cap_cents=args.cap_cents)
    try:
        with ThreadingHTTPServer(("127.0.0.1", args.port), handler(session)) as server:
            server.timeout = 1
            print(f"Setup page: {session.origin}/{session.token}", flush=True)
            while time.monotonic() < session.deadline:
                server.handle_request()
    finally:
        session.close()


if __name__ == "__main__":
    main()
