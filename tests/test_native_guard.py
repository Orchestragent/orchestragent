import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from orchestragent import native_guard

CHILD = r"""
import os, pathlib, signal, subprocess, sys, time
root = pathlib.Path(sys.argv[1])
def stop(*args):
    (root / "native-stopped").touch()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
grandchild = subprocess.Popen([sys.executable, "-c",
    "import pathlib,signal,time; "
    "signal.signal(signal.SIGTERM, lambda *a: (pathlib.Path(" +
    repr(str(root / "grandchild-stopped")) + ").touch(),exit(0))); "
    "pathlib.Path(" + repr(str(root / "ready")) + ").touch(); time.sleep(30)"])
(root / "native-pid").write_text(str(os.getpid()))
time.sleep(30)
"""


@pytest.fixture
def child(tmp_path: Path) -> Path:
    path = tmp_path / "child.py"
    path.write_text(CHILD)
    return path


@pytest.fixture
def parent(tmp_path: Path, child: Path) -> Iterator[subprocess.Popen[bytes]]:
    # Exercise NativeClients' actual guard launch under a disposable controller.
    script = tmp_path / "parent.py"
    script.write_text(
        "import pathlib,sys,threading,time\n"
        f"sys.path.insert(0,{str(Path(native_guard.__file__).resolve().parents[1])!r})\n"
        "from orchestragent.native import NativeClients\n"
        f"root=pathlib.Path({str(tmp_path)!r})\n"
        "clients=NativeClients(root / 'runtime')\n"
        f"command=[{sys.executable!r},{str(child)!r},str(root)]\n"
        "worker=threading.Thread(target=lambda:clients._run(command,root,'',0,lambda:False))\n"
        "worker.start()\n"
        "while not clients._processes: time.sleep(0.01)\n"
        "with clients._lock:\n"
        "    (root/'guard-pid').write_text(str(next(iter(clients._processes)).pid))\n"
        "worker.join()\n"
    )
    process = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        guard_file = tmp_path / "guard-pid"
        if guard_file.exists():
            try:
                os.kill(int(guard_file.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("action", ["kill_parent", "stop_guard"])
def test_guard_stops_native_and_grandchild_after_parent_death_or_shutdown(
    parent: subprocess.Popen[bytes],
    tmp_path: Path,
    action: str,
) -> None:
    deadline = time.monotonic() + 4
    while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (tmp_path / "ready").exists()
    if action == "kill_parent":
        parent.kill()
        parent.wait(timeout=3)
    else:
        os.kill(int((tmp_path / "guard-pid").read_text()), signal.SIGTERM)
    deadline = time.monotonic() + 3
    while not (tmp_path / "grandchild-stopped").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ((tmp_path / "native-stopped").exists(), (tmp_path / "grandchild-stopped").exists()) == (
        True,
        True,
    )


def test_guard_owns_timeout_even_without_controller_monitor(child: Path, tmp_path: Path) -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-S",
            str(Path(native_guard.__file__)),
            "--parent",
            str(os.getpid()),
            "--timeout",
            "0.6",
            "--",
            sys.executable,
            str(child),
            str(tmp_path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    code = process.wait(timeout=3)
    assert (
        code,
        (tmp_path / "native-stopped").exists(),
        (tmp_path / "grandchild-stopped").exists(),
    ) == (124, True, True)


def test_guard_escalates_for_native_process_ignoring_term(tmp_path: Path) -> None:
    child = tmp_path / "stubborn.py"
    child.write_text(
        "import os,pathlib,signal,time\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        f"pathlib.Path({str(tmp_path / 'stubborn-pid')!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-S",
            str(Path(native_guard.__file__)),
            "--parent",
            str(os.getpid()),
            "--timeout",
            "0.6",
            "--",
            sys.executable,
            str(child),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert process.wait(timeout=3) == 124
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "stubborn-pid").read_text()), 0)


@pytest.mark.parametrize("budget", ["-1", "nan", "inf", "-inf"])
def test_guard_rejects_invalid_deadline_without_launching(
    tmp_path: Path, budget: str,
) -> None:
    marker = tmp_path / "launched"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(Path(native_guard.__file__)),
         "--parent", str(os.getpid()), f"--timeout={budget}", "--",
         sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
        capture_output=True, timeout=3,
    )
    assert (result.returncode, marker.exists()) == (2, False)
