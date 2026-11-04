"""Models look at the repository themselves: the planner before planning, builders during an
attempt. Requests are answered from git at the working revision; nothing is precomputed."""

from typing import cast

from orchestragent.native import NativeResult
from orchestragent.orch_run import Orchestrator
from orchestragent.orch_state import Run
from tests.conftest import BAD_ADD, GOOD_ADD, GOOD_DOUBLE, ScriptedCaller, edit_result


def test_the_planner_may_look_around_before_planning(
    orchestrator: Orchestrator, caller: ScriptedCaller,
) -> None:
    caller.responses["claude:look"] = [
        NativeResult("ok", {"requests": [{"find": "*.md"}, {"search": "demo"}],
                            "note": "a tiny repo"}),
        NativeResult("ok", {"requests": [], "note": "seen enough"}),
    ]
    run = orchestrator.plan("Build add and double.")
    assert run.status == "planned"
    looks = [text for _, key, text in caller.calls if key == "claude:look"]
    assert len(looks) == 2 and "REPOSITORY (tracked files and sizes)" in looks[0]
    assert "$ find *.md" in looks[1] and "README.md" in looks[1] and "a tiny repo" in looks[1]
    boundary = next(text for _, key, text in caller.calls if key == "plan")
    assert "WHAT THE PLANNER SAW" in boundary and "seen enough" in boundary
    assert "$ search demo" in boundary


def test_a_builder_asks_for_context_and_gets_it_at_its_revision(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    ask = NativeResult("ok", {"status": "NEED_CONTEXT", "summary": "", "reason": "",
                              "requests": [{"read": "doubler.py"}, {"search": "def add"}],
                              "edits": []})
    caller.responses.update({"dsh:T-001": [ask, GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    assert run.status == "done", orchestrator.gate_failure
    builds = [text for _, key, text in caller.calls if key == "dsh:T-001"]
    assert len(builds) == 2
    assert "CONTEXT YOU ASKED FOR" not in builds[0]
    answered = builds[1].split("CONTEXT YOU ASKED FOR")[1]
    assert "$ read doubler.py" in answered and "def double" in answered
    assert "$ search def add" in answered and "adder.py:" in answered
    assert "REPOSITORY (tracked files and sizes)" in builds[0] and "adder.py" in builds[0]
    task = orchestrator.state.task(run.id, "T-001")
    assert task.context_requests == 1 and len(task.context_selectors) == 2


def test_repeating_a_request_is_refused_once_then_fails_the_attempt(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    ask = NativeResult("ok", {"status": "NEED_CONTEXT", "summary": "", "reason": "",
                              "requests": [{"read": "doubler.py"}], "edits": []})
    caller.responses.update({"dsh:T-001": [ask, ask, ask, BAD_ADD, GOOD_ADD],
                             "claude:T-001": [], "dsh:T-002": [GOOD_DOUBLE]})
    from tests.conftest import SUPERVISE_RETRY
    caller.responses["claude:T-001"] = [SUPERVISE_RETRY]
    run = orchestrator.execute(planned.id)
    assert run.status == "done", orchestrator.gate_failure
    builds = [text for _, key, text in caller.calls if key == "dsh:T-001"]
    assert "Do not request it again" in builds[2]  # the repeat was pointed out once
    failed = [e for e in orchestrator.state.events(run.id, "T-001") if e.kind == "task.failed"]
    assert failed and "repeated context" in str(failed[0].data["report"])


def test_dependency_files_are_shown_up_front_as_read_requests(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    caller.responses.update({"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    assert run.status == "done", orchestrator.gate_failure
    second = next(text for _, key, text in caller.calls if key == "dsh:T-002")
    assert "$ read adder.py" in second and "return a + b" in second  # T-001's merged file
    assert cast(str, edit_result("adder.py", "x").data["status"]) == "DONE"


def test_a_spent_context_budget_fails_the_attempt_instead_of_parking(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    """Three context rounds per task; a fourth request fails the attempt and the ladder goes
    on (cheap retry, then the supervisor) instead of parking the task outright."""
    def ask(path: str) -> NativeResult:
        return NativeResult("ok", {"status": "NEED_CONTEXT", "summary": "", "reason": "",
                                   "requests": [{"read": path}], "edits": []})
    caller.responses.update({
        "dsh:T-001": [ask("README.md"), ask("adder.py"), ask("doubler.py"),
                      ask("test_add.py"), GOOD_ADD],
        "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    assert run.status == "done", orchestrator.gate_failure
    task = orchestrator.state.task(run.id, "T-001")
    assert task.status == "DONE" and task.context_requests == 3
    failed = [e for e in orchestrator.state.events(run.id, "T-001") if e.kind == "task.failed"]
    assert len(failed) == 1 and "context request limit (3) exhausted" in str(failed[0].data)
