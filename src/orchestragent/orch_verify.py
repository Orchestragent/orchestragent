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

import functools
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
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
# Commands a project's build or tests commonly start with. Which of them exist here is a
# fact about this machine that the models cannot guess (Opus wrote `python` three times on
# a host with only python3, 2026-11-03); the inventory states it once, in every prompt.
COMMON_TOOLS = ("python", "python3", "pytest", "pip", "uv", "node", "npm", "npx", "pnpm", "yarn",
                "bun", "deno", "tsc", "go", "cargo", "rustc", "java", "javac", "mvn", "gradle",
                "kotlinc", "dotnet", "ruby", "bundle", "php", "composer", "perl", "swift",
                "zig", "gcc", "g++", "clang", "make", "cmake", "ninja", "bash", "sh")


def sandbox_path() -> tuple[str, ...]:
    """The PATH a sandboxed command sees: the host's, then the bin directories of the common
    toolchain homes that exist. orch started by systemd, cron or a GUI gets a bare PATH
    without ~/.cargo/bin or ~/go/bin (Meridian's first run: every Rust check exited 127)."""
    host_path = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    home = Path.home()
    for name in TOOL_HOMES:
        extra = home / name / "bin"
        if extra.is_dir() and str(extra) not in host_path:
            host_path.append(str(extra))
    return tuple(host_path)


@functools.lru_cache(maxsize=1)
def tool_inventory() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(present, absent) among COMMON_TOOLS on the PATH the sandbox will see."""
    path = os.pathsep.join(sandbox_path())
    present = tuple(name for name in COMMON_TOOLS if shutil.which(name, path=path or None))
    return present, tuple(name for name in COMMON_TOOLS if name not in present)


def sandbox_note() -> str:
    """SANDBOX_NOTE plus the tools actually installed here."""
    present, absent = tool_inventory()
    missing = [name for name in ("python", "node", "go", "cargo", "java") if name in absent]
    return (SANDBOX_NOTE + " Commands available here: " + ", ".join(present) + "."
            + (" Not installed: " + ", ".join(missing) + "." if missing else ""))


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


OS_PREFIXES = {"/", "/usr", "/usr/local", "/opt"}
# Configuration directories under /etc that toolchains link to and that never hold secrets
# worth reading: anything matching this is not mounted even when a toolchain links to it.
ETC_SECRET = re.compile(r"^/etc/(ssh|sudoers|shadow|gshadow|ssl/private|security|pki|"
                        r"krb5|ipsec|wireguard|NetworkManager)", re.IGNORECASE)


@functools.lru_cache(maxsize=256)
def _etc_links(prefix: Path, depth: int = 3) -> frozenset[Path]:
    """/etc directories a toolchain's install tree links its own configuration into.

    Ubuntu's JDK keeps conf/ as links into /etc/java-21-openjdk: without them javac fails
    to start in the sandbox (found building polyglot benchmark Meridian, 2026-11-03)."""
    found: set[Path] = set()
    if str(prefix) in OS_PREFIXES:  # the OS itself, not one toolchain's install tree
        return frozenset()
    base = len(prefix.parts)
    for folder, dirs, files in os.walk(prefix):
        if len(Path(folder).parts) - base >= depth:
            dirs[:] = []
        for name in [*dirs, *files]:
            item = Path(folder) / name
            if not item.is_symlink():
                continue
            try:
                target = item.resolve()
            except OSError:
                continue
            if target.parts[:2] == ("/", "etc") and len(target.parts) > 2:
                top = Path("/etc") / target.parts[2]
                if not ETC_SECRET.match(str(top)) and top.is_dir():
                    found.add(top)
    return frozenset(found)


def toolchain_roots(environ: dict[str, str] | None = None) -> tuple[Path, ...]:
    """Read-only roots for the sandbox: PATH install trees, *_HOME/*_ROOT directories,
    common toolchain homes and ORCH_SANDBOX_READ; never the home itself or private dirs.

    Resolving every link on PATH takes ~0.25 s; the answer depends only on the
    environment, so it is computed once per distinct environment."""
    environ = dict(os.environ if environ is None else environ)
    return _roots(tuple(sorted(environ.items())))


@functools.lru_cache(maxsize=8)
def _roots(items: tuple[tuple[str, str], ...]) -> tuple[Path, ...]:
    environ = dict(items)
    home = Path(environ.get("HOME") or Path.home()).resolve()
    found: list[Path] = []
    scanned: set[Path] = set()

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
                if not shared and prefix not in scanned:
                    scanned.add(prefix)
                    for etc in _etc_links(prefix):
                        if etc not in found:
                            found.append(etc)
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
    roots += [mount_source(link) for link in _links(execution)]  # linked ignored dirs
    host_path = list(sandbox_path())
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


# An ignored directory up to this size is copied into a worktree instead of linked: build
# outputs (build/, bin/, a local object directory) are small and a check must be able to
# write them. Linked directories are mounted read-only, so a Makefile writing build/*.o
# failed with "Read-only file system" whenever the repository held a local build (single
# Opus on Tessera, 2026-11-04). Installed dependencies (node_modules, a virtualenv) are
# usually far larger and stay shared, read-only links.
COPY_IGNORED_LIMIT = 32 * 1024 * 1024  # bytes
# A larger ignored directory becomes a symlink farm: its directories are real (so a tool
# can create files in them: vite bundles a vitest.config.ts into node_modules/.vite-temp,
# Meridian 2026-11-04), its files are links to the originals, which the sandbox mounts
# read-only, so nothing of the repository's own copy can change. FARM_MARK names the
# source; a directory with more entries than FARM_LIMIT stays a plain read-only link.
FARM_MARK = ".orch-farm"
FARM_LIMIT = 200_000


def _size_up_to(path: Path, limit: int) -> int:
    """Total file size under `path`, counting stops once it exceeds `limit`."""
    total = 0
    for root, _, names in os.walk(path, followlinks=False):
        for name in names:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                continue
            if total > limit:
                return total
    return total


def link_ignored(repo: Path, worktree: Path) -> None:
    """Bring the repository's ignored, untracked directories into a fresh worktree: small
    ones (build outputs) as private writable copies, large ones (installed dependencies) as
    links whose targets the sandbox mounts read-only."""
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
            real = source.resolve()
            if not source.is_symlink() and _size_up_to(real, COPY_IGNORED_LIMIT) \
                    <= COPY_IGNORED_LIMIT:
                try:
                    shutil.copytree(real, target, symlinks=True)
                    continue
                except (OSError, shutil.Error):
                    shutil.rmtree(target, ignore_errors=True)
            if not _farm(real, target):
                target.symlink_to(real, target_is_directory=True)


def _farm(source: Path, target: Path) -> bool:
    """Mirror `source` under `target`: real directories, links to the original files (and
    the same targets for links). False, leaving nothing behind, when it is too large."""
    entries = 0
    try:
        for root, dirs, files in os.walk(source, followlinks=False):
            relative = Path(root).relative_to(source)
            out = target / relative
            out.mkdir(parents=True, exist_ok=True)
            for name in [*files, *[d for d in dirs if (Path(root) / d).is_symlink()]]:
                entries += 1
                if entries > FARM_LIMIT:
                    raise OverflowError
                original = Path(root) / name
                link = out / name
                if original.is_symlink():
                    link.symlink_to(os.readlink(original))  # relative links stay inside
                else:
                    link.symlink_to(original)
            dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        (target / FARM_MARK).write_text(str(source))
        return True
    except (OSError, OverflowError):
        shutil.rmtree(target, ignore_errors=True)
        return False


def farm_source(path: Path) -> Path | None:
    """The directory a symlink farm mirrors, or None when `path` is not a farm."""
    mark = path / FARM_MARK
    if path.is_symlink() or not mark.is_file():
        return None
    return Path(mark.read_text().strip())


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
    """Linked and farmed ignored directories in a worktree (never descending into either:
    a farm holds one link per file)."""
    found = []
    real = worktree.resolve()
    for root, dirs, files in os.walk(worktree, followlinks=False):
        here = Path(root)
        # Relative parts: orch's own worktrees live under the project's .git/orch/.
        if here != worktree and (".git" in here.relative_to(worktree).parts
                                 or SCRATCH in here.relative_to(worktree).parts):
            dirs[:] = []
            continue
        keep = []
        for name in dirs:
            path = here / name
            if name in (".git", SCRATCH):
                continue
            if path.is_symlink():
                if path.resolve().is_dir() and not path.resolve().is_relative_to(real):
                    found.append(path)
                continue
            if (path / FARM_MARK).is_file():
                found.append(path)
                continue
            keep.append(name)
        for name in files:  # a link to a directory that os.walk reports as a file
            path = here / name
            if path.is_symlink() and path.resolve().is_dir() \
                    and not path.resolve().is_relative_to(real):
                found.append(path)
        dirs[:] = keep
    return found


def mount_source(path: Path) -> Path:
    """What the sandbox must mount read-only for a linked or farmed directory."""
    return farm_source(path) or path.resolve()


# --- running ---------------------------------------------------------------------------
# --- warm caches -----------------------------------------------------------------------
# Every check starts from an empty $TMPDIR, so caches the command keeps there (a Go build
# cache, a cargo target directory) begin cold: on polyglot-ledger each merge gate spent
# 20-80 s recompiling (2026-11-03). A check's red run on the stubs happens before any
# builder code exists; its $TMPDIR is kept as the check's seed and every later run of that
# check starts from a private copy. Nothing a builder's code writes is ever reused by
# another run, so a cache cannot be poisoned (e.g. with a forged "test passed" entry).
SEED_LIMIT = 2_000_000_000  # bytes; a larger $TMPDIR is not kept


def seed_path(scratch: Path, check: Check) -> Path:
    """Where a check's trusted seed lives: next to the run's scratch directory."""
    return scratch.parent / "seeds" / hashlib.sha256(check.id.encode()).hexdigest()[:16]


def _remove(path: Path) -> None:
    """rmtree that also clears read-only files and directories a tool left behind."""
    def retry(function: Callable[[str], object], name: str, _: BaseException) -> None:
        for target in (os.path.dirname(name), name):
            try:
                os.chmod(target, 0o700)
            except OSError:
                pass
        function(name)
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path, onexc=retry)


def _copy(source: Path, target: Path) -> bool:
    """A private copy: an APFS clone on macOS, a reflink or plain copy on Linux."""
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = ["-c", "-R", "-p"] if sys.platform == "darwin" else ["-a", "--reflink=auto"]
    done = subprocess.run(["cp", *flags, str(source), str(target)], capture_output=True)
    if done.returncode and sys.platform == "darwin":  # not APFS: a plain copy
        _remove(target)
        done = subprocess.run(["cp", "-R", "-p", str(source), str(target)], capture_output=True)
    return done.returncode == 0


def _size(path: Path, limit: int) -> int:
    total = 0
    for folder, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                continue
            if total > limit:
                return total
    return total


def keep_seed(execution: Path, seed: Path) -> None:
    """After a check's red run on the stubs: keep its $TMPDIR as that check's seed."""
    tmp = execution.resolve() / SCRATCH / "tmp"
    if seed.exists() or not tmp.is_dir() or _size(tmp, SEED_LIMIT) > SEED_LIMIT:
        return
    partial = seed.with_name(seed.name + ".partial")
    _remove(partial)
    if _copy(tmp, partial):
        _prune_runtime_state(partial)
        partial.rename(seed)
    else:
        _remove(partial)


def _prune_runtime_state(root: Path) -> None:
    """A seed holds build products, never a process's live state: sockets, pipes and device
    nodes are dropped. Sandboxed runs share pid numbers (each has its own pid namespace),
    so a tool's pid-named IPC socket left by the red run would collide with the same pid
    in a later run (tsx: `listen EADDRINUSE tsx-1000/164.pipe`, Meridian 2026-11-03)."""
    for folder, _, names in os.walk(root):
        for name in names:
            path = Path(folder) / name
            try:
                mode = path.lstat().st_mode
            except OSError:
                continue
            if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode) or stat.S_ISCHR(mode) \
                    or stat.S_ISBLK(mode):
                path.unlink(missing_ok=True)


def run_command(execution: Path, command: str, timeout: float,
                seed: Path | None = None) -> orch_sandbox.Result:
    execution = execution.resolve()  # TMPDIR and mounts must survive `cd` in the command
    scratch = execution / SCRATCH
    # Each command starts from its own $TMPDIR: an earlier command's leftovers (another
    # check's code ran there) are never visible to it.
    _remove(scratch / "tmp")
    if seed is None or not seed.is_dir() or not _copy(seed, scratch / "tmp"):
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
        result = run_command(execution, check.command, limit, seed_path(scratch, check))
        ok = result.ok
        passed &= ok
        status = "PASS" if ok else ("TIMEOUT" if result.timed_out else f"FAIL (exit {result.code})")
        lines.append(f"{status}: {check.id}")
        if not ok:
            tails.append(f"--- {check.command}\n{result.output[-6000:]}")
    # The status lines always survive truncation: failed() and the merge ripple read them.
    head = "\n".join(lines)
    output = ("\n\n" + "\n\n".join(tails)) if tails else ""
    room = max(REPORT_LIMIT - len(head), 0)
    audit = (f"\n\nissuer=controller\nmode=toolchain-sandbox\nrevision={revision}\n"
             f"checks={len(dict.fromkeys(checks))}\nwall_limit_s={limit:g}\n")
    return passed, head + (output[-room:] if room else "") + audit


def failed(report: str, candidates: list[str]) -> list[str]:
    """Candidate check ids that failed in a run() report, in report order."""
    out = []
    for line in report.splitlines():
        head, _, rest = line.partition(": ")
        if head.startswith(("FAIL", "TIMEOUT")) and rest in candidates and rest not in out:
            out.append(rest)
    return out


def red(execution: Path, check: Check,
        timeout: float = MIN_TIMEOUT) -> tuple[bool, str, str]:
    """A frozen check on the stubs: (usable, problem, output). It must run and fail, not
    pass; whether it failed for the right reason is judged from the output (orch_thin)."""
    result = run_command(execution, check.command, timeout)
    if result.code == 0 and not result.timed_out:
        return False, ("These tests pass against the stubs, so they prove nothing: test the "
                       "behaviour the requirements state."), result.output
    if result.code in NOT_RUNNABLE:
        listing = sorted(p.name + ("/" if p.is_dir() else "") for p in execution.iterdir()
                         if not p.name.startswith("."))[:40]
        return False, (
            f"The verification command could not run (exit {result.code}); output:\n"
            f"{result.output[-3000:]}\nIt runs with /bin/sh -c from the repository root, "
            f"whose entries are: {' '.join(listing)}. Change into the component's directory "
            "first when its tools and dependencies live there (for example `cd <dir> && ..."
            "`)."), result.output
    return True, "", result.output
