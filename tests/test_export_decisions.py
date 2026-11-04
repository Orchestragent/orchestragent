"""Supervisor decisions leave a record a training or evaluation set can be built from: the
evidence the supervisor read, its answer, and what the decision led to."""

import importlib.util
import json
import sys
from pathlib import Path

from orchestragent.orch_run import Orchestrator
from orchestragent.orch_state import Run
from tests.conftest import (
    BAD_ADD,
    GOOD_ADD,
    GOOD_DOUBLE,
    SUPERVISE_GIVE_UP,
    SUPERVISE_RETRY,
    ScriptedCaller,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_decisions.py"


def exporter():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("export_decisions", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_decisions"] = module
    spec.loader.exec_module(module)
    return module


def test_each_decision_is_exported_with_its_evidence_and_what_followed(
    orch_repo: Path, orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    caller.responses.update({"dsh:T-001": [BAD_ADD, BAD_ADD, GOOD_ADD],
                             "claude:T-001": [SUPERVISE_RETRY], "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    assert run.status == "done", orchestrator.gate_failure
    kept = list((orchestrator.run_dir(run.id) / "supervision").glob("T-001-s1-*.json"))
    assert len(kept) == 1
    record = json.loads(kept[0].read_text())
    assert "## LAST FAILURE" in record["prompt"]["dynamic"]
    assert record["answer"]["action"] == "retry"

    module = exporter()
    rows = [row for db in module.databases(orch_repo) for row in module.decisions(db)]
    assert len(rows) == 1
    row = rows[0]
    assert (row["task"], row["action"], row["supervision"]) == ("T-001", "retry", 1)
    assert row["applied"] and row["next"] == {"kind": "done"} and row["task_final"] == "DONE"
    assert row["prompt"]["shared"] and "return a - b" in row["prompt"]["dynamic"]
    assert row["acceptance"] is None  # not a gate run
    assert "| retry | 1 | 1 | 1 | 1 | 0 | 1 |" in module.summary(rows)


def test_a_decision_that_did_not_resolve_points_at_the_next_one(
    orch_repo: Path, orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    caller.responses.update({"dsh:T-001": [BAD_ADD, BAD_ADD, BAD_ADD],
                             "claude:T-001": [SUPERVISE_RETRY, SUPERVISE_GIVE_UP],
                             "dsh:T-002": [GOOD_DOUBLE]})
    orchestrator.execute(planned.id)
    module = exporter()
    rows = [row for db in module.databases(orch_repo) for row in module.decisions(db)]
    assert [(r["action"], r["next"]["kind"]) for r in rows] == [
        ("retry", "supervision"), ("give_up", "parked")]
    assert all(r["task_final"] == "PARKED" for r in rows)
