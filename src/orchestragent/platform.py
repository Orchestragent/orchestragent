"""Host-specific bits: where the vendor CLIs live and how a test runs isolated."""

import os
import platform
import shutil
import struct
from pathlib import Path


def source_root() -> Path | None:
    """The source checkout this package runs from, or None when installed as a package."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() and (root / "src").is_dir() else None


def default_data_dir() -> Path:
    """Keys, spend ledger and provider state: $ORCHESTRA_DATA_DIR, else the checkout's
    .data (unchanged for source installs), else the per-user application data directory."""
    if os.environ.get("ORCHESTRA_DATA_DIR"):
        return Path(os.environ["ORCHESTRA_DATA_DIR"]).expanduser()
    root = source_root()
    if root is not None:
        return root / ".data"
    if is_macos():
        return Path.home() / "Library" / "Application Support" / "orch"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "orch"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def binary(env: str, candidates: list[str], fallback: str) -> Path:
    """Explicit override first, then well-known install locations."""
    override = os.environ.get(env, "").strip()
    if override:
        return Path(override)
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return Path(found)
        path = Path(candidate)
        if path.is_file():
            return path
    return Path(fallback)


def codex_binary() -> Path:
    return binary(
        "ORCHESTRA_CODEX_BIN",
        ["codex", "/Applications/ChatGPT.app/Contents/Resources/codex"],
        "/Applications/ChatGPT.app/Contents/Resources/codex",
    )


def claude_binary() -> Path:
    return binary(
        "ORCHESTRA_CLAUDE_BIN",
        ["claude", str(Path.home() / ".local/bin/claude"), "/opt/homebrew/bin/claude"],
        "/opt/homebrew/bin/claude",
    )


def cursor_binary() -> Path:
    root = Path(__file__).resolve().parents[2]
    return binary(
        "ORCHESTRA_CURSOR_BIN",
        [
            "cursor-agent",
            str(root / ".tools/cursor-agent/2026.10.19-6bbf2c4/dist-package/cursor-agent"),
        ],
        str(root / ".tools/cursor-agent/2026.10.19-6bbf2c4/dist-package/cursor-agent"),
    )


def kimi_binary() -> Path:
    root = Path(__file__).resolve().parents[2]
    return binary(
        "ORCHESTRA_KIMI_BIN",
        ["kimi", str(root / ".tools/kimi/bin/kimi")],
        str(root / ".tools/kimi/bin/kimi"),
    )


def dsh_binary() -> Path:
    return binary(
        "ORCHESTRA_DSH_BIN",
        ["dsh", "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"],
        "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh",
    )


def sandbox_name() -> str:
    return "sandbox-exec" if is_macos() else "bwrap"


def sandbox_available() -> bool:
    """Whether this host has the OS sandbox every generated test must run in."""
    if is_macos():
        return Path("/usr/bin/sandbox-exec").exists()
    return shutil.which("bwrap") is not None


# Linux seccomp, installed by the sandboxed interpreter itself before any test code runs.
# bwrap already isolates mounts, network and PIDs; the filter makes the remaining
# Seatbelt guarantees explicit: no new processes or programs and no network sockets.
# Threads (clone with CLONE_THREAD) and AF_UNIX socketpairs stay available.
_SECCOMP_ARCH = {
    # machine: (AUDIT_ARCH, socket, clone, denied syscalls)
    # execve/execveat, legacy fork/vfork, io_uring_setup; clone3 -> ENOSYS so libc
    # falls back to clone, whose flags the filter can inspect.
    "x86_64": (0xC000003E, 41, 56, (59, 322, 57, 58, 425)),
    "aarch64": (0xC00000B7, 198, 220, (221, 281, 425)),
}
_CLONE3 = 435
_CLONE_THREAD = 0x10000
_AF_UNIX = 1
_X32_SYSCALL_BIT = 0x40000000


def seccomp_filter(machine: str | None = None) -> bytes:
    """Classic BPF program (struct sock_filter[]) for this host; fails closed elsewhere."""
    machine = machine or platform.machine()
    if machine not in _SECCOMP_ARCH:
        raise RuntimeError(f"No sandbox seccomp filter for {machine}")
    arch, socket, clone, denied = _SECCOMP_ARCH[machine]
    load, jeq, jge, jset, ret = 0x20, 0x15, 0x35, 0x45, 0x06
    allow, kill = 0x7FFF0000, 0x80000000
    eperm, enosys = 0x00050000 | 1, 0x00050000 | 38
    program = [(load, 0, 0, 4), (jeq, 1, 0, arch), (ret, 0, 0, kill), (load, 0, 0, 0)]
    if machine == "x86_64":
        program += [(jge, 0, 1, _X32_SYSCALL_BIT), (ret, 0, 0, eperm)]
    for number in denied:
        program += [(jeq, 0, 1, number), (ret, 0, 0, eperm)]
    program += [(jeq, 0, 1, _CLONE3), (ret, 0, 0, enosys)]
    # The first syscall argument (low 32 bits) is clone flags / socket domain.
    program += [(jeq, 0, 4, clone), (load, 0, 0, 16), (jset, 0, 1, _CLONE_THREAD),
                (ret, 0, 0, allow), (ret, 0, 0, eperm)]
    program += [(jeq, 0, 4, socket), (load, 0, 0, 16), (jeq, 0, 1, _AF_UNIX),
                (ret, 0, 0, allow), (ret, 0, 0, eperm)]
    program.append((ret, 0, 0, allow))
    return b"".join(struct.pack("=HBBI", *instruction) for instruction in program)


def _seccomp_prelude() -> str:
    """Python source that locks the interpreter's process before the test script."""
    return (
        "import ctypes\n"
        f"_orch_filter=ctypes.create_string_buffer({seccomp_filter()!r})\n"
        "class _OrchProgram(ctypes.Structure):\n"
        "    _fields_=[('len',ctypes.c_ushort),('filter',ctypes.c_void_p)]\n"
        "_orch_program=_OrchProgram(len(_orch_filter.raw)//8,"
        "ctypes.addressof(_orch_filter))\n"
        "_orch_libc=ctypes.CDLL(None,use_errno=True)\n"
        # PR_SET_NO_NEW_PRIVS, then PR_SET_SECCOMP with SECCOMP_MODE_FILTER.
        "if (_orch_libc.prctl(38,ctypes.c_ulong(1),ctypes.c_ulong(0),ctypes.c_ulong(0),"
        "ctypes.c_ulong(0))!=0 or _orch_libc.prctl(22,ctypes.c_ulong(2),"
        "ctypes.byref(_orch_program),ctypes.c_ulong(0),ctypes.c_ulong(0))!=0):\n"
        "    raise SystemExit('sandbox seccomp filter could not be installed')\n"
        "del _orch_filter,_OrchProgram,_orch_program,_orch_libc\n"
    )


def sandbox_command(
    workdir: Path, python: str, script: str, profile: str = "",
    read_only: tuple[Path, ...] = (),
) -> list[str]:
    """Return the argv that runs `python -c script` confined to workdir.

    On macOS the caller supplies the audited Seatbelt profile, which already grants
    `read_only`; on Linux the equivalent confinement comes from bwrap mounts,
    namespaces and a seccomp filter. Paths keep their host names in both sandboxes.
    """
    if is_macos():
        return [
            "/usr/bin/sandbox-exec",
            "-p",
            profile or _seatbelt_profile(workdir),
            python,
            "-I",
            "-S",
            "-B",  # no bytecode: a rewritten file (mutation testing) is never stale
            "-c",
            script,
        ]
    if not shutil.which("bwrap"):
        raise RuntimeError("No sandbox backend: install bubblewrap on Linux")
    workdir = workdir.resolve()
    system = [Path(p) for p in ("/usr", "/bin", "/lib", "/lib64") if Path(p).exists()]
    runtime = Path(python).resolve().parents[1]
    mounts = [*read_only]
    if not any(runtime.is_relative_to(root) for root in system):
        mounts.append(runtime)  # an interpreter installed outside /usr (uv, pyenv)
    command = ["bwrap", "--die-with-parent", "--unshare-all", "--new-session"]
    for root in system:
        command += ["--ro-bind", str(root), str(root)]
    command += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    # Ancestors of the bound trees are synthetic empty directories: no host listing.
    for root in mounts:
        command += ["--ro-bind", str(root.resolve()), str(root.resolve())]
    command += [
        "--bind", str(workdir), str(workdir),
        "--chdir", str(workdir),
        "--setenv", "HOME", str(workdir),
        "--setenv", "PATH", "/usr/bin:/bin",
        python, "-I", "-S", "-B", "-c", _seccomp_prelude() + script,
    ]
    return command


def _seatbelt_profile(workdir: Path) -> str:
    return (
        "(version 1)(deny default)(allow process*)(allow file-read*)\n"
        f'(allow file-write* (subpath "{workdir}"))\n'
        "(deny network*)(deny file-read* (subpath \"/Users\"))"
    )
