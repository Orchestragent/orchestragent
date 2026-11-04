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


# Linux seccomp for the toolchain sandbox (orch_sandbox), passed to bwrap by descriptor.
# Compilers and test runners fork, exec and use loopback sockets (vitest, jest and Gradle
# talk to their own workers): bwrap's namespaces isolate mounts and PIDs, and its empty
# network namespace has no route out. The filter denies io_uring and, on x86_64, the x32
# ABI, and kills any other architecture.
_SECCOMP_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}  # AUDIT_ARCH
_IO_URING_SETUP = 425  # the same number on both
_X32_SYSCALL_BIT = 0x40000000


def seccomp_filter(machine: str | None = None) -> bytes:
    """Classic BPF program (struct sock_filter[]) for this host; fails closed elsewhere."""
    machine = machine or platform.machine()
    if machine not in _SECCOMP_ARCH:
        raise RuntimeError(f"No sandbox seccomp filter for {machine}")
    load, jeq, jge, ret = 0x20, 0x15, 0x35, 0x06
    allow, kill, eperm = 0x7FFF0000, 0x80000000, 0x00050000 | 1
    program = [(load, 0, 0, 4), (jeq, 1, 0, _SECCOMP_ARCH[machine]), (ret, 0, 0, kill),
               (load, 0, 0, 0)]
    if machine == "x86_64":
        program += [(jge, 0, 1, _X32_SYSCALL_BIT), (ret, 0, 0, eperm)]
    program += [(jeq, 0, 1, _IO_URING_SETUP), (ret, 0, 0, eperm), (ret, 0, 0, allow)]
    return b"".join(struct.pack("=HBBI", *instruction) for instruction in program)
