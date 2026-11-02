import copy
import sqlite3
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event
from typing import Any, cast

import pytest

from orchestragent.native import ContextPrompt, NativeResult
from orchestragent.orch_audit import audit
from orchestragent.orch_build import REQUIRED_TRAILERS, SPEC_AUTHORITY, EditError, parse_trailers
from orchestragent.orch_plan import dependency_errors, lint, tasks_from
from orchestragent.orch_review import Review
from orchestragent.orch_run import (
    DSF,
    DSF_EXTENDED,
    AttemptFailed,
    Candidate,
    Deferred,
    Draft,
    Orchestrator,
    Parked,
    PlanError,
    Route,
    Signal,
)
from orchestragent.orch_state import OrchEvent, OrchTask, Run, plan_hash
from orchestragent.orch_synthetic import Shape, synthetic_plan
from orchestragent.orch_usage import Call
from orchestragent.store import Conflict
from orchestragent.workspace import Workspace
from tests.conftest import (
    ADDER_STUB,
    BAD_ADD,
    EMPTY_REPLAN,
    GOOD_ADD,
    GOOD_DOUBLE,
    MATH_TESTS,
    PARKING,
    THICK,
    Clock,
    ScriptedCaller,
    edit_result,
    local_runner,
)


def statuses(orchestrator: Orchestrator, run: Run) -> dict[str, str]:
    return {task.id: task.status for task in orchestrator.state.tasks(run.id)}


@pytest.fixture
def context_read() -> NativeResult:
    return NativeResult("ok", {"status": "NEED_FILES", "edits": [],
                               "need_files": ["doubler.py::double"]},
                        usage={"input_tokens": 100, "output_tokens": 20, "settled_cents": 1})


@pytest.fixture
def large_owned_case(
    orch_repo: Path, math_plan: dict[str, object], large_owned_source: str,
) -> tuple[Orchestrator, ScriptedCaller]:
    (orch_repo / "adder.py").write_text(large_owned_source)
    Workspace.git(orch_repo, "add", "adder.py")
    Workspace.git(orch_repo, "commit", "-m", "existing large module")
    math_plan["files"] = [entry for entry in cast(list[dict[str, str]], math_plan["files"])
                          if entry["path"] != "adder.py"]
    caller = ScriptedCaller({"plan": [NativeResult("ok", math_plan)],
                             "dsh:T-002": [GOOD_DOUBLE]})
    return Orchestrator(orch_repo, caller, local_runner, notify=lambda _: None), caller


@pytest.mark.parametrize("read_full", [False, True])
def test_large_owned_context_edits_and_explicit_expansion_preserve_unseen_code(
    large_owned_case: tuple[Orchestrator, ScriptedCaller], large_owned_source: str,
    read_full: bool,
) -> None:
    orch, caller = large_owned_case
    corrected = large_owned_source.replace("a - b", "a + b")
    caller.responses["dsh:T-001"] = ([
        NativeResult("ok", {"status": "NEED_FILES", "edits": [], "need_files": ["adder.py"]}),
        edit_result("adder.py", corrected, search=""),
    ] if read_full else [edit_result("adder.py", "return a + b + helper()",
                                     search="return a - b + helper()")])
    run = orch.execute(orch.plan("Correct add and implement double").id, jobs=1)
    assert run.status == "done", run.message
    actual = Workspace.git(orch.repo, "show", f"{run.verified_head}:adder.py", strip=False)
    assert actual == corrected
    prompts = [p for _, key, p in caller.calls if key == "dsh:T-001"]
    assert "context ballast" not in prompts[0] and len(prompts[0]) < len(large_owned_source) // 2
    assert len(prompts) == (2 if read_full else 1)
    if read_full:
        assert "context ballast" in prompts[-1]
    evidence = [e.data["partial_owned"] for e in orch.state.events(run.id, "T-001")
                if e.kind == "attempt.called"]
    assert evidence == ([["adder.py"], []] if read_full else [["adder.py"]])
    assert audit(orch, run.id, run_tests=False) == []


def test_context_read_is_pinned_billed_and_does_not_spend_a_failure_rung(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
    context_read: NativeResult,
) -> None:
    caller.responses.update({"dsh:T-001": [context_read, GOOD_ADD],
                             "dsh:T-002": [GOOD_DOUBLE]})
    result = orchestrator.execute(planned.id)
    task = orchestrator.state.task(planned.id, "T-001")
    receipts = [c for c in orchestrator.state.usage.calls(planned.id) if c.task_id == task.id]
    assert (result.status, task.attempts, task.level, task.context_requests) == ("done", 1, 0, 1)
    assert len(receipts) == 2 and len({c.base_revision for c in receipts}) == 1
    assert sum(int(str(c.usage["settled_cents"])) for c in receipts) == 2
    assert "doubler.py::double" in caller.calls[2][2]
    assert "class DoubleTest" not in caller.calls[1][2].split("## FROZEN TESTS", 1)[1]
    assert orchestrator.state.usage.calls(planned.id, limit=2) == (
        orchestrator.state.usage.calls(planned.id)[-2:])


def test_oversized_file_read_refines_catalog_to_definition_without_restarting_task(
    orchestrator: Orchestrator, caller: ScriptedCaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orchestragent.orch_run.DSF_BUDGET", 39919)
    (orchestrator.repo / "helper.py").write_text(
        "import inspect\nNOISE = '" + "x" * 45000 + "'\ndef useful():\n    return 7\n")
    Workspace.git(orchestrator.repo, "add", "helper.py")
    Workspace.git(orchestrator.repo, "commit", "-m", "large read-only dependency")
    run = orchestrator.plan("Add and double")
    reads = [NativeResult("ok", {"status": "NEED_FILES", "edits": [], "need_files": [s]})
             for s in ("helper.py", "helper.py::useful")]
    caller.responses.update({"dsh:T-001": [*reads, GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    result = orchestrator.execute(run.id)
    task = orchestrator.state.task(run.id, "T-001")
    prompts = [prompt for _, key, prompt in caller.calls if key == "dsh:T-001"]
    assert result.status == "done" and task.attempts == 1 and task.context_requests == 2
    assert "signatures only" in prompts[1] and "return 7" not in prompts[1]
    assert "Exact definition" in prompts[2] and "return 7" in prompts[2]
    assert all("x" * 100 not in prompt for prompt in prompts)
    evidence = events(orchestrator, run.id, "context.request")
    assert evidence[-1].data["partial_files"] == ["helper.py"]


def test_context_beyond_old_byte_limit_stays_on_dsf_and_remains_complete(
    orchestrator: Orchestrator, caller: ScriptedCaller,
) -> None:
    source = "def useful():\n" + "    # required context\n" * 2200 + "    return 7\n"
    (orchestrator.repo / "helper.py").write_text(source)
    Workspace.git(orchestrator.repo, "add", "helper.py")
    Workspace.git(orchestrator.repo, "commit", "-m", "large exact definition")
    run = orchestrator.plan("Add and double")
    caller.responses.update({
        "dsh:T-001": [NativeResult("ok", {"status": "NEED_FILES", "edits": [],
                                         "need_files": ["helper.py::useful"]}), GOOD_ADD],
        "dsh:T-002": [GOOD_DOUBLE],
    })
    result = orchestrator.execute(run.id)
    task = orchestrator.state.task(run.id, "T-001")
    prompts = [p for _, key, p in caller.calls if key == "dsh:T-001"]
    assert (result.status, task.attempts, task.level) == ("done", 1, 0)
    assert len(prompts) == 2 and "claude:T-001" not in caller.keys()
    assert source in prompts[1]
    assert not events(orchestrator, run.id, "task.failed", "T-001")


@pytest.mark.parametrize(("used", "level", "expected"), [
    ([], 0, 5), ([0, 1, 2], 4, 5), ([0, 3], 3, 5), ([0, 3, 5], 5, 6),
])
def test_context_overflow_escalation_is_bounded_and_does_not_train_failure_rate(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
    used: list[int], level: int, expected: int,
) -> None:
    orchestrator.state.transition(planned.id, "T-001", "READY", "task.ready", {})
    orchestrator.state.transition(planned.id, "T-001", "RUNNING", "task.started", {},
                                  attempts=1, level=level, used_levels=used)
    orchestrator._failed(planned.id, "T-001", 1, level, AttemptFailed("context", "too large"))
    task = orchestrator.state.task(planned.id, "T-001")
    assert (task.status, task.level) == (
        ("PARKED", level) if expected == 6 else ("READY", expected))
    assert caller.keys() == ["plan"]
    assert not events(orchestrator, planned.id, "attempt.outcome", "T-001")


def test_repeated_context_requests_spend_no_context_round_and_fail_only_the_attempt(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
    context_read: NativeResult,
) -> None:
    caller.responses.update({"dsh:T-001": [context_read] * 3 + [GOOD_ADD],
                             "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    task = orchestrator.state.task(run.id, "T-001")
    # read, repeat (answered in the attempt), repeat (fails a1), then a2 on the next rung
    assert run.status == "done"
    assert (task.status, task.context_requests, task.attempts, task.level) == ("DONE", 1, 2, 1)
    assert caller.keys()[:5] == ["plan", *["dsh:T-001"] * 4]


def test_context_read_honors_spend_cap_before_the_followup_call(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
    context_read: NativeResult,
) -> None:
    caller.responses["dsh:T-001"] = [context_read]
    run = orchestrator.execute(planned.id, max_cents=1)
    assert run.status == "paused" and caller.keys() == ["plan", "dsh:T-001"]
    assert orchestrator.state.task(run.id, "T-001").context_requests == 1


def test_context_request_limit_is_atomic_and_survives_reopening(
    orchestrator: Orchestrator, planned: Run,
) -> None:
    def read(number: int) -> bool:
        try:
            orchestrator.state.request_context(planned.id, "T-001", [f"{number}.py"], planned.base)
            return True
        except Conflict:
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        accepted = list(pool.map(read, range(6)))
    reopened = type(orchestrator.state)(orchestrator.state.store.path)
    assert sum(accepted) == reopened.task(planned.id, "T-001").context_requests == 3
    with pytest.raises(Conflict, match="request limit"):
        reopened.request_context(planned.id, "T-001", [], planned.base)


def test_parallel_samples_share_a_read_without_spending_another_round(
    orchestrator: Orchestrator, planned: Run,
) -> None:
    def read(_: int) -> int:
        return orchestrator.state.request_context(
            planned.id, "T-001", ["adder.py"], planned.base, observed_requests=0,
        ).context_requests

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(read, range(4))) == [1, 1, 1, 1]
    assert len(events(orchestrator, planned.id, "context.shared")) == 3
    with pytest.raises(Conflict, match="no progress"):
        orchestrator.state.request_context(planned.id, "T-001", ["adder.py::add"], planned.base,
                                           observed_requests=1)


@pytest.mark.parametrize(("reads", "edits"), [
    ("adder.py", []), (["../adder.py"], []), (["adder.py"] * 9, []),
    (["adder.py"], [{"path": "adder.py", "search": "", "replace": "unauthorized"}]),
])
def test_invalid_context_requests_are_bounded_and_receive_feedback(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run, reads: object,
    edits: object,
) -> None:
    invalid = NativeResult("ok", {"status": "NEED_FILES", "edits": edits, "need_files": reads})
    caller.responses.update({"dsh:T-001": [invalid, GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    result = orchestrator.execute(planned.id)
    evidence = events(orchestrator, planned.id, "context.request")
    assert result.status == "done" and evidence[0].data["error"]
    assert evidence[0].data["call_id"] and evidence[0].data["selectors"] == []
    assert orchestrator.state.task(planned.id, "T-001").context_requests == 1


@pytest.mark.parametrize("change", [
    {"total_tokens": -1}, {"served_model": "invalid model name"}, {"model": "other-model"},
])
def test_malformed_usage_is_not_valid_cache_evidence_after_persistence(
    orchestrator: Orchestrator, planned: Run, clock: Clock,
    change: dict[str, object],
) -> None:
    def invalid(*args: Any, **kwargs: Any) -> NativeResult:
        clock.now += 1
        return NativeResult("ok", {}, usage={"input_tokens": 20, "output_tokens": 10,
            "cache_read_input_tokens": 80, "cache_creation_input_tokens": 0, **change})

    orchestrator.call = invalid
    orchestrator._call_model(planned.id, DSF, "prompt", {}, "invalid-cache",
                             purpose="build", prefix="same-prefix")
    receipt = orchestrator.state.usage.calls(planned.id)[-1]
    assert receipt.usage_valid is False
    assert orchestrator.cache_policy.affinity((DSF.provider, DSF.model, "same-prefix")) == 0


def events(orchestrator: Orchestrator, run_id: str, kind: str, task: str = "") -> list[OrchEvent]:
    return [e for e in orchestrator.state.events(run_id, task) if e.kind == kind]


def test_recovery_preserves_unknown_calls_and_requires_explicit_retry(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run, unfinished_call: Call,
) -> None:
    result = orchestrator.execute(planned.id)
    assert result.status == "parked" and caller.keys() == ["plan"]
    assert orchestrator.state.task(planned.id, "T-001").status == "PARKED"
    assert any(call.id == unfinished_call.id and call.finished_at is None
               for call in orchestrator.state.usage.calls(planned.id))
    with pytest.raises(Conflict, match="--ack-unknown"):
        orchestrator.state.retry(planned.id, "T-001")
    orchestrator.state.retry(planned.id, "T-001", ack_unknown=True)
    caller.responses.update({"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    assert orchestrator.execute(planned.id).status == "done"
    assert events(orchestrator, planned.id, "task.retry")[0].data["acknowledged_calls"] == [
        unfinished_call.id]


def test_concurrent_dependency_handoffs_cannot_commit_a_cycle(
    orchestrator: Orchestrator, independent_run: Run,
) -> None:
    state, run_id = orchestrator.state, independent_run.id
    for task in state.tasks(run_id):
        state.transition(run_id, task.id, "READY", "task.ready", {})
        state.transition(run_id, task.id, "RUNNING", "task.started", {}, attempts=1)
        state.transition(run_id, task.id, "VERIFYING", "task.verifying", {})
    barrier = Barrier(2)

    def handoff(pair: tuple[str, str]) -> bool:
        barrier.wait(timeout=5)
        try:
            state.wait_for(run_id, pair[0], 1, [pair[1]], "import", validate=dependency_errors)
            return True
        except Conflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = list(pool.map(handoff, [("T-001", "T-002"), ("T-002", "T-001")]))
    assert sum(accepted) == 1
    assert dependency_errors({t.id: t.depends_on for t in state.tasks(run_id)}) == []


@pytest.mark.parametrize("dependency", ["T-001", "T-002", "missing"])
def test_invalid_saved_graph_stops_before_model_dispatch(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run, dependency: str,
) -> None:
    orchestrator.state.transition(planned.id, "T-001", "PLANNED", "test.graph", {},
                                  depends_on=[dependency])
    result = orchestrator.execute(planned.id)
    assert result.status == "parked" and "Invalid dependency graph" in result.message
    assert caller.keys() == ["plan"]


@pytest.mark.parametrize("outcome", ["timeout", "failed"])
def test_unknown_provider_exposure_stops_the_ladder(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run, outcome: str,
) -> None:
    caller.responses["dsh:T-001"] = [NativeResult(outcome, {}, usage={
        "forwarded": 1, "reserved_cents": 300,
    })]
    result = orchestrator.execute(planned.id)
    task = orchestrator.state.task(planned.id, "T-001")
    assert (result.status, task.status, task.level) == ("paused", "PARKED", 0)
    assert caller.keys() == ["plan", "dsh:T-001"]


def test_cancellation_after_dispatch_cannot_silently_repeat_the_unknown_call(
    orchestrator: Orchestrator, planned: Run,
) -> None:
    calls = 0

    def interrupted(*args: Any, **kwargs: Any) -> NativeResult:
        nonlocal calls
        calls += 1
        orchestrator.cancel.set()
        return NativeResult("cancelled", {}, usage={"forwarded": 1, "reserved_cents": 300})

    orchestrator.call = interrupted
    assert orchestrator.execute(planned.id).status == "paused"
    assert orchestrator.state.task(planned.id, "T-001").status == "PARKED"
    assert orchestrator.execute(planned.id).status == "parked" and calls == 1


def test_scheduler_fault_cancels_dispatch_and_resumes_without_ghost_slots(
    orchestrator: Orchestrator, planned: Run, caller: ScriptedCaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = orchestrator._attempt

    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError("worker infrastructure unavailable")

    monkeypatch.setattr(orchestrator, "_attempt", broken)
    paused = orchestrator.execute(planned.id, jobs=1)
    assert paused.status == "paused" and paused.message == "scheduler fault: OSError"
    assert caller.keys() == ["plan"]
    monkeypatch.setattr(orchestrator, "_attempt", original)
    caller.responses.update({"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    assert orchestrator.execute(planned.id, jobs=1).status == "done"


@pytest.mark.parametrize("fault", ["missing", "contract"])
def test_unusable_saved_candidates_park_without_model_calls(
    orchestrator: Orchestrator, planned: Run, caller: ScriptedCaller, fault: str,
) -> None:
    orchestrator.state.transition(planned.id, "T-001", "READY", "task.ready", {},
        pending_candidate="f" * 40 if fault == "missing" else planned.checkpoint)
    result = orchestrator.execute(planned.id)
    assert result.status == "parked" and caller.keys() == ["plan"]
    assert orchestrator.state.task(planned.id, "T-001").reason


def test_builder_route_is_pinned_to_deepseek_flash() -> None:
    assert (DSF.provider, DSF.model) == ("dsh", "deepseek-flash")


@pytest.fixture
def interface_case(
    orch_repo: Path, math_plan: dict[str, object],
) -> tuple[Orchestrator, ScriptedCaller]:
    cast(list[dict[str, object]], math_plan["tasks"])[1]["interface_depends_on"] = ["T-001"]
    caller = ScriptedCaller({"plan": [NativeResult("ok", math_plan)],
                             "dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    orch = Orchestrator(orch_repo, caller, local_runner, notify=lambda _: None)
    orch.early_interface_start = True  # these tests exercise the opt-in early build
    return orch, caller


@pytest.mark.parametrize("changes", [0, 1, 4])
def test_candidate_binds_dependency_contracts_and_checks_them_at_merge(
    interface_case: tuple[Orchestrator, ScriptedCaller], monkeypatch: pytest.MonkeyPatch,
    changes: int,
) -> None:
    orch, caller = interface_case
    caller.responses["dsh:T-002"] = [GOOD_DOUBLE] * min(changes + 1, 4)
    merge = orch._merge

    def change_contract(run: Run, task: OrchTask, candidate: Candidate, route: Route) -> None:
        if (task.id == "T-002"
                and len(events(orch, run.id, "task.inputs_changed", task.id)) < changes):
            upstream = orch.state.task(run.id, "T-001")
            orch.state.transition(run.id, upstream.id, upstream.status, "task.contract_changed", {},
                                  contract=upstream.contract + " New error semantics.")
        merge(run, task, candidate, route)

    monkeypatch.setattr(orch, "_merge", change_contract)
    run = orch.execute(orch.plan("Add and double").id)
    downstream = orch.state.task(run.id, "T-002")
    assert run.status == ("parked" if changes > 3 else "done")
    assert caller.keys().count("dsh:T-001") == 1
    assert caller.keys().count("dsh:T-002") == min(changes + 1, 4)
    revisions = events(orch, run.id, "task.inputs_changed", "T-002")
    assert len(revisions) == min(changes, 3)
    assert all(item.data["dependencies"] == ["T-001"] for item in revisions)
    assert downstream.input_revisions == [item.data["revision"] for item in revisions]
    assert audit(orch, run.id, run_tests=False) == []
    if changes > 3:
        assert "automatic refresh limit" in downstream.reason
        assert not downstream.commit
    else:
        trailers = parse_trailers(Workspace.git(orch.repo, "show", "-s", "--format=%B",
                                                downstream.commit))
        assert len(trailers["Orch-Inputs"]) == 64
        upstream = orch.state.task(run.id, "T-001")
        orch.state.transition(run.id, upstream.id, upstream.status, "task.contract_changed", {},
                              contract=upstream.contract + " Changed after saving.")
        with pytest.raises(Parked, match="dependency contract changed"):
            orch._candidate_identity(run, downstream, downstream.commit)


@pytest.mark.parametrize("previous", [["revision"], ["a", "b", "c"]])
def test_input_refresh_is_bounded_durably_and_rejected_without_partial_updates(
    orchestrator: Orchestrator, planned: Run, previous: list[str],
) -> None:
    state = orchestrator.state
    state.transition(planned.id, "T-002", "READY", "task.ready", {})
    task = state.transition(planned.id, "T-002", "RUNNING", "task.started", {},
                            attempts=1, pending_candidate="a" * 40, input_revisions=previous)
    count = len(state.events(planned.id))
    reopened = Orchestrator(orchestrator.repo, orchestrator.call, local_runner,
                           notify=lambda _: None)
    with pytest.raises(Conflict, match="automatic refresh limit"):
        reopened.state.refresh_inputs(planned.id, task.id, 1, "a" * 40, "revision", ["T-001"], "")
    assert state.task(planned.id, task.id) == task
    assert len(state.events(planned.id)) == count


def test_interface_pipeline_overlaps_generation_then_replays_once_after_dependency_merge(
    interface_case: tuple[Orchestrator, ScriptedCaller], monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch, caller = interface_case
    waiting = Event()

    def overlap(provider: str, model: str, prompt: str, schema: dict[str, object], run_id: str,
                cancelled: Callable[[], bool], *, effort: str = "") -> NativeResult:
        if "-T-001-a1-" in run_id:
            assert waiting.wait(10), "downstream did not build while upstream was generating"
        return caller(provider, model, prompt, schema, run_id, cancelled, effort=effort)

    orch.call = overlap
    handoff = orch.state.wait_for

    def signal_handoff(*args: Any, **kwargs: Any) -> None:
        handoff(*args, **kwargs)
        waiting.set()

    monkeypatch.setattr(orch.state, "wait_for", signal_handoff)
    run = orch.execute(orch.plan("Add and double").id, jobs=2)
    assert run.status == "done" and waiting.is_set()
    assert caller.keys().count("dsh:T-001") == caller.keys().count("dsh:T-002") == 1
    assert orch.state.task(run.id, "T-002").attempts == 1
    trace = orch.state.events(run.id)
    parked_code = next(e for e in trace if e.kind == "task.waits")
    upstream_done = next(e for e in trace if e.kind == "task.done" and e.task_id == "T-001")
    reused = next(e for e in trace if e.kind == "task.candidate_reused")
    assert parked_code.seq < upstream_done.seq < reused.seq
    assert not events(orch, run.id, "task.failed", "T-002")
    assert audit(orch, run.id, run_tests=False) == []


@pytest.mark.parametrize("blocked", [False, True])
def test_early_candidate_survives_restart_and_parked_upstream_without_more_generation(
    interface_case: tuple[Orchestrator, ScriptedCaller], blocked: bool,
) -> None:
    orch, caller = interface_case
    run = orch.plan("Add and double")
    orch._promote(run.id)
    task = orch.state.transition(run.id, "T-002", "RUNNING", "task.started", {}, attempts=1)
    orch._attempt(run.id, task)
    saved = orch.state.task(run.id, task.id)
    assert saved.status == "PLANNED" and saved.pending_candidate and saved.attempts == 0
    if blocked:
        orch.state.transition(run.id, "T-001", "PARKED", "task.parked", {}, reason="needs input")
    orch.release_worktrees(run.id)
    resumed = Orchestrator(orch.repo, caller, local_runner, notify=lambda _: None)
    finished = resumed.execute(run.id)
    if blocked:
        assert finished.status == "parked"
        assert resumed.state.task(run.id, task.id).pending_candidate == saved.pending_candidate
        assert not events(resumed, run.id, "task.candidate_reused")
        resumed.state.retry(run.id, "T-001")
        finished = resumed.execute(run.id)
    assert finished.status == "done"
    assert caller.keys().count("dsh:T-001") == caller.keys().count("dsh:T-002") == 1
    assert len(events(resumed, run.id, "task.candidate_reused", "T-002")) == 1
    assert audit(resumed, run.id, run_tests=False) == []


@pytest.mark.parametrize("proposal", [2, 99, 0])
def test_structure_gate_rejects_shadowed_code_before_sandbox_or_sampling(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
    monkeypatch: pytest.MonkeyPatch, proposal: int,
) -> None:
    duplicate = ADDER_STUB.replace("raise NotImplementedError", "return a + b") + ADDER_STUB
    caller.responses.update({"dsh:T-001": [edit_result("adder.py", duplicate, search=""), GOOD_ADD],
                             "dsh:T-002": [GOOD_DOUBLE]})
    verified: list[str] = []
    runner = orchestrator.runner

    def verify(cwd: Path, scratch: Path, revision: str, names: list[str], *,
               timeout: float = 120) -> tuple[bool, str]:
        verified.append(revision)
        return runner(cwd, scratch, revision, names, timeout=timeout)

    monkeypatch.setattr(orchestrator, "runner", verify)
    # Even an external classifier proposing sampling cannot bypass the eligibility gate.
    monkeypatch.setattr(orchestrator, "_triage", lambda *args: proposal)
    run = orchestrator.execute(planned.id, jobs=1)
    failed = events(orchestrator, run.id, "task.failed", "T-001")[0].data
    assert run.status == "done" and failed["category"] == "structure"
    assert failed["next"] == 1 and failed["sampleable"] is False
    assert failed["candidate"] not in verified
    assert caller.keys() == ["plan", "dsh:T-001", "dsh:T-001", "dsh:T-002"]
    assert events(orchestrator, run.id, "recovery.rejected", "T-001")
    assert audit(orchestrator, run.id, run_tests=False) == []


def test_discard_candidate_clears_pending_and_failed_artifacts(
    orchestrator: Orchestrator, planned: Run,
) -> None:
    state = orchestrator.state
    state.transition(planned.id, "T-001", "PARKED", "task.parked", {},
                     pending_candidate="a" * 40, failed_candidate="b" * 40)
    task = state.retry(planned.id, "T-001", discard_candidate=True)
    assert task.pending_candidate == task.failed_candidate == "" and task.level == 0


@pytest.mark.parametrize("response", [
    {"verdict": "tests_wrong", "contract": "bad mode", "edits": []},
    {"verdict": "contract", "contract": "clarify", "edits": [{"path": "test.py"}]},
    {"verdict": "contract", "contract": "", "edits": []},
])
def test_shared_exception_schema_does_not_grant_review_authority_to_thicken(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
    response: dict[str, object],
) -> None:
    caller.responses["claude:T-001"] = [NativeResult("ok", response)]
    task = orchestrator.state.task(planned.id, "T-001")
    with pytest.raises(AttemptFailed, match="only a clarified contract"):
        orchestrator._thicken(planned.id, task)
    assert orchestrator.state.task(planned.id, task.id).contract == task.contract


def test_happy_path_merges_tasks_in_dependency_order_with_trailers(
    orchestrator: Orchestrator, caller: ScriptedCaller, finished: Run, orch_repo: Path
) -> None:
    log = Workspace.git(orch_repo, "log", "--format=%H", finished.branch).splitlines()
    messages = [Workspace.git(orch_repo, "show", "-s", "--format=%B", sha) for sha in log[:2]]
    assert (finished.status, statuses(orchestrator, finished)) == (
        "done", {"T-001": "DONE", "T-002": "DONE"},
    )
    assert caller.keys() == ["plan", "dsh:T-001", "dsh:T-002"]
    assert [parse_trailers(m)["Orch-Task"] for m in messages] == ["T-002", "T-001"]
    assert all(set(parse_trailers(m)) == set(REQUIRED_TRAILERS) for m in messages)
    assert Workspace.git(orch_repo, "rev-parse", f"orch/{finished.id}/done") == log[0]
    assert Workspace.git(orch_repo, "rev-parse", "HEAD") == log[-1]  # user's branch untouched
    assert Workspace.git(orch_repo, "show", f"{log[0]}:adder.py").endswith("return a + b")


def test_events_form_a_parent_linked_chain_and_record_project_prefixes(
    orchestrator: Orchestrator, finished: Run
) -> None:
    events = orchestrator.state.events(finished.id)
    spans = {event.span for event in events}
    called = [event for event in events if event.kind == "attempt.called"]
    assert all(event.parent in spans or event.parent == "" for event in events)
    assert {event.span for event in called} == {
        f"{finished.id}/T-001/a1",
        f"{finished.id}/T-002/a1",
    }
    assert len({event.data["prefix"] for event in called}) == 1


EDIT_MISS = edit_result("adder.py", "x", search="not in the file")
DISPUTE = edit_result("adder.py", "", "TEST_DISPUTE")
UPHELD = NativeResult(
    "ok", {"verdict": "tests_correct", "explanation": "just add them", "edits": []}
)
SPLIT = NativeResult(
    "ok",
    {
        "summary": "own both modules separately",
        "tasks": [
            {
                "id": "T-003", "goal": "add", "reqs": ["R1"], "files": ["adder.py"],
                "depends_on": [], "tests": ["test_mathops.AddTest.test_r1_adds"],
                "contract": "a + b",
            },
            {
                "id": "T-004", "goal": "double", "reqs": ["R2"], "files": ["doubler.py"],
                "depends_on": ["T-003"], "tests": ["test_mathops.DoubleTest.test_r2_doubles"],
                "contract": "add(a, a)",
            },
        ],
    },
)


@pytest.mark.parametrize(
    ("responses", "expected", "routes"),
    [
        ({"dsh:T-001": [BAD_ADD, GOOD_ADD]}, ("DONE", "DONE", "done"), ["dsh"] * 3),
        (  # the in-attempt repair also misses, so the edit failure escalates unsampled
            {"dsh:T-001": [BAD_ADD, EDIT_MISS, EDIT_MISS], "claude:T-001": [GOOD_ADD]},
            ("DONE", "DONE", "done"),
            ["dsh", "dsh", "dsh", "claude", "dsh"],
        ),
        (
            {"dsh:T-001": [BAD_ADD, BAD_ADD], "claude:T-001": [GOOD_ADD]},
            ("DONE", "DONE", "done"),
            ["dsh", "dsh", "claude", "dsh"],
        ),
        (
            {"dsh:T-001": [EDIT_MISS, EDIT_MISS, BAD_ADD, BAD_ADD, BAD_ADD, GOOD_ADD, BAD_ADD]},
            ("DONE", "DONE", "done"),
            ["dsh"] * 8,
        ),
        (PARKING, ("PARKED", "PLANNED", "parked"), ["dsh", "dsh", "claude", "claude", "dsh",
                                                    "claude"]),
        (
            {
                "dsh:T-001": [edit_result("adder.py", "", "BLOCKED")],
                "claude:T-001": [SPLIT],
                "dsh:T-003": [GOOD_ADD],
                "dsh:T-004": [GOOD_DOUBLE],
            },
            ("REPLANNED", "REPLANNED", "done"),
            ["dsh", "claude", "dsh", "dsh"],
        ),
        (
            {"dsh:T-001": [DISPUTE, GOOD_ADD], "claude:T-001": [UPHELD]},
            ("DONE", "DONE", "done"),
            ["dsh", "claude", "dsh", "dsh"],
        ),
        (
            {"dsh:T-001": [edit_result("doubler.py", "return 0"), GOOD_ADD]},
            ("DONE", "DONE", "done"),
            ["dsh"] * 3,
        ),
    ],
    ids=["retry", "edit-miss-skips-sampling", "repeat-to-claude", "k-samples",
         "every-rung-parks", "blocked-replans", "dispute-upheld", "scope"],
)
def test_failure_ladder(
    orchestrator: Orchestrator,
    caller: ScriptedCaller,
    planned: Run,
    responses: dict[str, list[NativeResult]],
    expected: tuple[str, str, str],
    routes: list[str],
) -> None:
    caller.responses.update({"dsh:T-002": [GOOD_DOUBLE], **copy.deepcopy(responses)})
    run = orchestrator.execute(planned.id)
    tasks = statuses(orchestrator, run)
    assert (tasks["T-001"], tasks["T-002"], run.status) == expected
    assert [key.split(":")[0] for key in caller.keys()[1:]] == routes
    unused = {key: queue for key, queue in caller.responses.items() if queue}
    assert unused in ({}, {"dsh:T-002": [GOOD_DOUBLE]})  # replans retire T-002 unbuilt


SETTLED: dict[str, object] = {"input_tokens": 100, "output_tokens": 20, "settled_cents": 1,
                              "forwarded": 1}
BROKEN_JSON = NativeResult(
    "failed", {}, usage=SETTLED, message="Invalid DSH JSON result: Expecting ',' delimiter",
    text='{"status":"DONE","summary":"","reason":"","need_files":[],"edits":[{"path":'
         '"adder.py","search":"raise NotImplementedError","replace":"return "a + b""}]}')
CUT_JSON = replace(BROKEN_JSON, usage={**SETTLED, "turn_end": "max-tokens"})
UNSETTLED_JSON = NativeResult("failed", {}, usage={"forwarded": 1, "reserved_cents": 300},
                              message="Invalid DSH JSON result: Expecting ',' delimiter")
UNTERMINATED = edit_result("adder.py", 'return "a + b')


@pytest.mark.parametrize(("answers", "repairs", "first_attempt_calls", "evidence"), [
    ([BROKEN_JSON, GOOD_ADD], ["json"], 2, "error_char="),
    ([UNTERMINATED, GOOD_ADD], ["edit"], 2, "resulting line: '    return \"a + b'"),
    ([BROKEN_JSON, BROKEN_JSON, GOOD_ADD], ["json"], 2, "not valid JSON"),
    ([UNSETTLED_JSON, GOOD_ADD], [], 1, ""),
], ids=["json", "syntax", "repair-once-then-ladder", "unknown-never-repaired"])
def test_dsf_repairs_its_own_settled_answer_within_the_attempt(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
    answers: list[NativeResult], repairs: list[str], first_attempt_calls: int, evidence: str,
) -> None:
    caller.responses.update({"dsh:T-001": list(answers), "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    calls = [c for c in orchestrator.state.usage.calls(run.id) if c.task_id == "T-001"]
    first = [c for c in calls if c.attempt == 1]
    prompts = [text for _, key, text in caller.calls if key == "dsh:T-001"]
    sidecars = sorted((orchestrator.run_dir(run.id) / "responses").glob("*.json"))
    repaired = events(orchestrator, run.id, "attempt.format_repair")
    assert [e.data["kind"] for e in repaired] == repairs
    assert len(first) == first_attempt_calls and all(c.provider == "dsh" for c in calls)
    assert all(evidence in prompts[1] for _ in repairs)
    assert len(sidecars) == sum(a not in (GOOD_ADD, UNSETTLED_JSON) for a in answers)
    if answers[0] is UNSETTLED_JSON:
        assert not sidecars and caller.responses["dsh:T-001"] == [GOOD_ADD]
    else:
        assert statuses(orchestrator, run)["T-001"] == "DONE" and not caller.responses["dsh:T-001"]


def test_a_persistently_unparseable_builder_repairs_once_per_attempt_and_parks(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    caller.responses.update({"dsh:T-001": [BROKEN_JSON] * 40, "dsh:T-002": [GOOD_DOUBLE],
                             "claude:T-001": [BAD_ADD, THICK, EMPTY_REPLAN]})
    run = orchestrator.execute(planned.id)
    builds = Counter((c.attempt, c.sample) for c in orchestrator.state.usage.calls(run.id)
                     if c.task_id == "T-001" and c.provider == "dsh")
    task = orchestrator.state.task(run.id, "T-001")
    assert (task.status, task.reason) == ("PARKED", "replan returned no tasks")
    assert set(builds.values()) == {2} and len(caller.responses["dsh:T-001"]) > 0
    assert len(events(orchestrator, run.id, "attempt.format_repair")) == len(builds)


UNSETTLED_CUT = replace(UNSETTLED_JSON, usage={**UNSETTLED_JSON.usage, "turn_end": "max-tokens"})


@pytest.mark.parametrize(("answers", "reservation", "efforts", "reason", "events_seen"), [
    ([GOOD_ADD], 300, [(1, DSF_EXTENDED.effort)], "", {"attempt.format_repair": 0}),
    ([BROKEN_JSON, GOOD_ADD], 300, [(1, DSF_EXTENDED.effort), (1, DSF_EXTENDED.effort)], "",
     {"attempt.format_repair": 1}),
    ([CUT_JSON, GOOD_ADD], 300, [(1, DSF_EXTENDED.effort)],
     "output_limit: provider stopped the answer at the 131072-token output limit, the widest",
     {"attempt.format_repair": 0}),
    ([GOOD_ADD], 100, [(1, DSF.effort)], "", {"attempt.format_repair": 0}),
    ([CUT_JSON, GOOD_ADD], 100, [(1, DSF.effort)],
     "output_limit: provider stopped the answer at the 32768-token output limit; a wider "
     "budget is not covered", {"attempt.format_repair": 0}),
    ([UNSETTLED_CUT, GOOD_ADD], 300, [(1, DSF_EXTENDED.effort)], "unresolved provider call",
     {"attempt.format_repair": 0}),
], ids=["covered-request-starts-wide", "format-slip-self-repairs", "cut-at-widest-stops",
        "uncovered-request-keeps-base", "uncovered-cut-stops", "unknown-cut-is-not-settled"])
def test_dsf_starts_at_the_widest_covered_budget_and_a_confirmed_cut_stops_durably(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
    monkeypatch: pytest.MonkeyPatch, answers: list[NativeResult], reservation: int,
    efforts: list[tuple[int, str]], reason: str, events_seen: dict[str, int],
) -> None:
    monkeypatch.setattr("orchestragent.orch_run.DSF_RESERVATION", reservation)
    caller.responses.update({"dsh:T-001": list(answers), "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    orchestrator.execute(run.id)  # execute again: a stopped task is not redispatched
    resumed = Orchestrator(orchestrator.repo, caller, local_runner, notify=lambda _: None)
    resumed.execute(run.id)
    calls = [c for c in resumed.state.usage.calls(run.id) if c.task_id == "T-001"]
    task = resumed.state.task(run.id, "T-001")
    assert [(c.attempt, c.effort) for c in calls] == efforts
    assert {kind: len(events(resumed, run.id, kind)) for kind in events_seen} == events_seen
    assert (task.status, task.reason.startswith(reason)) == (
        "PARKED" if reason else "DONE", True)
    assert caller.responses["dsh:T-001"] == ([] if not reason else [*answers[len(efforts):]])
    assert not any(e.data.get("category") == "output_limit"
                   for e in events(resumed, run.id, "task.failed"))


@pytest.fixture
def untested_split() -> NativeResult:
    proposal = copy.deepcopy(SPLIT)
    tasks = cast(list[dict[str, Any]], proposal.data["tasks"])
    tasks[1]["tests"] += tasks[0]["tests"]
    tasks[0]["tests"] = []
    return proposal


@pytest.mark.parametrize("revised", [True, False])
def test_rejected_replan_gets_one_revision_with_the_controller_errors(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
    untested_split: NativeResult, revised: bool,
) -> None:
    caller.responses.update({
        "dsh:T-001": [edit_result("adder.py", "", "BLOCKED")],
        "claude:T-001": [untested_split, SPLIT if revised else copy.deepcopy(untested_split)],
        "dsh:T-003": [GOOD_ADD], "dsh:T-004": [GOOD_DOUBLE], "dsh:T-002": [GOOD_DOUBLE],
    })
    run = orchestrator.execute(planned.id)
    replans = [text for _, key, text in caller.calls if key == "claude:T-001"]
    rejected = events(orchestrator, run.id, "task.replan_rejected", "T-001")
    assert len(replans) == 2 and len(rejected) == (1 if revised else 2)
    assert "T-003: needs reqs, files and tests (missing tests)" in cast(
        list[str], rejected[0].data["errors"])
    assert "(missing tests)" in replans[1] and "PREVIOUS REPLAN REJECTED" in replans[1]
    assert "at least one of the replaced frozen tests" in replans[0]
    assert (run.status, orchestrator.state.task(run.id, "T-001").status) == (
        ("done", "REPLANNED") if revised else ("parked", "PARKED"))


def test_parked_task_keeps_evidence(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run
) -> None:
    caller.responses.update(copy.deepcopy(PARKING))
    orchestrator.execute(planned.id)
    task = orchestrator.state.task(planned.id, "T-001")
    failed = [e.data for e in events(orchestrator, planned.id, "task.failed", "T-001")]
    assert (task.attempts, task.used_levels, task.reason) == (
        5, [0, 1, 3, 4], "replan returned no tasks"
    )
    assert [entry["next"] for entry in failed] == [1, 3, 4, 5]
    assert task.contract == "add(a, b) returns a + b exactly"
    assert "AssertionError" in task.last_error and "a - b" in task.last_diff


def test_samples_run_in_parallel_and_the_passing_one_merges(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run
) -> None:
    caller.responses.update(
        {
            "dsh:T-001": [EDIT_MISS, EDIT_MISS, BAD_ADD, GOOD_ADD, BAD_ADD, GOOD_ADD, BAD_ADD],
            "dsh:T-002": [GOOD_DOUBLE],
        }
    )
    run = orchestrator.execute(planned.id)
    verifying = events(orchestrator, run.id, "task.verifying", "T-001")[-1].data
    merging = events(orchestrator, run.id, "task.merging", "T-001")[-1].data
    done = events(orchestrator, run.id, "task.done", "T-001")[0].data
    assert verifying["candidates"] == ["a3s1", "a3s2", "a3s3", "a3s4"]
    passing = cast(list[str], merging["passing"])
    assert len(passing) == 2 and done["candidate"] in passing
    assert run.status == "done"
    calls = orchestrator.state.usage_calls(run.id)
    samples = [c for c in calls if c.task_id == "T-001" and c.attempt == 3]
    assert {c.sample for c in samples} == {"a3s1", "a3s2", "a3s3", "a3s4"}
    assert len({c.id for c in samples}) == 4  # losing candidates also consumed tokens


@pytest.mark.parametrize("oracle", ["wrong", "correct", "duplicate_candidates", "already_reviewed"])
def test_failed_candidate_agreement_reviews_tests_once_and_preserves_code_evidence(
    orchestrator: Orchestrator, caller: ScriptedCaller, math_plan: dict[str, object], oracle: str,
) -> None:
    if oracle == "wrong":
        cast(list[dict[str, str]], math_plan["files"])[2]["content"] = MATH_TESTS.replace(
            "add(2, 3), 5", "add(2, 3), 6")
    variations = ([GOOD_ADD, edit_result("adder.py", "return b + a")] * 2 if oracle == "wrong" else
                  [BAD_ADD] * 4 if oracle == "duplicate_candidates" else
                  [BAD_ADD, edit_result("adder.py", "return 0")] * 2)
    reviews = [NativeResult("ok", {
        "verdict": "tests_wrong" if oracle == "wrong" else "tests_correct", "contract": "",
        "explanation": "Check arithmetic against the original requirement.",
        "edits": [{"path": "test_mathops.py", "search": "add(2, 3), 6",
                   "replace": "add(2, 3), 5"}] if oracle == "wrong" else [],
    })] if oracle in {"wrong", "correct"} else []
    caller.responses.update({
        "dsh:T-001": [EDIT_MISS, EDIT_MISS, BAD_ADD, *variations], "dsh:T-002": [GOOD_DOUBLE],
        "claude:T-001": [*reviews, *([] if oracle == "wrong" else [GOOD_ADD])],
    })
    run = orchestrator.plan("add and double")
    if oracle == "already_reviewed":
        orchestrator.state.transition(run.id, "T-001", "READY", "fixture.reviewed", {}, disputes=1)
    run = orchestrator.execute(run.id)
    recorded = orchestrator.state.events(run.id, "T-001")
    assert run.status == "done" and caller.keys().count("dsh:T-001") == 7  # 1 edit repair
    assert sum(e.kind == "task.failure_agreement" for e in recorded) == int(
        oracle != "duplicate_candidates")
    purposes = [c.purpose for c in orchestrator.state.usage.calls(run.id)
                if c.provider == "claude" and c.task_id == "T-001"]
    assert purposes == (["review"] if oracle == "wrong" else
                        ["review", "patch"] if oracle == "correct" else ["patch"])
    if oracle == "wrong":
        assert sum(e.kind == "task.candidate_reused" for e in recorded) == 1
    elif oracle == "correct":
        patch = [prompt for _, key, prompt in caller.calls if key == "claude:T-001"][-1]
        assert "AssertionError" in patch
        # Parallel responses may be assigned to either candidate label.
        assert any(f"+    {body}" in patch for body in ("return a - b", "return 0"))


@pytest.mark.parametrize("corrections", [1, 2, 3])
def test_wrong_frozen_test_is_corrected_by_claude_and_refrozen(
    orch_repo: Path, math_plan: dict[str, object], corrections: int,
) -> None:
    files = cast(list[dict[str, str]], math_plan["files"])
    cases = [("2, 3", 5), ("0, 0", 0), ("-1, 1", 0)][:corrections]
    assertions = "\n".join(f"        self.assertEqual(add({args}), {expected + 1})"
                           for args, expected in cases)
    files[2] = {"path": "test_mathops.py", "content": MATH_TESTS.replace(
        "        self.assertEqual(add(2, 3), 5)", assertions)}
    fixes = [NativeResult(
        "ok",
        {
            "verdict": "tests_wrong",
            "explanation": f"add({args}) is {expected}",
            "edits": [{"path": "test_mathops.py", "search": f"add({args}), {expected + 1}",
                       "replace": f"add({args}), {expected}"}],
        },
    ) for args, expected in cases]
    caller = ScriptedCaller(
        {
            "plan": [NativeResult("ok", math_plan)],
            "dsh:T-001": [*[DISPUTE] * corrections, GOOD_ADD],
            "claude:T-001": fixes,
            "dsh:T-002": [GOOD_DOUBLE],
        }
    )
    orchestrator = Orchestrator(orch_repo, caller, local_runner, notify=lambda line: None)
    run = orchestrator.execute(orchestrator.plan("spec").id)
    log = Workspace.git(orch_repo, "log", "--format=%s", run.branch).splitlines()
    assert run.status == "done"
    assert sum(line.startswith("orch tests T-001:") for line in log) == corrections
    assert "3), 5" in Workspace.git(orch_repo, "show", f"{run.branch}:test_mathops.py")
    assert run.plan_hash != orchestrator.state.events(run.id)[0].data["plan"]
    review = next(prompt for provider, _, prompt in caller.calls if provider == "claude"
                  and "Independently review" in prompt)
    assert isinstance(review, ContextPrompt) and run.spec in review.shared


@pytest.mark.parametrize("defect", ["constant", "capture"])
def test_test_correction_with_bad_fixture_rolls_back_without_state_or_source_change(
    orchestrator: Orchestrator, planned: Run, defect: str,
) -> None:
    state = orchestrator.state
    state.transition(planned.id, "T-001", "READY", "task.ready", {})
    task = state.transition(planned.id, "T-001", "RUNNING", "task.started", {}, attempts=1)
    path = orchestrator.integration(planned.id) / "test_mathops.py"
    original = path.read_text()
    before = (state.run(planned.id), state.tasks(planned.id), state.events(planned.id))
    search = "self.assertEqual(add(2, 3), 5)"
    replacement = ("self.assertEqual(1, 1)" if defect == "constant" else
                   search + "\n        callbacks = [lambda: n for n in (1, 2)]")
    with pytest.raises(AttemptFailed, match="test correction fails lint"):
        orchestrator._fix_tests(planned, task, [path.name], [{
            "path": path.name, "search": search, "replace": replacement}], "invalid fixture")
    assert (state.run(planned.id), state.tasks(planned.id), state.events(planned.id)) == before
    assert path.read_text() == original
    assert Workspace.git(path.parent, "status", "--porcelain") == ""


@pytest.mark.parametrize(("disputes", "corrected"), [(1, 0), (2, 1), (3, 3)])
def test_unchanged_or_exhausted_test_reviews_do_not_make_more_calls(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
    disputes: int, corrected: int,
) -> None:
    for _ in range(corrected):
        orchestrator.state.record(planned.id, "task.tests_fixed", {}, "T-001")
    task = replace(orchestrator.state.task(planned.id, "T-001"), disputes=disputes)
    with pytest.raises(AttemptFailed, match="dispute"):
        orchestrator._signal(planned.id, task, Signal("TEST_DISPUTE", "another claim"))
    assert caller.keys() == ["plan"]


@pytest.mark.parametrize("stage", ["RUNNING", "VERIFYING"])
def test_upheld_review_preserves_stage_then_uses_the_normal_failure_transition(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run, stage: str,
) -> None:
    state = orchestrator.state
    state.transition(planned.id, "T-001", "READY", "task.ready", {})
    task = state.transition(planned.id, "T-001", "RUNNING", "task.started", {}, attempts=1)
    if stage == "VERIFYING":
        task = state.transition(planned.id, task.id, stage, "task.verifying", {})
    caller.responses["claude:T-001"] = [NativeResult("ok", {
        "verdict": "tests_correct", "explanation": "The callback failure is intentional.",
        "edits": [], "contract": ""})]
    with pytest.raises(AttemptFailed, match="intentional") as caught:
        orchestrator._review(planned.id, task, "check fixture")
    assert state.task(planned.id, task.id).status == stage
    orchestrator._failed(planned.id, task.id, 1, 0, caught.value)
    current = state.task(planned.id, task.id)
    assert current.status == "READY" and current.disputes == 1
    assert caller.keys() == ["plan", "claude:T-001"]


@pytest.mark.parametrize(("corrections", "producer"), [(1, "dsh"), (2, "dsh"), (1, "claude")])
def test_fixture_error_reviews_directly_and_rechecks_saved_code_without_regeneration(
    orch_repo: Path, math_plan: dict[str, object], corrections: int, producer: str,
) -> None:
    cases = [("2, 3", 5), ("0, 0", 0)][:corrections]
    assertions = "\n".join(f"        self.assertEqual(add({args}), expected_{index})"
                           for index, (args, _) in enumerate(cases))
    cast(list[dict[str, str]], math_plan["files"])[2]["content"] = MATH_TESTS.replace(
        "        self.assertEqual(add(2, 3), 5)", assertions)
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", math_plan)],
        "dsh:T-001": [GOOD_ADD] if producer == "dsh" else [], "dsh:T-002": [GOOD_DOUBLE],
        "claude:T-001": [*([GOOD_ADD] if producer == "claude" else []), *[NativeResult("ok", {
            "verdict": "tests_wrong", "explanation": "expected value was never defined",
            "contract": "", "edits": [{"path": "test_mathops.py",
                                        "search": f"expected_{index}",
                                        "replace": str(expected)}],
        }) for index, (_, expected) in enumerate(cases)]],
    })
    orch = Orchestrator(orch_repo, caller, local_runner, notify=lambda _: None)
    run = orch.plan("Add and double")
    if producer == "claude":
        orch.state.transition(run.id, "T-001", "READY", "task.ready", {}, level=3)
    run = orch.execute(run.id)
    recorded = orch.state.events(run.id, "T-001")
    assert run.status == "done"
    assert caller.keys().count(f"{producer}:T-001") == (
        1 + (corrections if producer == "claude" else 0))
    assert sum(e.kind == "task.fixture_dispute" for e in recorded) == corrections
    assert sum(e.kind == "task.candidate_reused" for e in recorded) == corrections
    assert not any(e.kind == "task.failed" for e in recorded)
    assert [c.purpose for c in orch.state.usage.calls(run.id)
            if c.provider == "claude"] == ["plan", *(["patch"] if producer == "claude" else []),
                                          *["review"] * corrections]


@pytest.mark.parametrize(("producer", "missing"), [("dsh", False), ("claude", False),
                                                   ("dsh", True)])
def test_semantic_test_correction_reuses_durable_failed_code_or_discards_invalid_artifact(
    orch_repo: Path, math_plan: dict[str, object], monkeypatch: pytest.MonkeyPatch,
    producer: str, missing: bool,
) -> None:
    cast(list[dict[str, str]], math_plan["files"])[2]["content"] = MATH_TESTS.replace(
        "add(2, 3), 5", "add(2, 3), 6")
    correction = NativeResult("ok", {
        "verdict": "tests_wrong", "explanation": "2 + 3 is 5", "contract": "",
        "edits": [{"path": "test_mathops.py", "search": "add(2, 3), 6",
                   "replace": "add(2, 3), 5"}],
    })
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", math_plan)],
        "dsh:T-001": [*([GOOD_ADD] if producer == "dsh" else []), DISPUTE,
                      *([GOOD_ADD] if missing else [])],
        "claude:T-001": [*([GOOD_ADD] if producer == "claude" else []), correction],
        "dsh:T-002": [GOOD_DOUBLE],
    })
    orch = Orchestrator(orch_repo, caller, local_runner, notify=lambda _: None)
    run = orch.plan("Add and double")
    if producer == "claude":
        orch.state.transition(run.id, "T-001", "READY", "task.ready", {}, level=3)
    review = orch._review

    def after_restart(run_id: str, task: OrchTask, reason: str, **kwargs: Any) -> None:
        reopened = Orchestrator(orch_repo, caller, local_runner, notify=lambda _: None)
        saved = reopened.state.task(run_id, task.id).failed_candidate
        assert saved and not task.pending_candidate
        if missing:
            orch.state.transition(run_id, task.id, task.status, "task.test_fault", {},
                                  failed_candidate="f" * 40)
        review(run_id, task, reason, **kwargs)

    monkeypatch.setattr(orch, "_review", after_restart)
    result = orch.execute(run.id, jobs=1)
    assert result.status == "done"
    assert len(events(orch, run.id, "task.candidate_reused", "T-001")) == int(not missing)
    assert len(events(orch, run.id, "task.candidate_discarded", "T-001")) == int(missing)
    assert caller.keys().count("dsh:T-001") == (2 if producer == "dsh" else 1) + missing
    assert orch.state.task(run.id, "T-001").failed_candidate == ""
    assert audit(orch, run.id, run_tests=False) == []


def test_test_revision_rolls_back_git_plan_task_and_events_on_partial_database_failure(
    orchestrator: Orchestrator, planned: Run, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = orchestrator.state
    state.transition(planned.id, "T-001", "READY", "task.ready", {})
    task = state.transition(planned.id, "T-001", "RUNNING", "task.started", {}, attempts=1)
    before, count = state.run(planned.id), len(state.events(planned.id))

    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected write failure after run update")

    monkeypatch.setattr(state, "_save_task", unavailable)
    with pytest.raises(RuntimeError, match="injected write failure"):
        orchestrator._fix_tests(before, task, ["test_mathops.py"], [{
            "path": "test_mathops.py", "search": "add(2, 3), 5", "replace": "add(2, 3), 6",
        }], "fault injection")
    assert state.run(planned.id) == before and state.task(planned.id, task.id) == task
    assert len(state.events(planned.id)) == count
    assert Workspace.git(orchestrator.integration(planned.id), "rev-parse", "HEAD") == (
        before.verified_head)


@pytest.fixture
def reviewing(orchestrator: Orchestrator, planned: Run) -> OrchTask:
    orchestrator.state.transition(planned.id, "T-001", "READY", "task.ready", {})
    return orchestrator.state.transition(planned.id, "T-001", "RUNNING", "task.started", {},
                                         attempts=1)


@pytest.fixture
def review_answers() -> dict[str, NativeResult]:
    return {verdict: NativeResult("ok", {
        "verdict": verdict, "explanation": "The sum is five.", "contract": "",
        "lesson": "Keep callback fixtures consistent with their inputs.",
        "edits": [] if verdict == "tests_correct" else [{
            "path": "test_mathops.py", "search": "add(2, 3), 5",
            "replace": "add(2, 3), 2 + 3"}],
    }, usage={"input_tokens": 10, "output_tokens": 2, "settled_cents": 7})
            for verdict in ("tests_correct", "tests_wrong")}


@pytest.fixture
def concurrent_test_revision(orchestrator: Orchestrator, planned: Run) -> Callable[[], None]:
    def revise() -> None:
        state = orchestrator.state
        other = state.task(planned.id, "T-002")
        if other.status == "PLANNED":
            other = state.transition(planned.id, other.id, "READY", "task.ready", {})
        other = state.transition(planned.id, other.id, "RUNNING", "task.started", {},
                                 attempts=other.attempts + 1)
        path = orchestrator.integration(planned.id) / "test_mathops.py"
        original = path.read_text()
        orchestrator._fix_tests(state.run(planned.id), other, [path.name], [{
            "path": path.name, "search": original,
            "replace": original + f"\n# Concurrent correction {other.attempts}\n",
        }], "another node corrects this frozen file")
    return revise


@pytest.mark.parametrize(("verdict", "window"), [
    ("tests_correct", "after_result"), ("tests_wrong", "after_result"),
    ("tests_correct", "before_result_event"), ("tests_wrong", "before_result_event"),
    ("tests_correct", "after_verdict"),
])
def test_review_response_survives_restart_before_application_without_rebilling(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run, reviewing: OrchTask,
    review_answers: dict[str, NativeResult], monkeypatch: pytest.MonkeyPatch,
    verdict: str, window: str,
) -> None:
    caller.responses["claude:T-001"] = [review_answers[verdict]]
    invoke = orchestrator._claude

    def interrupted(*args: Any, **kwargs: Any) -> dict[str, object]:
        invoke(*args, **kwargs)
        raise RuntimeError("crash after response settlement, before application")

    if window == "after_result":
        monkeypatch.setattr(orchestrator, "_claude", interrupted)
    elif window == "before_result_event":
        record = orchestrator.state.record

        def before_event(run_id: str, kind: str, *args: Any, **kwargs: Any) -> None:
            if kind == "attempt.result":
                raise RuntimeError("crash after response settlement, before event")
            record(run_id, kind, *args, **kwargs)

        monkeypatch.setattr(orchestrator.state, "record", before_event)
    error = AttemptFailed if window == "after_verdict" else RuntimeError
    message = "sum is five" if window == "after_verdict" else "after response settlement"
    with pytest.raises(error, match=message):
        orchestrator._review(planned.id, reviewing, "check sum")
    receipt = orchestrator.state.usage.calls(planned.id)[-1]
    assert orchestrator.spent == 7  # event persistence cannot hide a settled dispatch
    reopened = Orchestrator(orchestrator.repo, caller, local_runner, notify=lambda _: None)
    reopened.state.recover(planned.id)
    task = reopened.state.transition(planned.id, reviewing.id, "RUNNING", "task.started", {},
                                     attempts=2)
    reopened.spent = reopened.state.spent(planned.id)
    if verdict == "tests_correct":
        with pytest.raises(AttemptFailed, match="sum is five") as caught:
            reopened._step(planned.id, task)
        reopened._failed(planned.id, task.id, task.attempts, task.level, caught.value)
    else:
        reopened._step(planned.id, task)
        content = (reopened.integration(planned.id) / "test_mathops.py").read_text()
        assert "add(2, 3), 2 + 3" in content
    current = reopened.state.task(planned.id, task.id)
    assert current.disputes == 1 and current.pending_review == ""
    assert caller.keys() == ["plan", "claude:T-001"]
    assert reopened.spent == reopened.state.spent(planned.id) == 7
    results = events(reopened, planned.id, "attempt.result", task.id)
    assert len(results) == int(window != "before_result_event")
    if window == "after_verdict":
        reused = events(reopened, planned.id, "task.review_applied_reused", task.id)
        assert reused[0].data["review"] == receipt.sample
        assert current.contract.count("Reviewer (tests are correct)") == 1
    else:
        reused = events(reopened, planned.id, "task.review_reused", task.id)
        assert reused[0].data["call_id"] == receipt.id


@pytest.mark.parametrize("category", ["test", "fixture"])
def test_applied_review_replays_original_failure_and_diff_until_ladder_transition_commits(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run, reviewing: OrchTask,
    review_answers: dict[str, NativeResult], category: str,
) -> None:
    caller.responses["claude:T-001"] = [review_answers["tests_correct"]]
    failure = AttemptFailed(category, "original complete failure report", "original candidate diff")
    with pytest.raises(AttemptFailed, match="original complete"):
        orchestrator._review(planned.id, reviewing, "check fixture", failure=failure)
    reopened = Orchestrator(orchestrator.repo, caller, local_runner, notify=lambda _: None)
    reopened.state.recover(planned.id)
    task = reopened.state.transition(planned.id, reviewing.id, "RUNNING", "task.started", {},
                                     attempts=2)
    with pytest.raises(AttemptFailed, match="original complete") as caught:
        reopened._step(planned.id, task)
    assert (caught.value.category, caught.value.detail, caught.value.diff) == (
        failure.category, failure.detail, failure.diff)
    reopened._failed(planned.id, task.id, task.attempts, task.level, caught.value)
    current = reopened.state.task(planned.id, task.id)
    assert current.pending_review == "" and current.disputes == 1
    assert current.last_diff == failure.diff and current.last_error == failure.detail
    assert caller.keys() == ["plan", "claude:T-001"]


@pytest.mark.parametrize("stale", [False, True])
def test_review_receipt_and_response_roll_back_together_and_unknown_needs_authorization(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run, reviewing: OrchTask,
    review_answers: dict[str, NativeResult], monkeypatch: pytest.MonkeyPatch,
    concurrent_test_revision: Callable[[], None], stale: bool,
) -> None:
    caller.responses["claude:T-001"] = [review_answers["tests_correct"]] * 2
    finish = orchestrator.reviews.finish

    def storage_fault(db: sqlite3.Connection, call: Call, result: NativeResult | None) -> None:
        finish(db, call, result)
        raise RuntimeError("crash inside settlement transaction")

    monkeypatch.setattr(orchestrator.reviews, "finish", storage_fault)
    with pytest.raises(RuntimeError, match="settlement transaction"):
        orchestrator._review(planned.id, reviewing, "check sum")
    old = orchestrator.state.usage.calls(planned.id)[-1]
    assert old.outcome == "pending" and old.finished_at is None
    reopened = Orchestrator(orchestrator.repo, caller, local_runner, notify=lambda _: None)
    reopened.state.recover(planned.id)
    assert reopened.state.task(planned.id, reviewing.id).status == "PARKED"
    with pytest.raises(Conflict, match="Unresolved"):
        reopened.state.retry(planned.id, reviewing.id)
    assert caller.keys() == ["plan", "claude:T-001"]
    if stale:
        concurrent_test_revision()
        reopened.state.transition(planned.id, reviewing.id, "READY", "task.test_resume", {})
        active = reopened.state.transition(planned.id, reviewing.id, "RUNNING", "task.started", {})
        with pytest.raises(Parked, match="unresolved"):
            reopened._review(planned.id, active, "")
        reopened.state.transition(planned.id, reviewing.id, "PARKED", "task.parked", {})
    reopened.state.retry(planned.id, reviewing.id, ack_unknown=True)
    task = reopened.state.transition(planned.id, reviewing.id, "RUNNING", "task.started", {},
                                     attempts=2)
    if stale:
        reopened._step(planned.id, task)
    else:
        with pytest.raises(AttemptFailed, match="sum is five"):
            reopened._step(planned.id, task)
    assert caller.keys().count("claude:T-001") == (1 if stale else 2)
    assert next(c for c in reopened.state.usage.calls(planned.id) if c.id == old.id) == old
    assert reopened.state.spent(planned.id) == (0 if stale else 7)


@pytest.mark.parametrize(("when", "verdict"), [
    (when, verdict) for when in ("before_dispatch", "during_dispatch")
    for verdict in ("tests_correct", "tests_wrong")
])
def test_concurrent_test_revision_discards_old_review_without_mutation_or_learning(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run, reviewing: OrchTask,
    review_answers: dict[str, NativeResult], concurrent_test_revision: Callable[[], None],
    monkeypatch: pytest.MonkeyPatch, when: str, verdict: str,
) -> None:
    caller.responses["claude:T-001"] = [review_answers[verdict]]
    if when == "before_dispatch":
        begin = orchestrator.reviews.begin

        def after_snapshot(*args: Any, **kwargs: Any) -> Review:
            saved = begin(*args, **kwargs)
            concurrent_test_revision()
            return saved

        monkeypatch.setattr(orchestrator.reviews, "begin", after_snapshot)
    else:
        invoke = orchestrator._claude

        def while_reviewing(*args: Any, **kwargs: Any) -> dict[str, object]:
            response = invoke(*args, **kwargs)
            concurrent_test_revision()
            return response

        monkeypatch.setattr(orchestrator, "_claude", while_reviewing)
    orchestrator._review(planned.id, reviewing, "check sum")
    task = orchestrator.state.task(planned.id, reviewing.id)
    content = (orchestrator.integration(planned.id) / "test_mathops.py").read_text()
    assert task.status == "READY" and task.pending_review == "" and task.disputes == 0
    assert "Concurrent correction 1" in content and "add(2, 3), 5" in content
    assert task.contract == reviewing.contract and not orchestrator.state.rules("planner")
    assert len(events(orchestrator, planned.id, "task.review_stale", task.id)) == 1
    assert caller.keys().count("claude:T-001") == int(when == "during_dispatch")


def test_unrelated_plan_metadata_does_not_invalidate_review(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run, reviewing: OrchTask,
    review_answers: dict[str, NativeResult], monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller.responses["claude:T-001"] = [review_answers["tests_wrong"]]
    invoke = orchestrator._claude

    def change_summary(*args: Any, **kwargs: Any) -> dict[str, object]:
        response = invoke(*args, **kwargs)
        plan = {**orchestrator.state.run(planned.id).plan, "summary": "other planning metadata"}
        orchestrator.state.update_run(planned.id, "plan.metadata", {}, plan=plan,
                                      plan_hash=plan_hash(plan))
        return response

    monkeypatch.setattr(orchestrator, "_claude", change_summary)
    orchestrator._review(planned.id, reviewing, "check sum")
    assert len(events(orchestrator, planned.id, "task.tests_fixed", reviewing.id)) == 1
    assert not events(orchestrator, planned.id, "task.review_stale", reviewing.id)


@pytest.mark.parametrize("outcome", ["rate_limited", "auth_required", "failed"])
def test_pending_review_does_not_refill_provider_retry_budget(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run, reviewing: OrchTask,
    outcome: str,
) -> None:
    caller.responses["claude:T-001"] = [NativeResult(outcome, {}, usage={"settled_cents": 7})] * 3
    for _ in range(3):
        orchestrator.stop_reason = ""  # explicit resume after resolving the provider condition
        with pytest.raises((AttemptFailed, Deferred)):
            orchestrator._review(planned.id, reviewing, "check sum")
    orchestrator.stop_reason = ""
    with pytest.raises(Parked, match="retry limit"):
        orchestrator._review(planned.id, reviewing, "check sum")
    assert caller.keys().count("claude:T-001") == 3
    assert orchestrator.state.spent(planned.id) == 21


def test_repeated_stale_review_inputs_stop_without_model_calls(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run, reviewing: OrchTask,
    concurrent_test_revision: Callable[[], None], monkeypatch: pytest.MonkeyPatch,
) -> None:
    begin = orchestrator.reviews.begin

    def after_snapshot(*args: Any, **kwargs: Any) -> Review:
        saved = begin(*args, **kwargs)
        concurrent_test_revision()
        return saved

    monkeypatch.setattr(orchestrator.reviews, "begin", after_snapshot)
    for _ in range(3):
        active = orchestrator.state.task(planned.id, reviewing.id)
        if active.status == "READY":
            active = orchestrator.state.transition(planned.id, active.id, "RUNNING",
                                                     "task.started", {})
        orchestrator._review(planned.id, active, "check sum")
    active = orchestrator.state.transition(planned.id, reviewing.id, "RUNNING", "task.started", {})
    with pytest.raises(Parked, match="refresh limit"):
        orchestrator._review(planned.id, active, "check sum")
    assert caller.keys() == ["plan"]
    assert len(events(orchestrator, planned.id, "task.review_stale", reviewing.id)) == 3


@pytest.mark.parametrize("answer", [
    {}, {"verdict": "unknown", "explanation": "uncertain"},
    {"verdict": "tests_correct", "explanation": ""},
    {"verdict": "tests_correct", "explanation": "ok", "edits": [{"path": "a.py"}]},
    {"verdict": "contract", "explanation": "wrong operation", "contract": "change"},
    {"verdict": "tests_correct", "explanation": "mixed operation", "contract": "change"},
])
def test_invalid_review_cannot_silently_uphold_generated_tests(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
    answer: dict[str, Any],
) -> None:
    caller.responses.update({"dsh:T-001": [DISPUTE],
                             "claude:T-001": [NativeResult("ok", answer)]})
    run = orchestrator.execute(planned.id)
    task = orchestrator.state.task(run.id, "T-001")
    assert task.status == "PARKED" and "invalid test review" in task.reason
    assert not events(orchestrator, run.id, "task.dispute_rejected")


def test_opus_patches_share_project_context_across_tasks(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    caller.responses.update({"claude:T-001": [GOOD_ADD], "claude:T-002": [GOOD_DOUBLE]})
    for task in orchestrator.state.tasks(planned.id):
        orchestrator.state.transition(planned.id, task.id, task.status, "task.route", {}, level=3)
    run = orchestrator.execute(planned.id)
    prompts = [prompt for provider, key, prompt in caller.calls
               if provider == "claude" and key != "plan"]
    assert run.status == "done" and len(prompts) == 2
    assert all(isinstance(p, ContextPrompt) for p in prompts)
    first, second = cast(list[ContextPrompt], prompts)
    assert first.shared == second.shared and planned.spec in first.shared
    assert '"task":"T-001"' in first.dynamic and '"task":"T-002"' in second.dynamic
    assert "## OWNED FILES" not in first.shared


def test_provider_stop_pauses_without_spending_an_attempt_then_resumes(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run
) -> None:
    caller.responses["dsh:T-001"] = [NativeResult("quota_exhausted", {}, message="budget")]
    paused = orchestrator.execute(planned.id)
    task = orchestrator.state.task(planned.id, "T-001")
    assert (paused.status, paused.message, task.status, task.attempts) == (
        "paused", "dsh/deepseek-flash quota_exhausted", "READY", 0,
    )
    caller.responses.update({"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    assert orchestrator.execute(planned.id).status == "done"
    calls = [c for c in orchestrator.state.usage_calls(planned.id) if c.task_id == "T-001"]
    assert [(c.attempt, c.outcome) for c in calls] == [(1, "quota_exhausted"), (1, "ok")]
    assert len({c.id for c in calls}) == 2  # resuming the same attempt does not overwrite it


def test_interrupted_task_restarts_from_ready(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run
) -> None:
    orchestrator.state.transition(planned.id, "T-001", "READY", "task.ready", {})
    orchestrator.state.transition(planned.id, "T-001", "RUNNING", "task.started", {}, attempts=1)
    caller.responses.update({"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    kinds = [e.kind for e in orchestrator.state.events(planned.id, "T-001")]
    assert (run.status, kinds.count("task.interrupted")) == ("done", 1)


def test_illegal_transition_is_refused(orchestrator: Orchestrator, planned: Run) -> None:
    with pytest.raises(Conflict, match="PLANNED -> DONE"):
        orchestrator.state.transition(planned.id, "T-001", "DONE", "task.done", {})


def test_integration_gate_failure_rolls_back_and_retries(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run
) -> None:
    gate_failures = [True]

    def flaky_gate(
        execution: Path, scratch: Path, revision: str, names: list[str] | None,
        *, timeout: float = 25.0,
    ) -> tuple[bool, str]:
        if execution.name.startswith("gate") and gate_failures:
            gate_failures.pop()
            return False, "FAIL: test_r1_adds (test_mathops.AddTest.test_r1_adds)"
        return local_runner(execution, scratch, revision, names)

    orchestrator.runner = flaky_gate
    caller.responses.update({"dsh:T-001": [GOOD_ADD, GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    failed = [e.data for e in events(orchestrator, planned.id, "task.failed", "T-001")]
    assert (run.status, [entry["category"] for entry in failed]) == ("done", ["integration"])


def test_planner_gets_lint_errors_and_replans(
    orch_repo: Path, math_plan: dict[str, object]
) -> None:
    broken = copy.deepcopy(math_plan)
    cast(list[object], broken["requirements"]).append({"id": "R3", "text": "x"})
    caller = ScriptedCaller({"plan": [NativeResult("ok", broken), NativeResult("ok", math_plan)]})
    orchestrator = Orchestrator(orch_repo, caller, local_runner, notify=lambda line: None)
    run = orchestrator.plan("spec")
    assert "R3: no test proves it" in caller.calls[1][2] and run.status == "planned"
    assert Workspace.git(orch_repo, "rev-parse", f"orch/{run.id}/plan") == run.checkpoint


def test_plan_that_never_passes_lint_is_refused(
    orch_repo: Path, math_plan: dict[str, object]
) -> None:
    (orch_repo / "adder.py").write_text("x = 1\n")
    Workspace.git(orch_repo, "add", "adder.py")
    Workspace.git(orch_repo, "commit", "-m", "existing adder")
    caller = ScriptedCaller({"plan": [NativeResult("ok", math_plan)] * 3})
    orchestrator = Orchestrator(orch_repo, caller, local_runner, notify=lambda line: None)
    with pytest.raises(PlanError) as refused:
        orchestrator.plan("spec")
    assert refused.value.errors == ["files: adder.py already exists; the plan may only add files"]
    assert caller.keys() == ["plan"] * 3


def test_vacuous_tests_are_flagged_at_plan_time(
    orch_repo: Path, math_plan: dict[str, object]
) -> None:
    files = math_plan["files"]
    assert isinstance(files, list)
    files[0] = {"path": "adder.py", "content": "def add(a: int, b: int) -> int:\n    return 0\n"}
    files[2] = {
        "path": "test_mathops.py",
        "content": MATH_TESTS.replace("add(2, 3), 5", "add(2, 3), 0"),
    }
    caller = ScriptedCaller({"plan": [NativeResult("ok", math_plan)]})
    orchestrator = Orchestrator(orch_repo, caller, local_runner, notify=lambda line: None)
    run = orchestrator.plan("spec")
    flagged = [event.task_id for event in events(orchestrator, run.id, "plan.vacuous_tests")]
    assert flagged == ["T-001"]


def test_nested_packages_and_test_directories_run_end_to_end(orch_repo: Path) -> None:
    stub = "def tokenize(text: str) -> list[str]:\n    raise NotImplementedError\n"
    tests = (
        "import unittest\n\nfrom textstats.tokenizer import tokenize\n\n\n"
        "class TokenizeTest(unittest.TestCase):\n    def test_r1_splits(self) -> None:\n"
        "        self.assertEqual(tokenize('A b'), ['a', 'b'])\n"
    )
    plan: dict[str, object] = {
        "summary": "nested",
        "requirements": [{"id": "R1", "text": "tokenize"}],
        "files": [
            {"path": "textstats/__init__.py", "content": ""},
            {"path": "textstats/tokenizer.py", "content": stub},
            {"path": "tests/test_tokenizer.py", "content": tests},
        ],
        "tasks": [
            {
                "id": "T-001", "goal": "tokenize", "reqs": ["R1"],
                "files": ["textstats/tokenizer.py"], "depends_on": [],
                "tests": ["tests.test_tokenizer.TokenizeTest.test_r1_splits"], "contract": "split",
            }
        ],
    }
    answer = edit_result("textstats/tokenizer.py", "return text.lower().split()")
    caller = ScriptedCaller({"plan": [NativeResult("ok", plan)], "dsh:T-001": [answer]})
    orchestrator = Orchestrator(orch_repo, caller, local_runner, notify=lambda line: None)
    run = orchestrator.execute(orchestrator.plan("spec").id)
    assert (run.status, statuses(orchestrator, run)) == ("done", {"T-001": "DONE"})


LEGACY = 'def greet() -> str:\n    return "hi"\n'
LEGACY_TEST = (
    "import unittest\n\nfrom legacy import greet\n\n\n"
    "class LegacyTest(unittest.TestCase):\n    def test_greets(self) -> None:\n"
    '        self.assertEqual(greet(), "hi")\n'
)


def test_final_gate_bisects_a_regression_and_rebuilds_the_culprit(
    orch_repo: Path, math_plan: dict[str, object]
) -> None:
    (orch_repo / "legacy.py").write_text(LEGACY)
    (orch_repo / "test_legacy.py").write_text(LEGACY_TEST)
    Workspace.git(orch_repo, "add", "legacy.py", "test_legacy.py")
    Workspace.git(orch_repo, "commit", "-m", "legacy code")
    cast(list[dict[str, object]], math_plan["tasks"])[0]["files"] = ["adder.py", "legacy.py"]
    breaking = NativeResult(
        "ok",
        {
            "status": "DONE", "summary": "", "reason": "",
            "edits": [
                {"path": "adder.py", "search": "raise NotImplementedError",
                 "replace": "return a + b"},
                {"path": "legacy.py", "search": 'return "hi"', "replace": 'return "bye"'},
            ],
        },
    )
    caller = ScriptedCaller(
        {
            "plan": [NativeResult("ok", math_plan)],
            "dsh:T-001": [breaking, GOOD_ADD],
            "dsh:T-002": [GOOD_DOUBLE, GOOD_DOUBLE],
        }
    )
    orchestrator = Orchestrator(orch_repo, caller, local_runner, notify=lambda line: None)
    planned = orchestrator.plan("spec")
    run = orchestrator.execute(planned.id)
    regression = events(orchestrator, run.id, "run.regression")[0].data
    log = Workspace.git(orch_repo, "log", "--format=%s", run.branch).splitlines()
    assert planned.baseline == ["test_legacy.LegacyTest.test_greets"]
    assert (run.status, run.regressions) == ("done", 1)
    assert regression == {"culprit": "T-001", "reopened": ["T-001", "T-002"]}
    assert [line.split(" ", 1)[0] for line in log[:4]] == ["orch", "orch", "Revert", "Revert"]
    assert Workspace.git(orch_repo, "show", f"{run.branch}:legacy.py") == LEGACY.strip()


@pytest.fixture
def advancing_sleep(orchestrator: Orchestrator, clock: Clock) -> list[float]:
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.now += seconds

    orchestrator.sleep = sleep
    return slept


def test_rate_limits_halve_concurrency_back_off_and_recover(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run, advancing_sleep: list[float]
) -> None:
    limited = NativeResult("rate_limited", {}, message="429")
    caller.responses.update({"dsh:T-001": [limited, GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id, jobs=4)
    task = orchestrator.state.task(run.id, "T-001")
    assert (run.status, task.attempts, task.used_levels, orchestrator.limit) == ("done", 1, [], 4)
    assert advancing_sleep and sum(advancing_sleep) + 1e-9 >= 2.0
    assert len(events(orchestrator, run.id, "task.deferred", "T-001")) == 1


def test_persistent_rate_limits_pause_the_run(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run, advancing_sleep: list[float]
) -> None:
    caller.responses["dsh:T-001"] = [NativeResult("rate_limited", {}, message="429")] * 7
    run = orchestrator.execute(planned.id)
    assert (run.status, run.message) == ("paused", "dsh/deepseek-flash rate limited repeatedly")
    assert orchestrator.state.task(run.id, "T-001").status == "READY"


def test_only_an_empty_provider_balance_pauses_dispatch(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run
) -> None:
    # No local budget gate: DeepSeek's own HTTP 402 is the only spend stop.
    caller.responses["dsh:T-001"] = [NativeResult(
        "quota_exhausted", {}, usage={"forwarded": 1, "rejected": 402},
        message="DSH upstream HTTP 402")]
    run = orchestrator.execute(planned.id, jobs=1)
    assert run.status == "paused" and "quota_exhausted" in run.message
    assert caller.keys().count("dsh:T-001") == 1


def test_module_breaker_parks_the_rest_of_a_failing_module(
    orchestrator: Orchestrator, planned: Run
) -> None:
    state = orchestrator.state
    tasks = [
        OrchTask(f"T-01{n}", "g", ["R1"], [f"pkg/m{n}.py"], [], [], "c") for n in range(5)
    ]
    other = OrchTask("T-020", "g", ["R1"], ["other/x.py"], [], [], "c")
    run = replace(planned, id="rbreaker")
    state.create_run(run, [*tasks, other])
    for task in tasks[:3]:
        state.transition(run.id, task.id, "PARKED", "task.parked", {}, reason="ladder")
    orchestrator._breaker(run.id)
    after = {task.id: (task.status, task.reason[:8]) for task in state.tasks(run.id)}
    assert after == {
        "T-010": ("PARKED", "ladder"), "T-011": ("PARKED", "ladder"),
        "T-012": ("PARKED", "ladder"), "T-013": ("PARKED", "breaker:"),
        "T-014": ("PARKED", "breaker:"), "T-020": ("PLANNED", ""),
    }


def test_ctrl_c_during_verification_defers_instead_of_failing(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run
) -> None:
    def interrupted(
        execution: Path, scratch: Path, revision: str, names: list[str] | None,
        *, timeout: float = 25.0,
    ) -> tuple[bool, str]:
        orchestrator.cancel.set()  # what the KeyboardInterrupt handler does
        return False, "KeyboardInterrupt"

    orchestrator.runner = interrupted
    caller.responses.update({"dsh:T-001": [GOOD_ADD, GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    paused = orchestrator.execute(planned.id)
    task = orchestrator.state.task(planned.id, "T-001")
    assert (paused.status, paused.message) == ("paused", "interrupted by user")
    assert (task.status, task.attempts, task.used_levels, task.level) == ("READY", 0, [], 0)
    orchestrator.runner = local_runner
    assert orchestrator.execute(planned.id).status == "done"


def test_resume_rolls_back_an_ungated_merge_left_by_a_crash(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run
) -> None:
    integration = orchestrator.integration(planned.id)
    (integration / "adder.py").write_text("def add(a: int, b: int) -> int:\n    return 0\n")
    Workspace.git(integration, "commit", "-am", "cherry-picked but never gated")
    for status in ("READY", "RUNNING", "VERIFYING", "MERGING"):
        orchestrator.state.transition(planned.id, "T-001", status, "test.setup", {})
    caller.responses.update({"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    restored = events(orchestrator, run.id, "run.integration_restored")[0].data
    assert (run.status, restored["to"]) == ("done", planned.checkpoint)
    assert "return a + b" in Workspace.git(orchestrator.repo, "show", f"{run.branch}:adder.py")


def test_red_checkpoint_parks_the_run_instead_of_blaming_a_task(
    orch_repo: Path, math_plan: dict[str, object]
) -> None:
    (orch_repo / "legacy.py").write_text(LEGACY)
    (orch_repo / "test_legacy.py").write_text(LEGACY_TEST)
    Workspace.git(orch_repo, "add", "legacy.py", "test_legacy.py")
    Workspace.git(orch_repo, "commit", "-m", "legacy code")
    cast(list[dict[str, str]], math_plan["files"]).append(
        {"path": "legacy/__init__.py", "content": "VERSION = 2\n"}  # a package shadows legacy.py
    )
    caller = ScriptedCaller(
        {"plan": [NativeResult("ok", math_plan)], "dsh:T-001": [GOOD_ADD],
         "dsh:T-002": [GOOD_DOUBLE]}
    )
    orchestrator = Orchestrator(orch_repo, caller, local_runner, notify=lambda line: None)
    run = orchestrator.execute(orchestrator.plan("spec").id)
    assert (run.status, run.regressions) == ("parked", 0)
    assert run.message.startswith("the plan's own files break pre-existing tests")


def test_a_fault_after_merge_cannot_crash_the_run(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: object) -> None:
        raise RuntimeError("bookkeeping failed")

    monkeypatch.setattr(orchestrator, "_observe", broken)
    caller.responses.update({"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    assert run.status == "done"
    assert len(events(orchestrator, run.id, "task.handler_conflict")) == 2


def test_tasks_too_big_for_one_builder_prompt_are_sent_back_to_the_planner(
    orch_repo: Path, math_plan: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orchestragent.orch_run.DSF_BUDGET", 39919)
    bloated = copy.deepcopy(math_plan)
    cast(list[dict[str, str]], bloated["files"])[2]["content"] = MATH_TESTS + "#" * 50_000 + "\n"
    caller = ScriptedCaller(
        {"plan": [NativeResult("ok", bloated), NativeResult("ok", math_plan)]}
    )
    orchestrator = Orchestrator(orch_repo, caller, local_runner, notify=lambda line: None)
    orchestrator.plan("spec")
    retry_prompt = caller.calls[1][2]
    assert "T-001: specification, interfaces, owned files and tests require" in retry_prompt
    assert "T-002: specification, interfaces, owned files and tests require" in retry_prompt


@pytest.fixture(params=["outside", "inside"])
def linked_plan(
    orch_repo: Path, tmp_path: Path, math_plan: dict[str, object], request: pytest.FixtureRequest,
) -> Path:
    target = (tmp_path if request.param == "outside" else orch_repo) / "existing"
    target.mkdir()
    victim = target / "note.txt"
    victim.write_text("user data")
    (orch_repo / "linked").symlink_to(
        target if request.param == "outside" else Path("existing"), target_is_directory=True
    )
    Workspace.git(orch_repo, "add", ".")
    Workspace.git(orch_repo, "commit", "-m", "fixture symlink")
    cast(list[dict[str, str]], math_plan["files"]).append(
        {"path": "linked/note.txt", "content": "planner overwrote data"}
    )
    return victim


@pytest.mark.parametrize(
    ("stage", "error"), [("plan", PlanError), ("probe", EditError), ("checkpoint", PlanError)],
)
def test_planning_never_writes_through_existing_symlinks(
    linked_plan: Path, orchestrator: Orchestrator, math_plan: dict[str, object],
    stage: str, error: type[Exception],
) -> None:
    base = Workspace.git(orchestrator.repo, "rev-parse", "HEAD")
    with pytest.raises(error):
        if stage == "plan":
            orchestrator.plan("Build add and double, plus a note.", attempts=1)
        elif stage == "probe":
            orchestrator._probe_plan("rprobe", base, math_plan)
        else:
            orchestrator._checkpoint("rcheckpoint", "spec", base, math_plan,
                                     Draft(math_plan, 0, []), [])
    assert linked_plan.read_text() == "user data"
    assert Workspace.git(orchestrator.repo, "rev-parse", "HEAD") == base


def test_concurrent_replans_preserve_every_requirement_and_finish(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch, caller: ScriptedCaller,
) -> None:
    plan, bodies = synthetic_plan(Shape(tasks=4, packages=2, fan_in=0))
    entries = cast(list[dict[str, object]], plan["tasks"])
    entries[1]["depends_on"], entries[3]["depends_on"] = ["T-001"], ["T-003"]
    caller.responses["plan"] = [NativeResult("ok", plan)]
    planned = orchestrator.plan("Build two independent pairs of tasks.")
    original = {task.id: task for task in orchestrator.state.tasks(planned.id)}
    barrier = Barrier(2)

    def replan_reply(
        run_id: str, task: OrchTask, purpose: str, prompt: str, schema: dict[str, object],
    ) -> dict[str, object]:
        assert isinstance(prompt, ContextPrompt)
        assert planned.spec in prompt.shared and SPEC_AUTHORITY in prompt.shared
        barrier.wait(timeout=10)
        child = original["T-002" if task.id == "T-001" else "T-004"]
        return {"summary": "split", "tasks": [
            {**task.entry(), "id": "T-005"},
            {**child.entry(), "id": "T-006", "depends_on": ["T-005"]},
        ]}

    tasks = []
    for task_id in ("T-001", "T-003"):
        orchestrator.state.transition(planned.id, task_id, "READY", "task.ready", {})
        tasks.append(orchestrator.state.transition(
            planned.id, task_id, "RUNNING", "task.started", {}, attempts=1
        ))
    monkeypatch.setattr(orchestrator, "_claude", replan_reply)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(orchestrator._replan, planned.id, task, "BLOCKED")
                   for task in tasks]
        for future in futures:
            future.result()
    active = [t for t in orchestrator.state.tasks(planned.id) if t.status != "REPLANNED"]
    edits = {path: edit_result(path, body) for path, body in bodies.values()}
    for task in active:
        caller.responses[f"dsh:{task.id}"] = [edits[task.files[0]]]
    run = orchestrator.execute(planned.id, jobs=2)
    requirements = {tuple(t.reqs) for t in active}
    assert (run.status, requirements, {t.id for t in active}, lint(run.plan)) == (
        "done", {("R1",), ("R2",), ("R3",), ("R4",)},
        {"T-005", "T-006", "T-007", "T-008"}, [],
    )
    files = {t.id: t.files[0] for t in active}
    assert {files[t.id]: [files[dep] for dep in t.depends_on] for t in active} == {
        "pkg0/f1.py": [], "pkg1/f2.py": ["pkg0/f1.py"],
        "pkg0/f3.py": [], "pkg1/f4.py": ["pkg0/f3.py"],
    }
    assert audit(orchestrator, run.id, run_tests=False) == []


@pytest.mark.parametrize("invalid", ["existing_id", "duplicate_id", "missing_tests", "cycle",
                                   "dropped_output", "post_output_report"])
def test_replan_validation_and_state_changes_are_one_transaction(
    orchestrator: Orchestrator, planned: Run, invalid: str,
) -> None:
    state = orchestrator.state
    state.transition(planned.id, "T-001", "READY", "task.ready", {})
    state.transition(planned.id, "T-001", "RUNNING", "task.started", {})
    replacements = tasks_from(SPLIT.data)
    if invalid == "existing_id":
        replacements[0].id = "T-001"
    elif invalid == "duplicate_id":
        replacements[1].id = replacements[0].id
    elif invalid == "missing_tests":
        replacements[1].tests = []
    elif invalid == "cycle":
        replacements[0].depends_on = [replacements[1].id]
    elif invalid == "dropped_output":
        replacements[0].files = ["elsewhere.py"]
    else:
        replacements.append(OrchTask("T-005", "record dispute", ["R1"], ["report.txt"],
                                     [replacements[0].id], [replacements[0].tests.pop()], "report"))
    before = (state.run(planned.id), state.tasks(planned.id), state.events(planned.id))
    message = ("contribute" if invalid == "post_output_report" else
               "drops original" if invalid == "dropped_output" else None)
    with pytest.raises(Conflict, match=message):
        state.replan(planned.id, "T-001", ["T-001", "T-002"], replacements, "split", validate=lint)
    assert (state.run(planned.id), state.tasks(planned.id), state.events(planned.id)) == before


@pytest.mark.parametrize(
    ("fault", "committed"), [("gate", False), ("before_save", False), ("after_save", True)],
)
def test_merge_fault_preserves_only_the_durably_verified_commits(
    independent_run: Run, orchestrator: Orchestrator, caller: ScriptedCaller,
    monkeypatch: pytest.MonkeyPatch, fault: str, committed: bool,
) -> None:
    fired = False
    transition = orchestrator.state.transition

    def broken_gate(
        execution: Path, scratch: Path, revision: str, names: list[str] | None,
        *, timeout: float = 25.0,
    ) -> tuple[bool, str]:
        nonlocal fired
        if fault == "gate" and execution.name.startswith("gate-") and not fired:
            fired = True
            raise OSError("verifier unavailable")
        return local_runner(execution, scratch, revision, names, timeout=timeout)

    def broken_save(*args: Any, **kwargs: Any) -> OrchTask:
        nonlocal fired
        if fault != "gate" and args[3] == "task.done" and not fired:
            fired = True
            if committed:
                transition(*args, **kwargs)
            raise OSError("task persistence interrupted")
        return transition(*args, **kwargs)

    caller.responses.update({
        "dsh:T-001": [GOOD_ADD], "dsh:T-002": [edit_result("doubler.py", "return a * 2")],
    })
    monkeypatch.setattr(orchestrator, "runner", broken_gate)
    monkeypatch.setattr(orchestrator.state, "transition", broken_save)
    run = orchestrator.execute(independent_run.id, jobs=1)
    adder = orchestrator.git(orchestrator.repo, "show", f"{run.verified_head}:adder.py")
    assert (statuses(orchestrator, run), "return a + b" in adder) == (
        {"T-001": "DONE" if committed else "PARKED", "T-002": "DONE"}, committed,
    )
    if not committed:
        orchestrator.state.transition(run.id, "T-001", "READY", "task.retry", {})
        caller.responses["dsh:T-001"] = [GOOD_ADD]
    resumed = orchestrator.execute(run.id, jobs=1)
    assert (resumed.status, audit(orchestrator, run.id, run_tests=False)) == ("done", [])


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_test_correction_fault_rolls_back_code_and_plan(
    orchestrator: Orchestrator, finished: Run, error: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: Any, **kwargs: Any) -> tuple[bool, str]:
        raise error("verification interrupted")

    monkeypatch.setattr(orchestrator, "runner", broken)
    task = orchestrator.state.task(finished.id, "T-001")
    with pytest.raises(error):
        orchestrator._fix_tests(finished, task, ["test_mathops.py"], [
            {"path": "test_mathops.py", "search": "add(2, 3), 5", "replace": "add(3, 3), 6"}
        ], "test correction")
    integration = orchestrator.integration(finished.id)
    assert (orchestrator.git(integration, "rev-parse", "HEAD"),
            orchestrator.git(integration, "status", "--porcelain"),
            orchestrator.state.run(finished.id)) == (finished.verified_head, "", finished)


def test_failed_rollback_stops_other_merges_until_resume(
    independent_run: Run, orchestrator: Orchestrator, caller: ScriptedCaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = orchestrator.git
    failed = False

    def broken_git(cwd: Path, *args: str, author: str = "") -> str:
        nonlocal failed
        if args[:2] == ("reset", "--hard") and not failed:
            failed = True
            raise RuntimeError("rollback unavailable")
        return git(cwd, *args, author=author)

    def broken_gate(
        execution: Path, scratch: Path, revision: str, names: list[str] | None,
        *, timeout: float = 25.0,
    ) -> tuple[bool, str]:
        if execution.name.startswith("gate-"):
            raise OSError("verifier unavailable")
        return local_runner(execution, scratch, revision, names, timeout=timeout)

    replies = {"dsh:T-001": [GOOD_ADD],
               "dsh:T-002": [edit_result("doubler.py", "return a * 2")]}
    caller.responses.update(copy.deepcopy(replies))
    monkeypatch.setattr(orchestrator, "git", broken_git)
    monkeypatch.setattr(orchestrator, "runner", broken_gate)
    paused = orchestrator.execute(independent_run.id, jobs=2)
    assert paused.status == "paused" and paused.message.startswith("integration rollback failed")
    assert paused.verified_head == independent_run.checkpoint
    assert all(t.status != "DONE" for t in orchestrator.state.tasks(paused.id))
    monkeypatch.setattr(orchestrator, "runner", local_runner)
    caller.responses.update(replies)
    resumed = orchestrator.execute(paused.id, jobs=2)
    assert (resumed.status, audit(orchestrator, resumed.id, run_tests=False)) == ("done", [])


def test_a_building_on_task_reads_its_dependencies_files_on_the_first_attempt(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    caller.responses.update({"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    assert orchestrator.execute(planned.id).status == "done"
    prompts = {key: str(text) for _, key, text in caller.calls if key.startswith("dsh:")}
    assert "REQUESTED READ-ONLY CONTEXT" not in prompts["dsh:T-001"]  # builds on nothing
    seeded = prompts["dsh:T-002"].split("## REQUESTED READ-ONLY CONTEXT (pinned revision)\n")
    assert len(seeded) == 2 and seeded[1].startswith("--- adder.py ---\n")
    assert orchestrator.state.task(planned.id, "T-002").context_selectors == []


def read(*selectors: str) -> NativeResult:
    return NativeResult("ok", {"status": "NEED_FILES", "edits": [], "need_files": list(selectors)},
                        usage={"input_tokens": 100, "output_tokens": 20, "settled_cents": 1})


def test_dependency_files_stay_next_to_the_tasks_own_reads(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
) -> None:
    caller.responses.update({"dsh:T-001": [GOOD_ADD],
                             "dsh:T-002": [read("test_mathops.py"), GOOD_DOUBLE]})
    assert orchestrator.execute(planned.id).status == "done"
    second = [str(p) for _, key, p in caller.calls if key == "dsh:T-002"][-1]
    requested = second.split("## REQUESTED READ-ONLY CONTEXT (pinned revision)\n")[1]
    assert "--- adder.py ---" in requested and "--- test_mathops.py ---" in requested
    assert orchestrator.state.task(planned.id, "T-002").context_selectors == ["test_mathops.py"]


@pytest.mark.parametrize("repeats", [1, 2])
def test_a_read_of_already_supplied_context_is_answered_not_parked(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run, repeats: int,
) -> None:
    caller.responses.update({
        "dsh:T-001": [GOOD_ADD],
        "dsh:T-002": [*[read("adder.py", "adder.py::add")] * repeats, GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id)
    task = orchestrator.state.task(planned.id, "T-002")
    prompts = [str(p) for _, key, p in caller.calls if key == "dsh:T-002"]
    assert run.status == "done" and task.status == "DONE"
    assert task.context_requests == 0 and task.context_selectors == []
    assert "already included above under REQUESTED READ-ONLY CONTEXT" in prompts[1]
    # A second repeat fails that attempt; the ladder retries instead of parking the task.
    assert (task.attempts, task.level) == ((1, 0) if repeats == 1 else (2, 1))
    assert [e.data["kind"] for e in orchestrator.state.events(planned.id, "T-002")
            if e.kind == "attempt.format_repair"] == ["context"]


def test_first_build_effort_switch_applies_only_to_a_tasks_first_dsf_attempt(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orchestragent.orch_run.DSF_BUILD_FIRST",
                        Route("dsh", "deepseek-flash", "off"))
    caller.responses.update({"dsh:T-001": [BAD_ADD, GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id, jobs=1)
    builds = [(c.task_id, c.attempt, c.effort) for c in orchestrator.state.usage.calls(run.id)
              if c.purpose == "build"]
    assert run.status == "done"
    assert ("T-001", 1, "off-extended") in builds and ("T-002", 1, "off-extended") in builds
    assert [e for t, a, e in builds if t == "T-001" and a > 1] == ["low-extended"]


def test_a_registered_byok_builder_runs_the_whole_ladder_entry(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = Route("myapi", "some-model", "")
    monkeypatch.setattr("orchestragent.orch_run.DSF", builder)
    monkeypatch.setattr("orchestragent.orch_run.DSF_BUILD_FIRST", builder)
    monkeypatch.setattr("orchestragent.orch_run.DSF_EXTENDED", builder)
    caller.responses.update({"myapi:T-001": [GOOD_ADD], "myapi:T-002": [GOOD_DOUBLE]})
    run = orchestrator.execute(planned.id, jobs=1)
    builds = {(c.provider, c.model, c.effort) for c in orchestrator.state.usage.calls(run.id)
              if c.purpose == "build"}
    assert run.status == "done" and builds == {("myapi", "some-model", "")}
