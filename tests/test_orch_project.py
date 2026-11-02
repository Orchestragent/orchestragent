from pathlib import Path

import pytest

from orchestragent.orch_run import Orchestrator
from orchestragent.workspace import Workspace
from tests.conftest import GOOD_ADD, GOOD_DOUBLE, ScriptedCaller, local_runner, scripted


@pytest.fixture
def project_repo(orch_repo: Path) -> Path:
    (orch_repo / "existing.py").write_text("def original(value):\n    return value + 1\n")
    Workspace.git(orch_repo, "add", "existing.py")
    Workspace.git(orch_repo, "commit", "-m", "existing project")
    return orch_repo


@pytest.fixture
def project_caller(math_plan: dict[str, object]) -> ScriptedCaller:
    return scripted(math_plan, {"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})


def test_explicit_policy_is_detached_from_callers_mutable_rule_objects(
    project_repo: Path, project_caller: ScriptedCaller, unsandboxed: None,
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
