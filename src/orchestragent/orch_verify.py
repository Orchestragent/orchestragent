"""Model-declared verification for projects in any language, or several at once.

orch does not decide which language a task is written in, where its tests live or how
they run. The planner writes stubs in whatever languages the project uses; each test
designer returns its frozen test files together with the shell command that runs exactly
those tests. orch keeps only language-neutral gates around that command:

- it runs offline in the toolchain sandbox (orch_sandbox), from the repository root;
- the command and its test files are frozen with the plan;
- it must fail on the stubs (red) and pass on the implementation (green): exit codes.

A check's test id is `cmd:` + canonical JSON {command, files}, so the id itself carries
everything needed to run it again, at any revision, in any worktree.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestragent import orch_sandbox
from orchestragent.orch_sandbox import Toolchain

PREFIX = "cmd:"
REPORT_LIMIT = 16000
MIN_TIMEOUT = 300.0  # commands compile and start runners; the per-test default is too short
NOT_RUNNABLE = (126, 127)  # the shell could not run the command at all
# Pathspecs for `git add -A`: the sandbox scratch tree and linked ignored directories are
# never part of a change (their links are added per worktree, see link_ignored).
SCRATCH = ".orch-tmp"
SANDBOX_NOTE = (
    "Verification runs offline in a sandbox from the repository root with /bin/sh -c. "
    "Installed toolchains are available read-only; directories the repository ignores "
    "(installed dependencies such as node_modules, .venv or vendor) are linked read-only. "
    "$TMPDIR is writable scratch: send build outputs and caches there (for example a "
    "target or cache directory under $TMPDIR). Nothing can be downloaded."
)


@dataclass(frozen=True)
class Check:
    """One frozen verification: a shell command and the test files it runs."""

    command: str
    files: tuple[str, ...]

    @property
    def id(self) -> str:
        return PREFIX + json.dumps({"command": self.command, "files": list(self.files)},
                                   sort_keys=True, separators=(",", ":"))


def parse(test_id: str) -> Check | None:
    if not test_id.startswith(PREFIX):
        return None
    try:
        value = json.loads(test_id[len(PREFIX):])
        return Check(str(value["command"]), tuple(str(f) for f in value["files"]))
    except (ValueError, KeyError, TypeError):
        return None


def is_check(test_id: str) -> bool:
    return parse(test_id) is not None


def files_of(test_ids: list[str]) -> list[str]:
    return sorted({f for test in test_ids if (c := parse(test)) is not None for f in c.files})


# --- the sandbox environment, discovered rather than tabulated -------------------------
SYSTEM = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/proc", "/dev", "/sys")
PRIVATE = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config", ".netrc",
           ".password-store", ".claude", ".codex", ".gemini", ".git-credentials")
# Common toolchain homes that are not on PATH themselves (rustup's toolchains, Go's module
# cache, Maven/Gradle caches). Environment plumbing only: any language still works, and
# ORCH_SANDBOX_READ adds more.
TOOL_HOMES = (".rustup", ".cargo", "go", ".m2", ".gradle", ".nvm", ".bun", ".deno",
              ".pyenv", ".local/share/uv", ".local/share/pnpm", ".npm", ".cache/go-build",
              ".dotnet", ".sdkman", ".ghcup", ".elan", ".opam", ".rbenv", ".gem")


def _private(path: Path, home: Path) -> bool:
    try:
        relative = path.relative_to(home)
    except ValueError:
        return False
    return not relative.parts or relative.parts[0] in PRIVATE


def toolchain_roots(environ: dict[str, str] | None = None) -> tuple[Path, ...]:
    """Read-only roots for the sandbox: PATH install trees, *_HOME/*_ROOT directories,
    common toolchain homes and ORCH_SANDBOX_READ; never the home itself or private dirs."""
    environ = dict(os.environ if environ is None else environ)
    home = Path(environ.get("HOME") or Path.home()).resolve()
    found: list[Path] = []

    def add(candidate: Path) -> None:
        try:
            path = candidate.resolve()
        except OSError:
            return
        if (not path.is_dir() or path == home or path == Path("/")
                or _private(path, home) or any(str(path).startswith(s) for s in SYSTEM)):
            return
        if not any(path == other or path.is_relative_to(other) for other in found):
            found[:] = [other for other in found if not other.is_relative_to(path)]
            found.append(path)

    for entry in environ.get("PATH", "").split(os.pathsep):
        if entry:
            bin_dir = Path(entry)
            add(bin_dir.parent if bin_dir.name in {"bin", "sbin"} else bin_dir)
    for key, value in environ.items():
        if key.endswith(("_HOME", "_ROOT", "GOPATH", "GOMODCACHE")) and value.startswith("/"):
            add(Path(value))
    for name in TOOL_HOMES:
        add(home / name)
    for extra in environ.get("ORCH_SANDBOX_READ", "").split(os.pathsep):
        if extra:
            add(Path(extra).expanduser())
    return tuple(found)


def toolchain(execution: Path) -> Toolchain:
    roots = list(toolchain_roots())
    roots += [link.resolve() for link in _links(execution)]  # linked ignored directories
    host_path = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    # The real HOME: mounted toolchain homes resolve as usual, anything else written under
    # it lands on the sandbox's private tmpfs root (Linux) and is discarded.
    passthrough = {key: value for key, value in os.environ.items()
                   if key.endswith(("_HOME", "_ROOT", "GOPATH", "GOMODCACHE"))
                   and value.startswith("/")}
    return Toolchain("any", read_only=tuple(roots), path=tuple(host_path),
                     env={**passthrough, "HOME": str(Path.home())})


def link_ignored(repo: Path, worktree: Path) -> None:
    """Link the repository's ignored, untracked directories (installed dependencies) into a
    fresh worktree; the sandbox mounts their targets read-only."""
    if worktree.resolve() == repo.resolve():
        return
    try:
        listed = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--others", "--ignored", "--exclude-standard",
             "--directory", "--no-empty-directory"],
            capture_output=True, text=True, timeout=60, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for line in listed.splitlines():
        name = line.rstrip("/")
        if not line.endswith("/") or not name or name.startswith((".git", ".orch", ".data")):
            continue
        source, target = repo / name, worktree / name
        if source.is_dir() and not target.exists() and not target.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source.resolve(), target_is_directory=True)


def excludes(worktree: Path) -> list[str]:
    """`git add -A` pathspecs that keep scratch and linked directories out of a change."""
    linked = sorted(str(p.relative_to(worktree)) for p in _links(worktree))
    return [f":(exclude){SCRATCH}", *(f":(exclude){path}" for path in linked)]


def _links(worktree: Path) -> list[Path]:
    found = []
    for path in worktree.rglob("*"):
        if ".git" in path.parts or SCRATCH in path.parts:
            continue
        if path.is_symlink() and path.resolve().is_dir() \
                and not path.resolve().is_relative_to(worktree.resolve()):
            found.append(path)
    return found


# --- running ---------------------------------------------------------------------------
def run_command(execution: Path, command: str, timeout: float) -> orch_sandbox.Result:
    scratch = execution / SCRATCH
    (scratch / "tmp").mkdir(parents=True, exist_ok=True)
    base = toolchain(execution)
    tools = Toolchain(base.name, base.read_only, base.path,
                      {**base.env, "TMPDIR": str(scratch / "tmp")},
                      cpu_seconds=max(base.cpu_seconds, int(timeout)))
    shell = shutil.which("sh") or "/bin/sh"
    return orch_sandbox.run(execution, [shell, "-c", command], tools, timeout=timeout)


def run(execution: Path, scratch: Path, revision: str, names: list[str] | None, /,
        *, timeout: float = 25.0) -> tuple[bool, str]:
    """Runner protocol: every named check's command must exit 0."""
    checks = [c for c in (parse(n) for n in names or []) if c is not None]
    if not checks:
        return False, "No verification command to run."
    limit = max(timeout, MIN_TIMEOUT)
    passed, lines, tails = True, [], []
    for check in dict.fromkeys(checks):
        result = run_command(execution, check.command, limit)
        ok = result.ok
        passed &= ok
        status = "PASS" if ok else ("TIMEOUT" if result.timed_out else f"FAIL (exit {result.code})")
        lines.append(f"{status}: {check.id}")
        if not ok:
            tails.append(f"--- {check.command}\n{result.output[-6000:]}")
    report = "\n".join(lines) + ("\n\n" + "\n\n".join(tails) if tails else "")
    audit = (f"\n\nissuer=controller\nmode=toolchain-sandbox\nrevision={revision}\n"
             f"checks={len(dict.fromkeys(checks))}\nwall_limit_s={limit:g}\n")
    return passed, report[-REPORT_LIMIT:] + audit


def failed(report: str, candidates: list[str]) -> list[str]:
    """Candidate check ids that failed in a run() report, in report order."""
    out = []
    for line in report.splitlines():
        head, _, rest = line.partition(": ")
        if head.startswith(("FAIL", "TIMEOUT")) and rest in candidates and rest not in out:
            out.append(rest)
    return out


def red(execution: Path, check: Check, timeout: float = MIN_TIMEOUT) -> tuple[bool, str]:
    """A frozen check on the stubs: (usable, problem). It must run and fail, not pass."""
    result = run_command(execution, check.command, timeout)
    if result.code == 0 and not result.timed_out:
        return False, ("These tests pass against the stubs, so they prove nothing: test the "
                       "behaviour the requirements state.")
    if result.code in NOT_RUNNABLE:
        return False, ("The verification command could not run (exit "
                       f"{result.code}); output:\n{result.output[-3000:]}")
    return True, ""


# --- which pipeline ------------------------------------------------------------------
PYTHON_PROJECT = ("pyproject.toml", "setup.py", "setup.cfg")
OTHER_BUILDS = ("package.json", "go.mod", "Cargo.toml", "pom.xml", "build.gradle",
                "build.gradle.kts", "CMakeLists.txt", "Gemfile", "composer.json", "mix.exs",
                "pubspec.yaml", "Package.swift", "deno.json")


def engine(repo: Path, tracked: list[str] | None = None) -> str:
    """"python" for a pure Python project (the tuned unittest/pytest pipeline), otherwise
    "generic". Routing only: generic plans may use any language; ORCH_ENGINE overrides."""
    forced = os.environ.get("ORCH_ENGINE", "").strip().lower()
    if forced in {"python", "generic"}:
        return forced
    if tracked is None:
        try:
            tracked = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True,
                                     text=True, timeout=60, check=True).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            tracked = []
    names = {Path(path).name for path in tracked}
    other = names.intersection(OTHER_BUILDS) or any(
        name.endswith((".csproj", ".sln")) for name in names)
    python = any(path.endswith(".py") for path in tracked) or any(
        (repo / marker).exists() for marker in PYTHON_PROJECT)
    return "python" if python and not other else "generic"
