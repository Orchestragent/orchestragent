"""Probe which endpoint and body the official DSH headless CLI sends, without spending."""

import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from orchestragent.dsh import DshClient

CLI = "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
SEEN: list[str] = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            parsed = json.loads(body)
            keys = sorted(parsed)[:8] if isinstance(parsed, dict) else []
        except ValueError:
            keys = []
        SEEN.append(f"{self.path} model={keys}")
        payload = json.dumps(
            {
                "id": "msg_probe",
                "type": "message",
                "role": "assistant",
                "model": "probe",
                "content": [
                    {"type": "text", "text": '{"reply": "ok", "action": "none", "task_prompt": ""}'}
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/anthropic"
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        env = {"HOME": str(home), "DSH_HOME": str(home), "PATH": os.defpath}
        default = subprocess.run(
            [CLI, "headless", "--dump-default-config"], capture_output=True, env=env, timeout=30
        ).stdout
        candidates = ["deepseek-official"]
        patch = DshClient._patch(default, "deepseek-flash", base)
        patch_path = Path(tmp) / "patch.json"
        patch_path.write_text(json.dumps(patch))
        env["DEEPSEEK_API_KEY"] = "probe-not-a-real-key"
        runs = []
        for provider in candidates:
            rows = [dict(row) for row in patch]
            for row in rows:
                if row.get("id") == "agent-default-model":
                    row["config"] = {"provider": provider, "model": "deepseek-flash"}
            patch_path.write_text(json.dumps(rows))
            SEEN.clear()
            run = subprocess.run(
                [CLI, "headless", "--patch", str(patch_path), "--json", "-"],
                input=b"reply with JSON",
                capture_output=True,
                env=env,
                timeout=120,
            )
            tail = run.stdout[-200:].decode("utf-8", "replace")
            runs.append((provider, run.returncode, list(SEEN), tail))
        for provider, code, seen, tail in runs:
            print(provider, "exit", code, "requests", seen, "tail", tail.replace("\n", " ")[:160])
    server.shutdown()
    print("exit", run.returncode)
    print("requests", SEEN)
    print("stderr_full", run.stderr.decode("utf-8", "replace")[-3000:])
    print("stdout", run.stdout[-600:].decode("utf-8", "replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
