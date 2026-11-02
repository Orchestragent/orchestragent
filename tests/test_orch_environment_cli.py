import json
from pathlib import Path

import pytest

from orchestragent.live_workspace import LiveWorkspace
from orchestragent.orch_cli import main
from orchestragent.orch_environment import PytestEnvironment
from orchestragent.workspace import Workspace


@pytest.fixture
def pinned_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PytestEnvironment:
    environment = PytestEnvironment(tmp_path / "environment", "verified-fingerprint")
    monkeypatch.setattr(PytestEnvironment, "load", lambda _: environment)

    def forbidden(_: Path) -> None:
        pytest.fail("Environment verification must not construct provider clients")

    monkeypatch.setattr("orchestragent.orch_cli.providers", forbidden)
    return environment


@pytest.mark.parametrize(("outcome", "expected"), [("passed", 0), ("failed", 3), ("error", 1)])
def test_pytest_cli_verifies_pinned_bytes_and_cleans_worktree_on_every_outcome(
    orch_repo: Path, pinned_environment: PytestEnvironment, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], outcome: str, expected: int,
) -> None:
    target = orch_repo / "test_project.py"
    target.write_text("def test_saved():\n    assert True\n")
    Workspace.git(orch_repo, "add", "test_project.py")
    Workspace.git(orch_repo, "commit", "-m", "saved tests")
    revision = Workspace.git(orch_repo, "rev-parse", "HEAD")
    target.write_text("dirty local work must survive\n")

    def runner(execution: Path, scratch: Path, sha: str, names: list[str] | None,
               *, environment: PytestEnvironment, timeout: float) -> tuple[bool, str]:
        assert execution != orch_repo and "assert True" in (execution / target.name).read_text()
        assert (sha, names, environment, timeout) == (
            revision, ["test_project.py::test_saved"], pinned_environment, 25)
        assert scratch.is_dir()
        if outcome == "error":
            raise RuntimeError("verification stopped")
        return outcome == "passed", "bounded pytest output"

    monkeypatch.setattr(LiveWorkspace, "run_pytest", runner)
    code = main(["--repo", str(orch_repo), "verify", "--environment",
                 str(pinned_environment.root), "test_project.py::test_saved", "--json"])
    captured = capsys.readouterr()
    assert code == expected
    if outcome != "error":
        result = json.loads(captured.out)
        assert result["revision"] == revision and result["passed"] is (outcome == "passed")
    assert target.read_text() == "dirty local work must survive\n"
    assert Workspace.git(orch_repo, "rev-parse", "HEAD") == revision
    assert Workspace.git(orch_repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_environment_inspection_is_provider_free_and_returns_a_fingerprint(
    pinned_environment: PytestEnvironment, capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["env", "inspect", str(pinned_environment.root)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "root": str(pinned_environment.root), "fingerprint": pinned_environment.fingerprint,
    }


def test_plan_rejects_a_missing_environment_before_provider_construction(
    orch_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orchestragent.orch_cli.providers",
                        lambda _: pytest.fail("Missing environment must stop before providers"))
    assert main(["--repo", str(orch_repo), "plan", "spec.md", "--environment",
                 str(tmp_path / "missing-environment")]) == 1


def test_environment_cli_copies_only_explicit_installed_dependencies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orchestragent.orch_cli.providers",
                        lambda _: pytest.fail("Environment creation must not construct providers"))
    path = tmp_path / "dependencies"
    assert main(["env", "create", str(path), "--package", "typing_extensions",
                 "--source-root", "src"]) == 0
    result = json.loads(capsys.readouterr().out)
    environment = PytestEnvironment.load(path)
    assert result["fingerprint"] == environment.fingerprint
    assert "typing_extensions" in environment.modules
    assert environment.source_roots == (".", "src")
