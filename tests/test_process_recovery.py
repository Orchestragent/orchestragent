import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
Launch = Callable[[], subprocess.Popen[bytes]]


@pytest.fixture
def restart_environment(tmp_path: Path) -> dict[str, str]:
    if shutil.which("flock") is None:
        pytest.skip("og-restart runs on the Linux deployment host")
    commands = {
        "curl": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CURL_MARKER"\n'
                'case "$*" in *"/health"*) echo healthy;; '
                '*) printf %s "$DRAIN_CODE";; esac\n',
        "sudo": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$RESTART_MARKER"\n'
                'exit "${RESTART_EXIT:-0}"\n',
        "sleep": "#!/bin/sh\nexit 0\n",
    }
    for name, content in commands.items():
        path = tmp_path / name
        path.write_text(content)
        path.chmod(0o700)
    return {"PATH": f"{tmp_path}:{os.defpath}", "WAIT": "0", "TMPDIR": str(tmp_path),
            "RESTART_MARKER": str(tmp_path / "restarted"),
            "CURL_MARKER": str(tmp_path / "requests")}


@pytest.mark.parametrize("code,expected", [("200", True), ("409", False),
                                           ("404", False), ("500", False)])
def test_restart_script_fails_closed_unless_gate_is_acquired(
    restart_environment: dict[str, str], code: str, expected: bool,
) -> None:
    result = subprocess.run(["bash", str(ROOT / "scripts/safe_restart.sh")],
                            env={**restart_environment, "DRAIN_CODE": code},
                            capture_output=True, timeout=5)
    marker = Path(restart_environment["RESTART_MARKER"])
    assert (result.returncode == 0, marker.exists()) == (expected, expected)
    if expected:
        assert marker.read_text() == "systemctl restart orchestragent\n"


def test_failed_restart_releases_admission_gate(restart_environment: dict[str, str]) -> None:
    result = subprocess.run(["bash", str(ROOT / "scripts/safe_restart.sh")],
                            env={**restart_environment, "DRAIN_CODE": "200", "RESTART_EXIT": "1"},
                            capture_output=True, timeout=5)
    assert result.returncode != 0
    assert '{"enabled":false}' in Path(restart_environment["CURL_MARKER"]).read_text()


def call(port: int, method: str, path: str, body: object = None) -> dict[str, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(
            method,
            path,
            json.dumps(body) if body is not None else None,
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        data: dict[str, object] = json.loads(response.read())
        if response.status >= 400:
            raise AssertionError(f"Unexpected HTTP {response.status}")
        return data
    finally:
        connection.close()


def until(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.05)
    raise AssertionError("Local Mock service did not reach expected state")


@pytest.fixture
def service(tmp_path: Path) -> Iterator[tuple[int, Launch]]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    processes: list[subprocess.Popen[bytes]] = []

    def launch() -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "orchestragent.server",
                "--port",
                str(port),
                "--data-dir",
                str(tmp_path / "data"),
            ],
            cwd=ROOT,
            env={"PATH": os.defpath, "PYTHONPATH": str(ROOT / "src")},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(process)
        return process

    try:
        yield port, launch
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


def test_process_kill_and_restart_preserves_approval(service: tuple[int, Launch]) -> None:
    port, launch = service
    first = launch()
    until(lambda: call(port, "GET", "/health")["mode"] == "mock")
    request = {
        "request_id": "restart",
        "prompt": "Recover after process kill",
        "scenario": "approval",
    }
    task = call(port, "POST", "/tasks", request)
    path = f"/tasks/{task['id']}"
    until(lambda: call(port, "GET", path)["status"] == "waiting_approval")
    before = call(port, "GET", path)
    first.kill()
    first.wait(timeout=5)
    launch()
    until(lambda: call(port, "GET", "/health")["mode"] == "mock")
    duplicate = call(port, "POST", "/tasks", request)
    approval = duplicate["approval"]
    assert isinstance(approval, dict)
    assert (duplicate["id"], approval, duplicate["status"]) == (
        before["id"],
        before["approval"],
        "waiting_approval",
    )
    call(
        port,
        "POST",
        f"{path}/approvals/{approval['id']}",
        {
            "decision": "approve",
            "operation_digest": approval["operation_digest"],
        },
    )
    until(lambda: call(port, "GET", path)["status"] == "completed")
    assert call(port, "GET", path)["cost_usd"] == 0


def test_second_server_cannot_recover_first_servers_tasks(service: tuple[int, Launch]) -> None:
    port, launch = service
    first = launch()
    until(lambda: call(port, "GET", "/health")["mode"] == "mock")
    second = launch()
    code = second.wait(timeout=5)
    assert (code != 0, first.poll(), call(port, "GET", "/health")["mode"]) == (True, None, "mock")
