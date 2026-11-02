"""Shared Git isolation and fixed Mock fixture. LiveWorkspace adds process isolation.

Worktrees alone are NOT security sandboxes.
"""

import ast
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

GOOD_SOURCE = "def add(a: int, b: int) -> int:\n    return a + b\n"
BAD_SOURCE = "def add(a: int, b: int) -> int:\n    return a - b\n"


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _git_call(
        cwd: Path, args: tuple[str, ...], env: Mapping[str, str] | None
    ) -> tuple[list[str], dict[str, str]]:
        base = {
            "PATH": os.defpath,
            "HOME": str(cwd),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
            **(env or {}),
        }
        # Do not inherit git hooks, external diff tools, SSH agent, or API credentials.
        environment = {**base, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        command = [
            "git",
            "-c",
            "user.name=Orchestragent Mock",
            "-c",
            "user.email=mock@localhost",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            *args,
        ]
        return command, environment

    @staticmethod
    def git(
        cwd: Path,
        *args: str,
        strip: bool = True,
        env: Mapping[str, str] | None = None,
        timeout: float = 10,
    ) -> str:
        """`env` may override identity/date variables (GIT_AUTHOR_* take precedence
        over the -c user config); it never restores hooks or the user's config."""
        command, environment = Workspace._git_call(cwd, args, env)
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
            # Own session: a terminal Ctrl-C must not kill git half-way through a step.
            start_new_session=True,
        )
        return result.stdout.strip() if strip else result.stdout

    @staticmethod
    def git_bytes(cwd: Path, *args: str, stdin: bytes = b"", timeout: float = 60) -> bytes:
        """Binary plumbing (`cat-file --batch`, `ls-tree -z`) with the same isolation."""
        command, environment = Workspace._git_call(cwd, args, None)
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=True,
            start_new_session=True,
        )
        return result.stdout

    def repo(self, task_id: str) -> Path:
        # Task ids originate in uuid4; validate again at the filesystem boundary.
        if len(task_id) != 32 or any(c not in "0123456789abcdef" for c in task_id):
            raise ValueError("Invalid workspace id")
        return self.root / task_id

    def implement(self, task_id: str, source: str) -> tuple[str, str]:
        if ast.dump(ast.parse(source)) not in {
            ast.dump(ast.parse(GOOD_SOURCE)),
            ast.dump(ast.parse(BAD_SOURCE)),
        }:
            raise ValueError("Mock workspace accepts only the fixed arithmetic fixture")
        repo, implementation = self.prepare(task_id)
        (implementation / "calculator.py").write_text(source)
        self.git(implementation, "add", "calculator.py")
        if self.git(implementation, "diff", "--cached", "--name-only"):
            self.git(implementation, "commit", "-m", "Mock implementation")
        revision = self.git(implementation, "rev-parse", "HEAD")
        diff = self.git(repo, "diff", "main", revision, "--", "calculator.py")
        return revision, diff

    def prepare(self, task_id: str) -> tuple[Path, Path]:
        """Shared replay-safe seed and isolated implementation worktree."""
        repo = self.repo(task_id)
        repo.mkdir(exist_ok=True)
        if not (repo / ".git").exists():
            self.git(repo, "init", "-b", "main")
        try:
            self.git(repo, "rev-parse", "--verify", "HEAD")
        except subprocess.CalledProcessError:
            # A prior process may have died after writing/staging but before committing.
            (repo / "README.md").write_text("Isolated Orchestragent task workspace.\n")
            self.git(repo, "add", "README.md")
            self.git(repo, "commit", "-m", "Seed Mock workspace")
        implementation = repo / "implementation"
        if not implementation.exists():
            self.git(
                repo, "worktree", "add", "-B", "mock/implementation", str(implementation), "main"
            )
        return repo, implementation

    def review_source(self, task_id: str, revision: str) -> str:
        repo = self.repo(task_id)
        if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
            raise ValueError("Review requires a fixed Git revision")
        review = repo / "review"
        if not review.exists():
            self.git(repo, "worktree", "add", "--detach", str(review), revision)
        if self.git(review, "rev-parse", "HEAD") != revision:
            raise ValueError("Review revision changed")
        return self.git(review, "show", f"{revision}:calculator.py") + "\n"

    def integrate_and_test(self, task_id: str, revision: str) -> tuple[bool, str]:
        repo = self.repo(task_id)
        self.git(repo, "merge", "--ff-only", revision)
        if self.git(repo, "rev-parse", "HEAD") != revision:
            raise ValueError("Integrated revision differs from reviewed revision")
        source = self.git(repo, "show", "HEAD:calculator.py") + "\n"
        # Fixed fixture runner avoids executing arbitrary code, even from a corrupt artifact.
        syntax = ast.dump(ast.parse(source))
        if syntax not in {ast.dump(ast.parse(GOOD_SOURCE)), ast.dump(ast.parse(BAD_SOURCE))}:
            raise ValueError("Refusing non-fixture code")
        runner = source + "\nassert add(2, 3) == 5\nassert add(-2, 2) == 0\n"
        # Execute the exact validated blob, never import the mutable worktree file.
        # -I excludes cwd, PYTHONPATH and user packages; -S disables site initialization.
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", runner],
            cwd=repo,
            env={"PATH": os.defpath, "HOME": str(repo)},
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        passed = result.returncode == 0
        return passed, f"Mock fixed-fixture integration tests: {'PASS 2/2' if passed else 'FAIL'}"
