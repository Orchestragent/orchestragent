"""Run any program (a compiler, `go test`, `npm test`, `cargo test`) confined to a workdir.

The Python test runner in live_workspace locks its own interpreter and forbids new
processes. Toolchains need processes, so this executor keeps the other guarantees and
lets processes run:

- Linux: bubblewrap namespaces (mounts, PIDs, IPC, UTS, and an empty network namespace:
  loopback only, no route out) with only the OS, the declared toolchain roots and the
  workdir visible; a seccomp filter, passed to bwrap by file descriptor, denies io_uring.
- macOS: a Seatbelt profile that denies all but loopback networking, writes outside the
  workdir and reading user homes; processes start only from the OS and toolchain roots.

Dependencies are never fetched here: toolchain caches are mounted read-only.
"""

from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from json import dumps
from pathlib import Path

from orchestragent.platform import is_macos, seccomp_filter

OUTPUT_LIMIT = 4 * 1024 * 1024  # bytes of combined output kept on disk before stopping
OS_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/alternatives", "/etc/ssl",
            "/etc/ca-certificates", "/etc/hosts", "/etc/nsswitch.conf", "/etc/host.conf",
            "/etc/passwd", "/etc/group", "/etc/localtime")
MAC_EXEC_ROOTS = ("/usr", "/bin", "/sbin", "/System", "/Library/Developer",
                  "/Applications/Xcode.app", "/opt/homebrew", "/usr/local")


@dataclass(frozen=True)
class Toolchain:
    """What a language needs inside the sandbox: read-only roots, PATH and environment."""

    name: str
    read_only: tuple[Path, ...] = ()
    path: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cpu_seconds: int = 600
    open_files: int = 4096


@dataclass(frozen=True)
class Result:
    code: int
    output: str
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out and not self.truncated


def _environment(workdir: Path, toolchain: Toolchain) -> dict[str, str]:
    path = ":".join([*toolchain.path, "/usr/local/bin", "/usr/bin", "/bin"])
    tmp = workdir / ".orch-tmp"
    env = {"PATH": path, "HOME": str(workdir), "TMPDIR": str(tmp), "LANG": "C.UTF-8",
           "CI": "1", "NO_COLOR": "1"}
    env.update(toolchain.env)
    return env


def linux_command(workdir: Path, argv: list[str], toolchain: Toolchain, env: dict[str, str],
                  seccomp_fd: int) -> list[str]:
    if not shutil.which("bwrap"):
        raise RuntimeError("No sandbox backend: install bubblewrap on Linux")
    command = ["bwrap", "--die-with-parent", "--unshare-all", "--new-session",
               "--hostname", "orch-sandbox", "--seccomp", str(seccomp_fd)]
    for root in OS_ROOTS:
        if Path(root).exists():
            command += ["--ro-bind", root, root]
    command += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    for extra in toolchain.read_only:
        resolved = extra.resolve()
        if resolved.exists():
            command += ["--ro-bind", str(resolved), str(resolved)]
    command += ["--bind", str(workdir), str(workdir), "--chdir", str(workdir), "--clearenv"]
    for key, value in env.items():
        command += ["--setenv", key, value]
    return command + argv


def mac_profile(workdir: Path, toolchain: Toolchain) -> str:
    roots = [*MAC_EXEC_ROOTS, *(str(p.resolve()) for p in toolchain.read_only)]
    excluded = ("/Users", "/Volumes", "/Network", "/private/tmp", "/private/var/tmp",
                "/private/var/folders")
    keep_out = " ".join(f"(require-not (subpath {dumps(p)}))" for p in excluded)
    readable = " ".join(f"(subpath {dumps(str(p.resolve()))})" for p in toolchain.read_only)
    return (
        "(version 1)(deny default)(deny network*)"
        # Loopback only. Outbound is filtered on the remote end: a `local ip` filter also
        # matches outbound connects (CI macos-14 reached 1.1.1.1 through it).
        '(allow network-outbound (remote ip "localhost:*"))'
        '(allow network-bind network-inbound (local ip "localhost:*"))'
        '(allow network-outbound (remote unix-socket))'
        "(allow process-fork)(allow signal (target same-sandbox))"
        "(allow process-info*)(allow sysctl-read)(allow mach-lookup)(allow ipc-posix-shm)"
        f"(allow process-exec (subpath {dumps(str(workdir))}) "
        + " ".join(f"(subpath {dumps(root)})" for root in roots) + ")"
        + f"(allow file-read* (require-all {keep_out}))"
        + (f"(allow file-read* {readable})" if readable else "")
        + f"(allow file-read* file-write* (subpath {dumps(str(workdir))}))"
        + '(allow file-write-data (literal "/dev/null"))'
        + "(allow file-read-metadata)"
    )


def run(workdir: Path, argv: list[str], toolchain: Toolchain, *, timeout: float,
        output_limit: int = OUTPUT_LIMIT) -> Result:
    """Run argv in workdir under the toolchain policy; never raises for a failing program."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    workdir = workdir.resolve()
    env = _environment(workdir, toolchain)
    Path(env["TMPDIR"]).mkdir(exist_ok=True)
    # Limits are set by the shell's own ulimit, then exec: orch runs sandboxes from many
    # threads, and subprocess's preexec_fn (Python code between fork and exec) is unsafe
    # there. They are inherited by bwrap/sandbox-exec and every process below them.
    blocks = output_limit * 64 // 512  # ulimit -f counts 512-byte blocks
    limits = (f"ulimit -t {toolchain.cpu_seconds}; ulimit -f {blocks}; "
              f"ulimit -n {toolchain.open_files} 2>/dev/null; exec \"$@\"")

    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as program:
        pass_fds: tuple[int, ...] = ()
        if is_macos():
            command = ["/usr/bin/sandbox-exec", "-p", mac_profile(workdir, toolchain), *argv]
        else:
            program.write(seccomp_filter())
            program.flush()
            program.seek(0)
            fd = program.fileno()
            os.set_inheritable(fd, True)
            pass_fds = (fd,)
            command = linux_command(workdir, argv, toolchain, env, fd)
        command = ["/bin/sh", "-c", limits, "orch-limits", *command]
        process = subprocess.Popen(
            command, cwd=workdir, env=env, stdin=subprocess.DEVNULL, stdout=output,
            stderr=subprocess.STDOUT, pass_fds=pass_fds, start_new_session=True)
        try:
            code, timed_out = process.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            code, timed_out = -1, True
        finally:  # the whole process group: test runners leave workers behind
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()
        size = output.tell()
        output.seek(max(0, size - output_limit))
        text = output.read(output_limit).decode("utf-8", errors="replace")
    truncated = size > output_limit
    if timed_out:
        text += f"\n[orch] stopped after {timeout:g} s"
    return Result(code, text, timed_out, truncated)
