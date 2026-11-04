"""The supervisor: after one cheap retry, a strong model reads the evidence and chooses one
action from a closed menu; the engine applies it mechanically."""

from dataclasses import replace
from pathlib import Path

import pytest

from orchestragent.native import ContextPrompt, NativeResult
from orchestragent.orch_run import Orchestrator, Parked
from orchestragent.orch_state import OrchEvent, Run
from orchestragent.orch_supervisor import ACTIONS, MAX_SUPERVISIONS, SUPERVISOR_SCHEMA
from tests.conftest import (
    ADD_CHECK,
    ADD_TEST,
    ADD_TESTS,
    BAD_ADD,
    DOUBLE_CHECK,
    DOUBLE_SHARD,
    DOUBLE_TEST,
    DOUBLE_TESTS,
    GOOD_ADD,
    GOOD_DOUBLE,
    SUPERVISE_ESCALATE,
    SUPERVISE_GIVE_UP,
    SUPERVISE_RETRY,
    Clock,
    ScriptedCaller,
    component_design,
    edit_result,
    local_runner,
    scripted,
    supervise,
)


def events(orch: Orchestrator, run_id: str, kind: str, task: str = "") -> list[OrchEvent]:
    return [e for e in orch.state.events(run_id, task) if e.kind == kind]


def test_cheap_retry_then_supervisor_guidance_reaches_the_builder(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    caller.responses.update({"dsh:T-001": [BAD_ADD, BAD_ADD, GOOD_ADD],
                             "claude:T-001": [SUPERVISE_RETRY],
                             "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    assert run.status == "done", orchestrator.gate_failure
    builds = [text for provider, key, text in caller.calls if key == "dsh:T-001"]
    assert "SUPERVISOR GUIDANCE" not in builds[1]  # the cheap retry carries only the failure
    assert "SUPERVISOR GUIDANCE" in builds[2] and "fix the root cause" in builds[2]
    task = orchestrator.state.task(run.id, "T-001")
    # the supervisor turn is an attempt of its own (a3); the guided build is a4
    assert (task.attempts, task.supervisions, task.status) == (4, 1, "DONE")
    decided = events(orchestrator, run.id, "task.supervised", "T-001")
    assert [e.data["action"] for e in decided] == ["retry"]
    failed = events(orchestrator, run.id, "task.failed", "T-001")
    assert [e.data["next"] for e in failed] == [1, 2]


def test_escalate_hands_the_next_attempt_to_the_strong_model(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    caller.responses.update({"dsh:T-001": [BAD_ADD, BAD_ADD],
                             "claude:T-001": [SUPERVISE_ESCALATE, GOOD_ADD],
                             "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    assert run.status == "done", orchestrator.gate_failure
    assert caller.keys()[3:] == ["dsh:T-001", "dsh:T-001", "claude:T-001", "claude:T-001",
                                 "dsh:T-002"]  # after plan, look and the component design
    patch = caller.calls[6][2]
    assert "SUPERVISOR GUIDANCE" in patch and "stronger model" in patch


def test_supervisor_sees_the_evidence_and_the_menu(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    caller.responses.update({"dsh:T-001": [BAD_ADD, BAD_ADD], "claude:T-001": [SUPERVISE_GIVE_UP]})
    run = orchestrator.execute(planned.id)
    prompt = next(text for _, key, text in caller.calls if key == "claude:T-001")
    assert isinstance(prompt, ContextPrompt)
    assert run.spec in prompt.shared and "test_add.py" in prompt  # tests are per task
    for action in ACTIONS:
        assert f"- {action}:" in prompt.shared
    assert "return a - b" in prompt and "## ATTEMPTS" in prompt and "a2:" in prompt
    assert set(SUPERVISOR_SCHEMA["properties"]) == {  # type: ignore[index]
        "action", "explanation", "command", "guidance", "test_edits", "command_edits",
        "contract", "tasks"}
    task = orchestrator.state.task(run.id, "T-001")
    assert task.status == "PARKED" and task.reason.startswith("supervisor: ")
    assert "a person should look" in task.reason


def test_environment_parks_without_blaming_the_code(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    caller.responses.update({"dsh:T-001": [BAD_ADD, BAD_ADD],
                             "claude:T-001": [supervise("environment",
                                                        explanation="python3 is missing")]})
    run = orchestrator.execute(planned.id)
    task = orchestrator.state.task(run.id, "T-001")
    assert task.status == "PARKED" and task.reason == "environment: python3 is missing"


def test_revised_tests_are_applied_and_the_saved_code_is_reverified(
    orch_repo: Path, math_plan: dict[str, object], clock: Clock, unsandboxed: None,
) -> None:
    """The designed test expects add(2, 3) == 6: correct code fails twice, the supervisor
    fixes the test with the spec in hand, and the saved candidate passes without a new build."""
    wrong = ("T-001", "test_add.py", ADD_TESTS.replace("add(2, 3), 5)", "add(2, 3), 6)"),
             ADD_CHECK.command)
    caller = scripted(math_plan, designs={"dsh:design:root": [component_design(wrong,
                                                                                DOUBLE_SHARD)]})
    fix = supervise("revise_tests", explanation="R1 says add(2, 3) is 5",
                    test_edits=[{"path": "test_add.py", "search": "add(2, 3), 6)",
                                 "replace": "add(2, 3), 5)"}])
    caller.responses.update({"dsh:T-001": [GOOD_ADD, GOOD_ADD], "claude:T-001": [fix],
                             "dsh:T-002": [GOOD_DOUBLE]})
    orchestrator = Orchestrator(orch_repo, caller, local_runner, notify=lambda _: None,
                                clock=clock)
    planned = orchestrator.plan("Build add and double.")
    run = orchestrator.execute(planned.id)
    assert run.status == "done", orchestrator.gate_failure
    task = orchestrator.state.task(run.id, "T-001")
    assert task.status == "DONE" and task.attempts == 4  # a3 supervised, a4 reverified a2
    assert caller.keys().count("dsh:T-001") == 2  # two builds
    assert len(events(orchestrator, run.id, "plan.tests_revised")) == 1
    files = {f["path"]: f["content"] for f in orchestrator.state.run(run.id).plan["files"]}  # type: ignore[index, union-attr]
    assert "add(2, 3), 5)" in files["test_add.py"]


def test_a_test_revision_that_passes_on_the_stubs_is_refused(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    vacuous = supervise("revise_tests", explanation="loosen",
                        test_edits=[{"path": "test_add.py",
                                     "search": "self.assertEqual(add(2, 3), 5)",
                                     "replace": "self.assertTrue(True)"}])
    caller.responses.update({"dsh:T-001": [BAD_ADD, BAD_ADD],
                             "claude:T-001": [vacuous, SUPERVISE_GIVE_UP]})
    run = orchestrator.execute(planned.id)
    task = orchestrator.state.task(run.id, "T-001")
    assert task.status == "PARKED" and task.supervisions == 2
    assert not events(orchestrator, run.id, "plan.tests_revised")
    refused = [e for e in events(orchestrator, run.id, "task.failed", "T-001")
               if e.data["category"] == "supervisor"]
    assert refused and "on the stubs" in str(refused[0].data["report"])


def test_revised_contract_reaches_the_next_attempt(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    clearer = supervise("revise_contract", contract="add(a, b) returns a + b, never a - b",
                        guidance="the sign was wrong")
    caller.responses.update({"dsh:T-001": [BAD_ADD, BAD_ADD, GOOD_ADD],
                             "claude:T-001": [clearer], "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    assert run.status == "done", orchestrator.gate_failure
    third = [text for _, key, text in caller.calls if key == "dsh:T-001"][2]
    assert "never a - b" in third and "the sign was wrong" in third
    assert orchestrator.state.task(run.id, "T-001").contract.endswith("never a - b")


def test_split_replaces_the_task_and_its_dependents_under_mechanical_lint_only(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    """T-002 depends on T-001, so a split replaces both; the lint is only structure:
    disjoint ownership, acyclic dependencies, the same checks, fresh ids."""
    adder = {"id": "T-003", "goal": "add, smaller", "reqs": ["R1"], "files": ["adder.py"],
             "depends_on": [], "interface_depends_on": [], "tests": [ADD_TEST],
             "contract": "add(a, b) == a + b"}
    doubler = {"id": "T-004", "goal": "double", "reqs": ["R2"], "files": ["doubler.py"],
               "depends_on": ["T-003"], "interface_depends_on": [], "tests": [DOUBLE_TEST],
               "contract": "double(a) == add(a, a)"}
    incomplete = supervise("split", tasks=[adder])  # drops T-002's file and check
    good_split = supervise("split", tasks=[adder, doubler])
    caller.responses.update({"dsh:T-001": [BAD_ADD, BAD_ADD],
                             "claude:T-001": [incomplete, good_split],
                             "dsh:T-003": [GOOD_ADD], "dsh:T-004": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    assert run.status == "done", orchestrator.gate_failure
    statuses = {t.id: t.status for t in orchestrator.state.tasks(run.id)}
    assert statuses == {"T-001": "REPLANNED", "T-002": "REPLANNED", "T-003": "DONE",
                        "T-004": "DONE"}
    refused = [e for e in events(orchestrator, run.id, "task.failed", "T-001")
               if e.data["category"] == "supervisor"]
    assert refused and "split rejected" in str(refused[0].data["report"])
    prompt = next(text for _, key, text in caller.calls if key == "claude:T-001")
    assert '## REPLACE (for split)\n["T-001","T-002"]' in prompt


def test_blocked_and_disputes_go_straight_to_the_supervisor(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    dispute = edit_result("adder.py", "", status="TEST_DISPUTE")
    dispute = NativeResult("ok", {**dispute.data, "edits": [],  # type: ignore[dict-item]
                                  "reason": "R1 says 5 but the test wants 6"})
    caller.responses.update({"dsh:T-001": [dispute, GOOD_ADD],
                             "claude:T-001": [SUPERVISE_RETRY], "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    assert run.status == "done", orchestrator.gate_failure
    prompt = next(text for _, key, text in caller.calls if key == "claude:T-001")
    assert "BUILDER'S REPORT" in prompt and "TEST_DISPUTE: R1 says 5" in prompt
    assert caller.keys()[3:6] == ["dsh:T-001", "claude:T-001", "dsh:T-001"]


def test_supervision_is_bounded(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    caller.responses.update({"dsh:T-001": [BAD_ADD] * (MAX_SUPERVISIONS + 2),
                             "claude:T-001": [SUPERVISE_RETRY] * MAX_SUPERVISIONS})
    run = orchestrator.execute(planned.id)
    task = orchestrator.state.task(run.id, "T-001")
    assert task.status == "PARKED" and task.supervisions == MAX_SUPERVISIONS
    assert f"{MAX_SUPERVISIONS} supervisor decisions" in task.reason
    assert caller.keys().count("claude:T-001") == MAX_SUPERVISIONS


def test_the_person_can_still_take_over_after_the_supervisor_gives_up(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    caller.responses.update({"dsh:T-001": [BAD_ADD, BAD_ADD], "claude:T-001": [SUPERVISE_GIVE_UP]})
    run = orchestrator.execute(planned.id)
    parked = orchestrator.state.task(run.id, "T-001")
    assert parked.status == "PARKED"
    hinted = orchestrator.state.message(run.id, "T-001", "use a + b")
    assert hinted.status == "READY" and hinted.level == 0
    with pytest.raises(Parked):  # bounded supervision also protects a resumed run
        orchestrator._supervise(run.id, replace(hinted, supervisions=MAX_SUPERVISIONS), "")


def test_the_supervisor_can_investigate_before_deciding(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    """Thin evidence (a test that hid stderr) is no reason to guess: the supervisor runs a
    command in the sandbox on the candidate and decides with its output in hand."""
    look = supervise("investigate", command="cat adder.py; echo probe-$((40+2))")
    caller.responses.update({"dsh:T-001": [BAD_ADD, BAD_ADD, GOOD_ADD],
                             "claude:T-001": [look, SUPERVISE_RETRY],
                             "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    assert run.status == "done", orchestrator.gate_failure
    prompts = [text for _, key, text in caller.calls if key == "claude:T-001"]
    assert len(prompts) == 2 and "## INVESTIGATION 1" not in prompts[0]
    assert "## INVESTIGATION 1" in prompts[1] and "probe-42" in prompts[1]
    assert "return a - b" in prompts[1].split("## INVESTIGATION 1")[1]  # the candidate's tree
    seen = events(orchestrator, run.id, "task.investigated", "T-001")
    assert len(seen) == 1 and seen[0].data["exit"] == 0
    assert orchestrator.state.task(run.id, "T-001").supervisions == 1  # looking is free


def test_endless_investigation_is_a_failed_supervision(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    from orchestragent.orch_supervisor import MAX_INVESTIGATIONS
    look = supervise("investigate", command="true")
    caller.responses.update({"dsh:T-001": [BAD_ADD, BAD_ADD],
                             "claude:T-001": [look] * (MAX_INVESTIGATIONS + 1)
                             + [SUPERVISE_GIVE_UP]})
    run = orchestrator.execute(planned.id)
    task = orchestrator.state.task(run.id, "T-001")
    assert task.status == "PARKED"
    refused = [e for e in events(orchestrator, run.id, "task.failed", "T-001")
               if e.data["category"] == "supervisor"]
    assert refused and "kept investigating" in str(refused[0].data["report"])


def test_the_supervisor_may_repair_the_command_that_runs_the_tests(
    orch_repo: Path, math_plan: dict[str, object], clock: Clock, unsandboxed: None,
) -> None:
    """The designer's check command is wrong (an unknown flag; on Meridian a javac line
    without the junit classpath). It is red on the stubs for the wrong reason and red on
    correct code too. The supervisor repairs the command; the repaired check must still fail
    on the stubs; the saved candidate is then verified without a new build."""
    broken = ("T-001", "test_add.py", ADD_TESTS, ADD_CHECK.command + " --no-such-flag")
    caller = scripted(math_plan, designs={"dsh:design:root": [component_design(broken,
                                                                                DOUBLE_SHARD)]})
    fix = supervise("revise_tests", explanation="the runner's flag does not exist",
                    command_edits=[{"search": " --no-such-flag", "replace": ""}])
    caller.responses.update({"dsh:T-001": [GOOD_ADD, GOOD_ADD], "claude:T-001": [fix],
                             "dsh:T-002": [GOOD_DOUBLE]})
    orchestrator = Orchestrator(orch_repo, caller, local_runner, notify=lambda _: None,
                                clock=clock)
    planned = orchestrator.plan("Build add and double.")
    assert "--no-such-flag" in orchestrator.state.task(planned.id, "T-001").tests[0]
    run = orchestrator.execute(planned.id)
    assert run.status == "done", orchestrator.gate_failure
    task = orchestrator.state.task(run.id, "T-001")
    assert task.status == "DONE" and task.tests == [ADD_CHECK.id]
    assert caller.keys().count("dsh:T-001") == 2  # the saved code was reverified, not rebuilt
    plan_tasks = {t["id"]: t for t in orchestrator.state.run(run.id).plan["tasks"]}  # type: ignore[index, union-attr]
    assert plan_tasks["T-001"]["tests"] == [ADD_CHECK.id]


def test_a_command_edit_that_matches_nothing_is_refused(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    nothing = supervise("revise_tests", explanation="x",
                        command_edits=[{"search": "--absent", "replace": ""}])
    caller.responses.update({"dsh:T-001": [BAD_ADD, BAD_ADD],
                             "claude:T-001": [nothing, SUPERVISE_GIVE_UP],
                             "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    task = orchestrator.state.task(run.id, "T-001")
    assert task.status == "PARKED"
    failed = [e for e in orchestrator.state.events(run.id, "T-001") if e.kind == "task.failed"]
    assert any("matches no check command" in str(e.data) for e in failed)


def test_the_supervisor_may_fix_another_tasks_test_file_that_breaks_this_one(
    orch_repo: Path, math_plan: dict[str, object], clock: Clock, unsandboxed: None,
) -> None:
    """Polyglot-ledger r03a94547: one task's Go test file did not compile, so every check of
    the package failed, including other tasks'. The supervisor of a blocked task named the
    file but could only edit its own tests, and the run parked. Here T-002's fixture module
    is broken and T-001's tests import it: T-001's supervisor repairs T-002's file, and both
    tasks' checks must still fail on the stubs."""
    broken = "VALUES = (1, 2\n"  # a syntax error
    add_tests = ADD_TESTS.replace("from adder import add\n",
                                  "from adder import add\nimport shared_fixture  # noqa\n")
    design = NativeResult("ok", {"shards": [
        {"task": "T-001", "files": [{"path": "test_add.py", "content": add_tests}],
         "command": ADD_CHECK.command},
        {"task": "T-002", "files": [{"path": "test_double.py", "content": DOUBLE_TESTS},
                                    {"path": "shared_fixture.py", "content": broken}],
         "command": "python3 -m unittest -q test_double"}]})
    caller = scripted(math_plan, designs={"dsh:design:root": [design]})
    fix = supervise("revise_tests", explanation="shared_fixture.py does not parse",
                    test_edits=[{"path": "shared_fixture.py", "search": "VALUES = (1, 2\n",
                                 "replace": "VALUES = (1, 2)\n"}])
    caller.responses.update({"dsh:T-001": [GOOD_ADD, GOOD_ADD], "claude:T-001": [fix],
                             "dsh:T-002": [GOOD_DOUBLE]})
    orchestrator = Orchestrator(orch_repo, caller, local_runner, notify=lambda _: None,
                                clock=clock)
    run = orchestrator.execute(orchestrator.plan("Build add and double.").id)
    assert run.status == "done", orchestrator.gate_failure
    assert orchestrator.state.task(run.id, "T-001").status == "DONE"
    assert caller.keys().count("dsh:T-001") == 2  # the saved code was reverified
    files = {f["path"]: f["content"] for f in orchestrator.state.run(run.id).plan["files"]}  # type: ignore[index, union-attr]
    assert files["shared_fixture.py"] == "VALUES = (1, 2)\n"


def test_a_cross_task_revision_that_makes_the_other_task_pass_on_stubs_is_refused(
    orch_repo: Path, math_plan: dict[str, object], clock: Clock, unsandboxed: None,
) -> None:
    """Editing another task's test is held to that task's red check too."""
    design = NativeResult("ok", {"shards": [
        {"task": "T-001", "files": [{"path": "test_add.py", "content": ADD_TESTS.replace(
            "add(2, 3), 5)", "add(2, 3), 6)")}], "command": ADD_CHECK.command},
        {"task": "T-002", "files": [{"path": "test_double.py", "content": DOUBLE_TESTS}],
         "command": DOUBLE_CHECK.command}]})
    caller = scripted(math_plan, designs={"dsh:design:root": [design]})
    vacuous = supervise("revise_tests", explanation="loosen double",
                        test_edits=[{"path": "test_double.py",
                                     "search": "self.assertEqual(double(4), 8)",
                                     "replace": "self.assertTrue(True)"}])
    caller.responses.update({"dsh:T-001": [GOOD_ADD, GOOD_ADD],
                             "claude:T-001": [vacuous, SUPERVISE_GIVE_UP]})
    orchestrator = Orchestrator(orch_repo, caller, local_runner, notify=lambda _: None,
                                clock=clock)
    run = orchestrator.execute(orchestrator.plan("Build add and double.").id)
    failed = [e for e in orchestrator.state.events(run.id, "T-001") if e.kind == "task.failed"]
    assert any("pass against the stubs" in str(e.data) for e in failed)
    assert orchestrator.state.task(run.id, "T-001").status == "PARKED"


def test_a_revision_that_breaks_merged_tasks_is_rolled_back_and_judged_again(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    """The revision is gated like a merge. Failing it used to park the task; now the
    integration branch is restored and the supervisor decides again with the report."""
    BAD_DOUBLE = edit_result("doubler.py", "return a")
    breaking = supervise("revise_tests", explanation="tighten add",
                         test_edits=[{"path": "test_add.py", "search": "add(2, 3), 5)",
                                      "replace": "add(2, 3), 6)"}])
    caller.responses.update({"dsh:T-001": [GOOD_ADD], "dsh:T-002": [BAD_DOUBLE, BAD_DOUBLE],
                             "claude:T-002": [breaking, SUPERVISE_GIVE_UP]})
    run = orchestrator.execute(planned.id)
    task = orchestrator.state.task(run.id, "T-002")
    assert task.status == "PARKED" and task.supervisions == 2
    failed = [e for e in orchestrator.state.events(run.id, "T-002") if e.kind == "task.failed"]
    assert any("breaks merged tasks' checks" in str(e.data.get("report")) for e in failed)
    files = {f["path"]: f["content"] for f in orchestrator.state.run(run.id).plan["files"]}  # type: ignore[index, union-attr]
    assert "add(2, 3), 5)" in files["test_add.py"]  # the plan kept the original test
    head = orchestrator.git(orchestrator.integration(run.id), "show", "HEAD:test_add.py")
    assert "add(2, 3), 5)" in head  # and so did the integration branch
    second = [text for _, key, text in caller.calls if key == "claude:T-002"][1]
    assert "breaks merged tasks" in second


def test_a_test_revision_that_changes_nothing_is_judged_again_not_parked(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    """Polyglot-ledger (after-step7c): the supervisor meant the six characters \\u2028 but
    JSON decoded them to the character itself, so search and replace were identical; the
    empty commit failed and the task parked as an infrastructure fault."""
    same = supervise("revise_tests", explanation="escape it",
                     test_edits=[{"path": "test_add.py", "search": "add(2, 3), 5)",
                                  "replace": "add(2, 3), 5)"}])
    caller.responses.update({"dsh:T-001": [BAD_ADD, BAD_ADD],
                             "claude:T-001": [same, SUPERVISE_GIVE_UP]})
    run = orchestrator.execute(planned.id)
    task = orchestrator.state.task(run.id, "T-001")
    assert task.status == "PARKED" and task.reason.startswith("supervisor: ")
    assert task.supervisions == 2
    second = [text for _, key, text in caller.calls if key == "claude:T-001"][1]
    assert "changed nothing" in second
