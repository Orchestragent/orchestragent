from pathlib import Path

import pytest

from orchestragent.project import GitError, ProjectRepo


def seed(tmp_path: Path) -> tuple[Path, str]:
    """A bare origin plus a working clone, both local (no network)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    ProjectRepo(tmp_path).git("init", "--bare", "-b", "main", str(origin))
    repo = ProjectRepo.clone(str(origin), tmp_path / "repo")
    (tmp_path / "repo" / "a.py").write_text("x = 1\n")
    repo.git("add", "-A")
    repo.git("-c", "user.name=seed", "-c", "user.email=s@local", "commit", "-m", "seed")
    repo.git("push", "origin", "main")
    return origin, str(repo.root)


def test_task_branch_worktree_commit_and_push(tmp_path: Path) -> None:
    origin, path = seed(tmp_path)
    repo = ProjectRepo(Path(path))
    repo.start_branch("task/1")
    tree = repo.worktree("task/1", tmp_path / "wt")
    (tree / "b.py").write_text("y = 2\n")
    sha = repo.commit_worktree(tree, "task 1", "orchestra")
    assert sha == repo.head("task/1")
    repo.push("task/1")
    remote_heads = ProjectRepo(tmp_path / "repo").git(
        "ls-remote", "--heads", str(origin)
    )
    assert "refs/heads/task/1" in remote_heads


def test_integrate_fast_forwards_only(tmp_path: Path) -> None:
    _origin, path = seed(tmp_path)
    repo = ProjectRepo(Path(path))
    repo.start_branch("task/2")
    tree = repo.worktree("task/2", tmp_path / "wt2")
    (tree / "c.py").write_text("z = 3\n")
    repo.commit_worktree(tree, "task 2", "orchestra")
    sha = repo.integrate("task/2", "main")
    assert sha == repo.head("task/2")
    assert (Path(path) / "c.py").exists()


def test_missing_needs_rebase_is_reported(tmp_path: Path) -> None:
    _origin, path = seed(tmp_path)
    repo = ProjectRepo(Path(path))
    repo.start_branch("task/3")
    tree = repo.worktree("task/3", tmp_path / "wt3")
    (tree / "d.py").write_text("w = 4\n")
    repo.commit_worktree(tree, "task 3", "orchestra")
    (Path(path) / "main.py").write_text("m = 5\n")
    repo.git("add", "-A")
    repo.git("-c", "user.name=x", "-c", "user.email=x@local", "commit", "-m", "main moves")
    with pytest.raises(GitError):
        repo.integrate("task/3", "main")


def test_github_https_is_rewritten_to_ssh() -> None:
    from orchestragent.project import ssh_url

    assert ssh_url("https://github.com/me/quant.git") == "git@github.com:me/quant.git"
    assert ssh_url("https://github.com/me/quant") == "git@github.com:me/quant.git"
    assert ssh_url("git@github.com:me/quant.git") == "git@github.com:me/quant.git"
    assert ssh_url("https://gitlab.com/me/quant.git") == "https://gitlab.com/me/quant.git"
