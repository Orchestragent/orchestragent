import sys
from pathlib import Path

import pytest

from orchestragent.orch_sandbox import Toolchain, run
from orchestragent.platform import sandbox_available

pytestmark = pytest.mark.skipif(not sandbox_available(), reason="no sandbox backend")
SHELL = Toolchain("shell")


def sh(workdir: Path, script: str, timeout: float = 30) -> str:
    result = run(workdir, ["/bin/sh", "-c", script], SHELL, timeout=timeout)
    assert not result.timed_out
    return result.output


def test_toolchains_may_start_processes_and_write_their_workdir(tmp_path: Path) -> None:
    out = sh(tmp_path, "echo hi | tr a-z A-Z; (echo child) & wait; echo ok > made.txt")
    assert "HI" in out and "child" in out
    assert (tmp_path / "made.txt").read_text() == "ok\n"


def test_no_network_route_and_no_writes_outside(tmp_path: Path) -> None:
    probe = ("import socket\n"
             "try:\n"
             "    socket.create_connection(('1.1.1.1', 80), 3); print('NET-OPEN')\n"
             "except OSError as error:\n"
             "    print('NET-CLOSED', type(error).__name__)\n")
    (tmp_path / "probe.py").write_text(probe)
    python = Path(sys.executable).resolve()
    out = run(tmp_path, [str(python), "-I", "probe.py"],
              Toolchain("python", read_only=(python.parents[1],)), timeout=30).output
    assert "NET-CLOSED" in out and "NET-OPEN" not in out
    outside = Path.home() / f"orch-sandbox-escape-{tmp_path.name}"
    sh(tmp_path, f"echo x > {outside} 2>/dev/null; true")
    assert not outside.exists()


def test_user_homes_are_not_readable(tmp_path: Path) -> None:
    probe = Path.home() / f".orch-sandbox-probe-{tmp_path.name}"
    probe.write_text("CONTENT-ONLY-VISIBLE-OUTSIDE")
    try:
        assert "CONTENT-ONLY" not in sh(tmp_path, f"cat {probe} 2>&1; true")
    finally:
        probe.unlink()


def test_timeout_stops_the_whole_process_group(tmp_path: Path) -> None:
    result = run(tmp_path, ["/bin/sh", "-c", "(sleep 30; echo late > late.txt) & sleep 30"],
                 SHELL, timeout=2)
    assert result.timed_out and not result.ok
    assert not (tmp_path / "late.txt").exists()


def test_output_is_bounded(tmp_path: Path) -> None:
    result = run(tmp_path, ["/bin/sh", "-c", "yes x | head -c 200000"], SHELL, timeout=30,
                 output_limit=10000)
    assert result.truncated and len(result.output) <= 10000 and not result.ok


def test_profile_never_allows_outbound_by_local_address(tmp_path: Path) -> None:
    from orchestragent.orch_sandbox import mac_profile

    profile = mac_profile(tmp_path, SHELL)
    assert "(local ip" not in profile.split("network-bind", 1)[0]
    assert '(allow network-outbound (remote ip "localhost:*"))' in profile
