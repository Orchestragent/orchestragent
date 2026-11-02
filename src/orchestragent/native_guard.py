"""One-call native supervisor; exits after parent death, cancellation, or deadline."""

import argparse
import math
import os
import signal
import subprocess
import time
from types import FrameType


def stop_group(process: subprocess.Popen[bytes], grace: float = 0.3) -> None:
    """Terminate and reap an owned group, including descendants after leader exit."""
    deadline = time.monotonic() + grace
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=2)
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # Allow descendants to handle TERM even if their group leader exited first.
    time.sleep(max(0, deadline - time.monotonic()))
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def supervise(parent_pid: int, timeout: float, command: list[str]) -> int:
    stopped = False

    def stop(signum: int, frame: FrameType | None) -> None:
        nonlocal stopped
        stopped = True

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    if os.getppid() != parent_pid or stopped:
        return 125
    deadline = time.monotonic() + timeout if timeout else None
    try:
        process = subprocess.Popen(command, start_new_session=True)
    except OSError:
        return 127
    try:
        while process.poll() is None:
            if stopped or os.getppid() != parent_pid:
                return 125
            if deadline is not None and time.monotonic() >= deadline:
                return 124
            time.sleep(0.05)
        return process.wait()
    finally:
        stop_group(process)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    # Zero disables the execution deadline; parent death and signals still stop children.
    if args.parent < 1 or not math.isfinite(args.timeout) or args.timeout < 0 or not command:
        raise SystemExit(2)
    raise SystemExit(supervise(args.parent, args.timeout, command))


if __name__ == "__main__":
    main()
