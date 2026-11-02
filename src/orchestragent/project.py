"""One shared git repository per project, with a branch per task.

Tasks never write to the shared checkout directly: each gets a worktree on its
own branch, and only an accepted task is committed and pushed to the remote.
"""

import re
import subprocess
from pathlib import Path

HTTPS_GITHUB = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def ssh_url(url: str) -> str:
    """The server holds an SSH key, so GitHub HTTPS URLs are rewritten to SSH."""
    match = HTTPS_GITHUB.match(url.strip())
    if not match:
        return url
    owner, repo = match.group(1), match.group(2)
    return f"git@github.com:{owner}/{repo}.git"


class GitError(RuntimeError):
    pass


class ProjectRepo:
    """A clone whose branches are the single source of truth for a project."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    # --- plumbing -------------------------------------------------------
    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.root),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()[:300]}"
            )
        return result.stdout.strip()

    @classmethod
    def clone(cls, url: str, dest: Path) -> "ProjectRepo":
        dest = Path(dest)
        if dest.exists() and any(dest.iterdir()):
            return cls(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "clone", ssh_url(url), str(dest)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise GitError(f"clone failed: {(result.stderr or result.stdout).strip()[:300]}")
        return cls(dest)

    def remote(self) -> str:
        try:
            return self.git("remote", "get-url", "origin")
        except GitError:
            return ""

    def head(self, ref: str = "HEAD", cwd: Path | None = None) -> str:
        return self.git("rev-parse", ref, cwd=cwd)

    def branches(self) -> list[str]:
        raw = self.git("for-each-ref", "--format=%(refname:short)", "refs/heads")
        return [line for line in raw.splitlines() if line]

    # --- task lifecycle -------------------------------------------------
    def start_branch(self, branch: str, base: str = "HEAD") -> str:
        """Create (or reset) a task branch at the current base and return its sha."""
        self.git("branch", "-f", branch, base)
        return self.head(branch)

    def worktree(self, branch: str, dest: Path) -> Path:
        dest = Path(dest)
        if dest.exists():
            raise GitError("worktree destination already exists")
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "add", str(dest), branch)
        return dest

    def commit_worktree(self, worktree: Path, message: str, author: str) -> str:
        """Commit everything under a task worktree on its branch."""
        self.git("add", "-A", cwd=worktree)
        if not self.git("status", "--porcelain", cwd=worktree):
            return self.head(cwd=worktree)
        self.git(
            "-c", f"user.name={author}", "-c", "user.email=orchestragent@localhost",
            "commit", "-m", message, cwd=worktree,
        )
        return self.head(cwd=worktree)

    def push(self, branch: str, remote: str = "origin") -> str:
        self.git("push", remote, f"{branch}:{branch}")
        return self.head(branch)

    def fetch(self, remote: str = "origin") -> None:
        self.git("fetch", remote, "--prune")

    def integrate(self, branch: str, target: str = "main") -> str:
        """Fast-forward the target branch to an accepted task branch."""
        self.git("checkout", target)
        try:
            self.git("merge", "--ff-only", branch)
        finally:
            self.git("checkout", target)
        return self.head(target)
