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
import re
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
           ".password-store", ".claude", ".codex", ".gemini", ".git-credentials",
           ".local/share/keyrings", ".local/share/orch", ".local/state", "Library")
# Common toolchain homes and installed-dependency stores that are not on PATH themselves
# (rustup's toolchains, Go's module store, Maven's repository), mounted read-only.
# Environment plumbing only: any language still works, and ORCH_SANDBOX_READ adds more.
# Pure build caches (~/.cache/go-build, ~/.npm) are deliberately absent: tools write them,
# and the sandbox home is a private, writable tmpfs where they are rebuilt.
SHARED = (".local", "Library", ".config")  # prefixes with more than one tool's files
TOOL_HOMES = (".rustup", ".cargo", "go", ".m2", ".gradle", ".nvm", ".bun", ".deno",
              ".pyenv", ".local/share/uv", ".local/share/pnpm",
              ".dotnet", ".sdkman", ".ghcup", ".elan", ".opam", ".rbenv", ".gem")


def _private(path: Path, home: Path) -> bool:
    """The home itself, a credential or key store, or a directory that contains one."""
    try:
        relative = path.relative_to(home)
    except ValueError:
        return _holds_orch_data(path)
    if not relative.parts:
        return True
    text = relative.as_posix()
    return (any(text == p or text.startswith(p + "/") or p.startswith(text + "/")
                for p in PRIVATE) or _holds_orch_data(path))


def _holds_orch_data(path: Path) -> bool:
    from orchestragent.platform import default_data_dir  # keys live in the data dir
    data = default_data_dir().resolve()
    return data == path or data.is_relative_to(path)


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
        if not entry:
            continue
        bin_dir = Path(entry)
        # A bin's install tree is mounted (lib/, share/ of a toolchain), except for a
        # shared prefix whose other contents are not a toolchain: ~/.local/bin mounts just
        # itself, never ~/.local/share; ~/bin never mounts the home.
        parent = bin_dir.parent
        parent_ok = bin_dir.name in {"bin", "sbin"} and parent != home and not (
            parent.is_relative_to(home) and parent.relative_to(home).as_posix() in SHARED)
        add(parent if parent_ok else bin_dir)
        # Tools linked into a system directory (CI runners link /usr/bin/go into
        # /opt/hostedtoolcache/...): mount the install tree the link points into.
        try:
            links = [item for item in bin_dir.iterdir() if item.is_symlink()]
        except OSError:
            continue
        for link in links:
            try:
                target = link.resolve()
            except OSError:
                continue
            if target.parent.name in {"bin", "sbin"}:
                prefix = target.parent.parent
                shared = prefix == home or (prefix.is_relative_to(home) and
                                            prefix.relative_to(home).as_posix() in SHARED)
                add(target.parent if shared else prefix)
    for key, value in environ.items():
        if key.endswith(("_HOME", "_ROOT", "GOPATH", "GOMODCACHE")) and value.startswith("/"):
            add(Path(value))
    for name in TOOL_HOMES:
        add(home / name)
    for extra in environ.get("ORCH_SANDBOX_READ", "").split(os.pathsep):
        if extra:
            add(Path(extra).expanduser())
    return tuple(found)


def git_dir(execution: Path) -> Path | None:
    """The repository's .git a worktree points to: mounted read-only so tools that read
    version-control metadata (build stamping, `git describe`) work in the sandbox."""
    marker = execution / ".git"
    try:
        text = marker.read_text() if marker.is_file() else ""
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    worktree_git = (execution / text.split(":", 1)[1].strip()).resolve()
    common = worktree_git / "commondir"
    try:
        return (worktree_git / common.read_text().strip()).resolve() if common.is_file() \
            else worktree_git
    except OSError:
        return None


def toolchain(execution: Path) -> Toolchain:
    roots = list(toolchain_roots())
    repo_git = git_dir(execution)
    if repo_git is not None and repo_git.is_dir():
        roots.append(repo_git)
    roots += [link.resolve() for link in _links(execution)]  # linked ignored directories
    host_path = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    # The real HOME: mounted toolchain homes resolve as usual, anything else written under
    # it lands on the sandbox's private tmpfs root (Linux) and is discarded.
    passthrough = {key: value for key, value in os.environ.items()
                   if key.endswith(("_HOME", "_ROOT", "GOPATH", "GOMODCACHE"))
                   and value.startswith("/")}
    return Toolchain("any", read_only=tuple(roots), path=tuple(host_path),
                     env={**passthrough, "HOME": str(Path.home())})


# Ignored directories that may hold credentials are never linked into a worktree: test
# output reaches model prompts. Files (.env, keys) are never linked at all.
SECRET_DIR = re.compile(r"(^|/)\.?(secrets?|credentials?|private|keys?|tokens?|certs?|"
                        r"\.env[^/]*|\.aws|\.ssh|\.gnupg)(/|$)", re.IGNORECASE)


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
        if (not name or not (repo / name).is_dir()  # directories, or links to one
                or name.startswith((".git", ".orch", ".data"))
                or SECRET_DIR.search(name) or _holds_orch_data((repo / name).resolve())):
            continue
        source, target = repo / name, worktree / name
        if source.is_dir() and not target.exists() and not target.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source.resolve(), target_is_directory=True)


def excludes(worktree: Path) -> list[str]:
    """`git add -A` pathspecs that keep scratch and linked directories out of a change.

    A link git already ignores needs no pathspec (and naming an ignored path makes
    `git add` fail); one it would stage, e.g. under a `node_modules/` directory-only
    pattern, is excluded explicitly."""
    paths = [SCRATCH, *sorted(str(p.relative_to(worktree)) for p in _links(worktree))]
    return [f":(exclude){path}" for path in paths if not _ignored(worktree, path)]


def _ignored(worktree: Path, path: str) -> bool:
    try:
        return subprocess.run(["git", "-C", str(worktree), "check-ignore", "-q", path],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _links(worktree: Path) -> list[Path]:
    found = []
    for path in worktree.rglob("*"):
        # Relative parts: orch's own worktrees live under the project's .git/orch/.
        inside = path.relative_to(worktree).parts
        if ".git" in inside or SCRATCH in inside:
            continue
        if path.is_symlink() and path.resolve().is_dir() \
                and not path.resolve().is_relative_to(worktree.resolve()):
            found.append(path)
    return found


# --- running ---------------------------------------------------------------------------
def run_command(execution: Path, command: str, timeout: float) -> orch_sandbox.Result:
    execution = execution.resolve()  # TMPDIR and mounts must survive `cd` in the command
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
        listing = sorted(p.name + ("/" if p.is_dir() else "") for p in execution.iterdir()
                         if not p.name.startswith("."))[:40]
        return False, (
            f"The verification command could not run (exit {result.code}); output:\n"
            f"{result.output[-3000:]}\nIt runs with /bin/sh -c from the repository root, "
            f"whose entries are: {' '.join(listing)}. Change into the component's directory "
            "first when its tools and dependencies live there (for example `cd <dir> && ...`).")
    return True, ""
