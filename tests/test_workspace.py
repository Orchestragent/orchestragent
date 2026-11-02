from pathlib import Path

import pytest

from orchestragent.workspace import GOOD_SOURCE, Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path / "workspace")


@pytest.mark.parametrize("staged", [False, True])
def test_recover_seed_interruption_before_first_commit(workspace: Workspace, staged: bool) -> None:
    task_id = "a" * 32
    repo = workspace.repo(task_id)
    repo.mkdir()
    workspace.git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("Interrupted seed write")
    if staged:
        workspace.git(repo, "add", "README.md")
    revision, _ = workspace.implement(task_id, GOOD_SOURCE)
    passed, _ = workspace.integrate_and_test(task_id, revision)
    assert passed


@pytest.mark.parametrize(
    "replacement",
    [
        "raise RuntimeError('must never execute')\n",
        "from pathlib import Path\nPath('untrusted-marker').touch()\n" + GOOD_SOURCE,
    ],
)
def test_tests_execute_pinned_blob_not_dirty_worktree(
    workspace: Workspace, replacement: str
) -> None:
    task_id = "b" * 32
    revision, _ = workspace.implement(task_id, GOOD_SOURCE)
    workspace.integrate_and_test(task_id, revision)
    repo = workspace.repo(task_id)
    (repo / "calculator.py").write_text(replacement)
    passed, _ = workspace.integrate_and_test(task_id, revision)
    assert (passed, (repo / "untrusted-marker").exists()) == (True, False)
