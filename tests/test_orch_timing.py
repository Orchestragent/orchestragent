"""A run reports where its wall time went: plan, design, build, final gate, and calls."""

from orchestragent.orch_run import Orchestrator
from orchestragent.orch_state import Run
from orchestragent.orch_timing import stage_line, stages, state_db
from tests.conftest import GOOD_ADD, GOOD_DOUBLE, ScriptedCaller


def test_a_finished_run_reports_its_stages_and_call_time(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    caller.responses.update({"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    assert run.status == "done", orchestrator.gate_failure
    db = state_db(orchestrator.repo)
    assert db is not None
    timing = stages(db)
    for key in ("plan", "design", "build", "final"):
        assert timing[key] is not None and timing[key] >= 0, (key, timing)
    assert {"claude:plan", "dsh:build"} <= set(timing["calls"])
    assert timing["calls"]["dsh:build"]["n"] == 2
    assert stage_line(timing).count("/") == 3


def test_a_project_without_a_run_has_no_state(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert state_db(tmp_path) is None
