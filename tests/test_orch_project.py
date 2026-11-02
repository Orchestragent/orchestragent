import json
from dataclasses import asdict
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from orchestragent.envelope import digest
from orchestragent.live_workspace import LiveWorkspace
from orchestragent.native import NativeResult
from orchestragent.orch_audit import audit
from orchestragent.orch_environment import MANIFEST, PytestEnvironment
from orchestragent.orch_run import Orchestrator, PlanError
from orchestragent.orch_state import Run
from orchestragent.platform import sandbox_available, sandbox_name
from orchestragent.serialization import dumps
from orchestragent.workspace import Workspace
from tests.conftest import GOOD_ADD, GOOD_DOUBLE, ScriptedCaller, edit_result, local_runner


@pytest.fixture
def project_repo(orch_repo: Path) -> Path:
    (orch_repo / "existing.py").write_text("def original(value):\n    return value + 1\n")
    (orch_repo / "test_existing.py").write_text(
        "import pytest\nfrom existing import original\n"
        "@pytest.mark.parametrize('value',[1,7])\n"
        "def test_original(value):\n    assert original(value)==value+1\n"
    )
    Workspace.git(orch_repo, "add", "existing.py", "test_existing.py")
    Workspace.git(orch_repo, "commit", "-m", "existing pytest project")
    return orch_repo


@pytest.fixture
def project_environment(tmp_path: Path) -> PytestEnvironment:
    return PytestEnvironment.create(tmp_path / "environment")


@pytest.fixture
def project_caller(math_plan: dict[str, object]) -> ScriptedCaller:
    return ScriptedCaller({"plan": [NativeResult("ok", math_plan)],
                           "dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})


@pytest.fixture
def project_gate(monkeypatch: pytest.MonkeyPatch) -> Mock:
    gate = Mock(return_value=(True, "project tests passed"))
    monkeypatch.setattr(LiveWorkspace, "run_pytest", gate)
    monkeypatch.setattr("orchestragent.orch_run.sandbox_available", lambda: True)
    return gate


@pytest.fixture
def project_orchestrator(
    project_repo: Path, project_caller: ScriptedCaller, project_environment: PytestEnvironment,
    project_gate: Mock,
) -> Orchestrator:
    return Orchestrator(project_repo, project_caller, local_runner, notify=lambda _: None,
                        environment=project_environment)


def test_project_baseline_and_final_gate_pin_full_suite_and_record_evidence(
    project_orchestrator: Orchestrator, project_gate: Mock, project_caller: ScriptedCaller,
    project_environment: PytestEnvironment,
) -> None:
    planned = project_orchestrator.plan("add and double")
    baseline = project_gate.call_args_list[0]
    assert baseline.args[2:] == (planned.base, None)
    assert baseline.kwargs["environment"] == project_environment
    assert not baseline.args[0].exists()  # disposable pre-call worktree was released
    assert (planned.environment_root, planned.environment_fingerprint) == (
        str(project_environment.root), project_environment.fingerprint)
    completed = project_orchestrator.execute(planned.id, jobs=1)
    assert completed.status == "done" and project_gate.call_count == 2
    assert project_gate.call_args.args[2:] == (completed.verified_head, None)
    assert project_caller.keys() == ["plan", "dsh:T-001", "dsh:T-002"]
    evidence = [event for event in project_orchestrator.state.events(planned.id)
                if event.kind in {"plan.project_baseline", "run.project_final"}]
    assert [event.data["revision"] for event in evidence] == [planned.base, completed.verified_head]
    assert all(event.data["environment_fingerprint"] == project_environment.fingerprint
               and event.data["passed"] is True for event in evidence)
    assert project_orchestrator.metrics["peak_verifying"] >= 1


@pytest.mark.parametrize("failure", ["red", "zero", "unsupported", "tampered"])
def test_failed_project_baseline_never_dispatches_a_provider(
    project_orchestrator: Orchestrator, project_gate: Mock, project_caller: ScriptedCaller,
    project_environment: PytestEnvironment, failure: str,
) -> None:
    if failure == "unsupported":
        project_gate.side_effect = RuntimeError("unsupported sandbox")
    elif failure == "tampered":
        (project_environment.root / "py.py").write_text("changed")
    else:
        project_gate.return_value = False, "zero tests" if failure == "zero" else "failed test"
    with pytest.raises(PlanError, match="project pytest baseline"):
        project_orchestrator.plan("must not dispatch")
    assert project_caller.calls == [] and project_orchestrator.state.usage.calls() == []
    assert project_orchestrator.state.runs() == []


def test_final_pytest_regression_cannot_be_reported_done(
    project_orchestrator: Orchestrator, project_gate: Mock,
) -> None:
    project_gate.side_effect = [(True, "baseline green"), (False, "x" * 6000 + "old test failed")]
    planned = project_orchestrator.plan("new tasks")
    run = project_orchestrator.execute(planned.id, jobs=1)
    evidence = next(event.data for event in project_orchestrator.state.events(run.id)
                    if event.kind == "run.project_final")
    assert run.status == "parked" and "project pytest gate failed" in run.message
    assert evidence["passed"] is False and len(str(evidence["report"])) <= 4000
    assert Workspace.git(project_orchestrator.repo, "tag", "-l", f"orch/{run.id}/done") == ""
    assert any(event.kind == "run.final_gate" and event.data["passed"] is True
               for event in project_orchestrator.state.events(run.id))


@pytest.mark.parametrize("change", ["tampered", "missing", "swapped", "incomplete", "unsupported"])
def test_resume_rejects_environment_drift_before_any_model_call(
    project_orchestrator: Orchestrator, project_environment: PytestEnvironment,
    project_caller: ScriptedCaller, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    run = project_orchestrator.plan("resume after validation")
    if change == "missing":
        project_environment.root.rename(project_environment.root.with_name("moved"))
    elif change == "incomplete":
        project_orchestrator.state.update_run(run.id, "test.corrupt", {}, environment_root="")
    elif change == "unsupported":
        monkeypatch.setattr("orchestragent.orch_run.sandbox_available", lambda: False)
    else:
        source = project_environment.root / "py.py"
        source.write_bytes(source.read_bytes() + b"\n# mutation\n")
        if change == "swapped":
            path = project_environment.root / MANIFEST
            document = json.loads(path.read_text())
            document["manifest"]["files"]["py.py"] = digest(source.read_text())
            document["fingerprint"] = digest(dumps(document["manifest"]))
            path.write_text(dumps(document))
            assert PytestEnvironment.load(project_environment.root) != project_environment
    before = len(project_caller.calls)
    result = project_orchestrator.execute(run.id)
    assert result.status == "paused" and "environment" in result.message
    assert len(project_caller.calls) == before
    assert project_orchestrator.state.events(run.id)[-1].kind == "run.environment_invalid"


def test_resume_uses_persisted_environment_instead_of_new_constructor_argument(
    project_orchestrator: Orchestrator, project_caller: ScriptedCaller,
    project_environment: PytestEnvironment, project_gate: Mock,
) -> None:
    run = project_orchestrator.plan("frozen environment")
    other = PytestEnvironment(Path("nonexistent-constructor-environment"), "different")
    resumed = Orchestrator(project_orchestrator.repo, project_caller, local_runner,
                           notify=lambda _: None, environment=other)
    result = resumed.execute(run.id, jobs=1)
    assert result.status == "done" and resumed.environment == project_environment
    assert project_gate.call_args.kwargs["environment"] == project_environment


def test_legacy_run_without_environment_ignores_new_constructor_environment(
    project_repo: Path, project_caller: ScriptedCaller, project_gate: Mock,
) -> None:
    original = Orchestrator(project_repo, project_caller, local_runner, notify=lambda _: None)
    run = original.plan("legacy")
    body = asdict(run)
    del body["environment_root"], body["environment_fingerprint"]
    assert Run(**body).environment_root == ""  # old saved rows decode with defaults
    resumed = Orchestrator(project_repo, project_caller, local_runner, notify=lambda _: None,
                           environment=PytestEnvironment(Path("missing"), "ignored"))
    assert resumed.execute(run.id, jobs=1).status == "done"
    assert resumed.environment is None and project_gate.call_count == 0


def test_explicit_policy_is_detached_from_callers_mutable_rule_objects(
    project_repo: Path, project_caller: ScriptedCaller,
) -> None:
    owner = Orchestrator(project_repo, project_caller, local_runner, notify=lambda _: None)
    owner.policies.enable()
    lesson = "Preserve both arithmetic operands."
    owner.state.add_rule(lesson, "training")
    policy = owner.policies.snapshot(("r1",))
    frozen = Orchestrator(project_repo, project_caller, local_runner, notify=lambda _: None,
                          policy=policy)
    policy.rules[0].text = "Mutated caller-owned rule."
    run = frozen.execute(frozen.plan("detached policy").id, jobs=1)
    assert run.status == "done" and run.policy_id == policy.id
    assert frozen.policy is not None and frozen.policy.rules[0].text == lesson
    assert frozen.policy.rules[0] is not policy.rules[0]
    assert "Mutated caller-owned rule." not in project_caller.calls[-1][2]


@pytest.mark.skipif(not sandbox_available(), reason="real OS sandbox acceptance")
def test_existing_pytest_project_and_generated_unittest_tasks_pass_real_sandbox(
    project_repo: Path, project_caller: ScriptedCaller, project_environment: PytestEnvironment,
) -> None:
    orchestrator = Orchestrator(project_repo, project_caller, LiveWorkspace.run_unittests,
                                notify=lambda _: None, environment=project_environment)
    run = orchestrator.execute(orchestrator.plan("real sandbox integration").id, jobs=1)
    evidence = next(event.data for event in orchestrator.state.events(run.id)
                    if event.kind == "run.project_final")
    assert run.status == "done" and evidence["passed"] is True
    report = str(evidence["report"])
    assert "passed" in report and f"mode={sandbox_name()}" in report
    assert f"environment={project_environment.fingerprint}" in str(evidence["report"])


@pytest.mark.skipif(not sandbox_available(), reason="real OS sandbox acceptance")
@pytest.mark.parametrize("dependency_environment", [(".",), (".", "src")], indirect=True)
def test_cluster_uses_the_same_dependency_environment_for_every_verification_gate(
    project_repo: Path, project_caller: ScriptedCaller, dependency_environment: PytestEnvironment,
) -> None:
    prefix = "src/" if "src" in dependency_environment.source_roots else ""
    if prefix:
        (project_repo / "src").mkdir()
        Workspace.git(project_repo, "mv", "existing.py", "src/existing.py")
        Workspace.git(project_repo, "commit", "-m", "src layout")
        plan = project_caller.responses["plan"][0].data
        for entry in cast(list[dict[str, str]], plan["files"]):
            if not LiveWorkspace.is_test_file(entry["path"]):
                entry["path"] = prefix + entry["path"]
        for task in cast(list[dict[str, object]], plan["tasks"]):
            task["files"] = [prefix + p for p in cast(list[str], task["files"])]
        project_caller.responses["dsh:T-002"] = [edit_result("src/doubler.py", "return add(a,a)")]
    project_caller.responses["dsh:T-001"] = [edit_result(
        prefix + "adder.py", "from typing_extensions import assert_type\n"
        "def add(a: int, b: int) -> int:\n    return assert_type(a + b, int)\n", search="")]
    orch = Orchestrator(project_repo, project_caller, LiveWorkspace.run_unittests,
                        notify=lambda _: None, environment=dependency_environment)
    planned = orch.plan("real dependency integration")
    resumed = Orchestrator(project_repo, project_caller, LiveWorkspace.run_unittests,
                           notify=lambda _: None)
    run = resumed.execute(planned.id, jobs=1)
    assert run.status == "done", run.message
    assert project_caller.keys() == ["plan", "dsh:T-001", "dsh:T-002"]
    assert all(dependency_environment.fingerprint in prompt
               and "typing_extensions" in prompt for _, _, prompt in project_caller.calls)
    fresh = Orchestrator(project_repo, project_caller, LiveWorkspace.run_unittests,
                         notify=lambda _: None)
    assert audit(fresh, run.id) == []
    calls = {c.id: c for c in resumed.state.usage.calls(run.id)}
    called = {e.task_id: e.data["prefix"] for e in resumed.state.events(run.id)
              if e.kind == "attempt.called"}
    assert all(c.prefix_hash == called[c.task_id] for c in calls.values() if c.purpose == "build")


@pytest.mark.parametrize("target", ["conftest.py", "nested/conftest.py", "existing-owner"])
def test_generated_collection_control_cannot_hide_original_project_tests(
    project_orchestrator: Orchestrator, project_caller: ScriptedCaller,
    math_plan: dict[str, object], target: str,
) -> None:
    if target == "existing-owner":
        path = project_orchestrator.repo / "conftest.py"
        path.write_text("# Original collection control stays frozen.\n")
        Workspace.git(project_orchestrator.repo, "add", "conftest.py")
        Workspace.git(project_orchestrator.repo, "commit", "-m", "original conftest")
        task = cast(list[dict[str, object]], math_plan["tasks"])[0]
        task["files"] = [*cast(list[str], task["files"]), "conftest.py"]
    else:
        cast(list[dict[str, str]], math_plan["files"]).append({
            "path": target, "content": "collect_ignore=['test_existing.py']\n",
        })
    with pytest.raises(PlanError) as failure:
        project_orchestrator.plan("cannot bypass original tests", attempts=1)
    assert "collection control is frozen" in str(failure.value.errors)
    assert project_caller.keys() == ["plan"]
    assert not project_orchestrator.state.runs()


def test_persisted_collection_control_ownership_is_refused_before_dispatch(
    project_orchestrator: Orchestrator, project_caller: ScriptedCaller,
) -> None:
    run = project_orchestrator.plan("checkpoint")
    task = cast(list[dict[str, object]], run.plan["tasks"])[0]
    task["files"] = [*cast(list[str], task["files"]), "conftest.py"]
    project_orchestrator.state.update_run(run.id, "test.invalid-plan", {}, plan=run.plan)
    assert project_orchestrator.execute(run.id).status == "paused"
    assert project_caller.keys() == ["plan"]


def test_project_baseline_uses_requested_commit_and_preserves_dirty_working_tree(
    project_orchestrator: Orchestrator, project_gate: Mock,
) -> None:
    base = Workspace.git(project_orchestrator.repo, "rev-parse", "HEAD")
    dirty = project_orchestrator.repo / "test_existing.py"
    dirty.write_text("uncommitted-user-sentinel")

    def inspect(execution: Path, scratch: Path, revision: str, names: list[str] | None,
                *, environment: PytestEnvironment, timeout: float = 25.0) -> tuple[bool, str]:
        assert revision == base and names is None
        assert "def test_original" in (execution / "test_existing.py").read_text()
        return True, "requested base verified"

    project_gate.side_effect = inspect
    run = project_orchestrator.plan("base snapshot", base=base)
    assert run.base == base and project_gate.call_count == 1
    assert dirty.read_text() == "uncommitted-user-sentinel"


@pytest.mark.skipif(not sandbox_available(), reason="real OS sandbox acceptance")
def test_real_existing_pytest_regression_parks_after_generated_unittests_pass(
    project_repo: Path, project_caller: ScriptedCaller, project_environment: PytestEnvironment,
) -> None:
    (project_repo / "test_existing.py").write_text(
        "from pathlib import Path\ndef test_original():\n    assert not Path('adder.py').exists()\n"
    )
    Workspace.git(project_repo, "add", "test_existing.py")
    Workspace.git(project_repo, "commit", "-m", "existing project invariant")
    orchestrator = Orchestrator(project_repo, project_caller, LiveWorkspace.run_unittests,
                                notify=lambda _: None, environment=project_environment)
    planned = orchestrator.plan("new module violates original invariant")
    run = orchestrator.execute(planned.id, jobs=1)
    assert run.status == "parked"
    assert any(event.kind == "run.final_gate" and event.data["passed"] is True
               for event in orchestrator.state.events(run.id))
    assert any(event.kind == "run.project_final" and event.data["passed"] is False
               for event in orchestrator.state.events(run.id))


@pytest.fixture
def missing_project_baseline(project_orchestrator: Orchestrator) -> Run:
    run = project_orchestrator.plan("interrupted checkpoint baseline persistence")
    with project_orchestrator.state.store.transaction() as db:
        db.execute("DELETE FROM orch_events WHERE run_id=? AND kind='plan.project_baseline'",
                   (run.id,))
    return run


def test_resume_recovers_missing_durable_baseline_before_dispatch(
    project_orchestrator: Orchestrator, missing_project_baseline: Run,
    project_gate: Mock, project_caller: ScriptedCaller,
) -> None:
    run = project_orchestrator.execute(missing_project_baseline.id, jobs=1)
    evidence = next(event.data for event in project_orchestrator.state.events(run.id)
                    if event.kind == "plan.project_baseline")
    assert run.status == "done" and evidence["recovered"] is True
    assert evidence["passed"] is True and evidence["revision"] == run.base
    assert project_gate.call_args_list[1].args[2:] == (run.base, None)
    assert project_caller.keys() == ["plan", "dsh:T-001", "dsh:T-002"]


def test_failed_missing_baseline_recovery_pauses_without_provider_calls(
    project_orchestrator: Orchestrator, missing_project_baseline: Run,
    project_gate: Mock, project_caller: ScriptedCaller,
) -> None:
    project_gate.return_value = False, "original baseline reproof failed"
    run = project_orchestrator.execute(missing_project_baseline.id)
    evidence = next(event.data for event in project_orchestrator.state.events(run.id)
                    if event.kind == "plan.project_baseline")
    assert run.status == "paused" and project_caller.keys() == ["plan"]
    assert evidence["passed"] is False and evidence["recovered"] is True
    assert "reproof failed" in str(evidence["report"])


@pytest.mark.parametrize("status", ["planned", "done"])
def test_project_harden_is_blocked_before_calls_and_preserves_done_tag(
    project_orchestrator: Orchestrator, project_caller: ScriptedCaller, status: str,
) -> None:
    run = project_orchestrator.plan("project hardening is not yet supported")
    if status == "done":
        run = project_orchestrator.execute(run.id, jobs=1)
    tag = Workspace.git(project_orchestrator.repo, "tag", "-l", f"orch/{run.id}/done")
    head = Workspace.git(project_orchestrator.integration(run.id), "rev-parse", "HEAD")
    before = len(project_caller.calls)
    with pytest.raises(ValueError, match="Project pytest runs cannot be hardened"):
        project_orchestrator.harden(run.id)
    assert len(project_caller.calls) == before
    assert Workspace.git(project_orchestrator.integration(run.id), "rev-parse", "HEAD") == head
    assert Workspace.git(project_orchestrator.repo, "tag", "-l", f"orch/{run.id}/done") == tag
    assert project_orchestrator.state.run(run.id).status == status
