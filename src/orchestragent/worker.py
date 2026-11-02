"""Remote CI role for a secondary instance: files in, test verdict out.

The coordinator sends one pinned revision; this process runs it under bwrap with
no network access and returns the report. Code never leaves the primary instance
except as this read-only bundle, and the worker keeps no project state.
"""

import argparse
import hmac
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from orchestragent.platform import sandbox_available, sandbox_command

BOOTSTRAP = (
    "import resource,sys,unittest,os\n"
    "resource.setrlimit(resource.RLIMIT_CPU,(15,15))\n"
    "resource.setrlimit(resource.RLIMIT_FSIZE,(1048576,1048576))\n"
    "sys.path.insert(0,os.getcwd())\n"
    "suite=unittest.TestLoader().discover('.',pattern='test_*.py')\n"
    "count=suite.countTestCases()\n"
    "result=unittest.TextTestRunner(verbosity=2).run(suite)\n"
    "print('WORKER_DONE',count,int(result.wasSuccessful()),flush=True)\n"
    "sys.exit(0 if count and result.wasSuccessful() else 1)\n"
)
MAX_FILES = 40
MAX_BYTES = 512_000
TIMEOUT = 60


def run_bwrap(workdir: Path, python: str = "/usr/bin/python3") -> tuple[int, str]:
    command = sandbox_command(workdir, python, BOOTSTRAP)
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired:
        return -1, "测试在 bwrap 沙箱中超时"
    output = (result.stdout + result.stderr).decode("utf-8", "replace")
    return result.returncode, output[-16000:]


class Handler(BaseHTTPRequestHandler):
    token = ""
    results: dict[str, dict[str, object]] = {}
    lock = threading.Lock()

    def log_message(self, *args: object) -> None:
        return

    def reply(self, status: int, body: object) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def authorised(self) -> bool:
        return hmac.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {self.token}"
        )

    def do_GET(self) -> None:
        if self.path == "/health":
            self.reply(200, {"role": "ci", "bwrap": sandbox_available()})
            return
        self.reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/run" or not self.authorised():
            self.reply(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                raise ValueError
            body = json.loads(self.rfile.read(length))
            task_id = str(body["task_id"])[:64]
            revision = str(body["revision"])
            files = body["files"]
            if (
                not isinstance(files, list)
                or not 1 <= len(files) <= MAX_FILES
                or len(json.dumps(files).encode()) > MAX_BYTES
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError, UnicodeError):
            self.reply(400, {"error": "invalid request"})
            return
        key = f"{task_id}:{revision}"
        with self.lock:
            cached = self.results.get(key)
        if cached is not None:
            self.reply(200, {**cached, "cached": True})
            return
        workdir = Path(tempfile.mkdtemp(prefix="og-ci-"))
        try:
            for item in files:
                path = Path(str(item["path"]))
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError
                target = workdir / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(item["content"]), encoding="utf-8")
            code, report = run_bwrap(workdir)
        except (OSError, ValueError):
            self.reply(400, {"error": "invalid bundle"})
            return
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        marker = [line for line in report.splitlines() if line.startswith("WORKER_DONE ")]
        passed = bool(
            code == 0
            and marker
            and int(marker[-1].split()[2]) == 1
            and int(marker[-1].split()[1]) > 0
        )
        result = {"passed": passed, "report": report, "exit": code, "at": time.time()}
        with self.lock:
            self.results[key] = result
        self.reply(200, result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--token", default=os.environ.get("ORCHESTRA_CI_TOKEN", ""))
    args = parser.parse_args()
    if not args.token:
        raise SystemExit("A CI token is required (--token or ORCHESTRA_CI_TOKEN)")
    if not sandbox_available():
        raise SystemExit("bwrap is required for the CI role")
    Handler.token = args.token
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"ci-worker: http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
