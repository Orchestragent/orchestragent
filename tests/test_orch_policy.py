import copy
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from orchestragent.envelope import digest
from orchestragent.native import NativeResult
from orchestragent.orch_policy import Policy, PolicyStore, Tuning, evaluate
from orchestragent.orch_run import Orchestrator, PlanError
from orchestragent.orch_state import OrchState, Run
from orchestragent.orch_usage import Call
from orchestragent.serialization import dumps
from orchestragent.store import Conflict
from tests.conftest import ADD_DESIGN, DOUBLE_DESIGN, GOOD_ADD, GOOD_DOUBLE, ScriptedCaller

LESSON = "Check both operands before returning an arithmetic result."


@pytest.fixture
def policies(tmp_path: Path) -> PolicyStore:
    registry = PolicyStore(OrchState(tmp_path / "state.sqlite"))
    registry.enable()
    registry.state.add_rule(LESSON, "training-run")
    return registry


@pytest.fixture
def candidate(policies: PolicyStore) -> Policy:
    return policies.snapshot(("r1",))


def evidence_reports(policy: Policy) -> tuple[dict[str, Any], dict[str, Any]]:
    cases = [{"id": f"case-{i}", "spec": f"held out specification {i}"} for i in range(6)]
    trials = [{"id": f"case-{i}-{repeat}", "case_id": f"case-{i}",
               "input_hash": digest(str(i)), "acceptance_hash": digest(f"test-{i}"),
               "status": "passed", "acceptance_passed": True}
              for i in range(6) for repeat in (1, 2)]
    baseline: dict[str, Any] = {"mode": "real", "synthetic": False, "case_kind": "curated",
                "policy_id": policy.parent, "suite_snapshot": {"cases": cases},
                "suite_hash": digest(dumps({"cases": cases})), "success_rate": 1.0,
                "expected_trials": 12, "completed_trials": 12, "trials": trials,
                "config": {"engine_source_hash": "engine", "jobs": 2},
                "api_accounting": {"currency": "CNY", "settled_cents": 100,
                                   "unsettled_calls": 0, "known_reserved_cents": 0,
                                   "missing_reservation_calls": 0},
                "totals": {"pending_calls": 0, "legacy_calls": 0, "calls": 12,
                           "reported_calls": {"total_tokens": 12},
                           "known_tokens": {"total_tokens": 12000}}}
    proposed = copy.deepcopy(baseline)
    proposed["policy_id"] = policy.id
    proposed["totals"]["known_tokens"]["total_tokens"] = 11000
    return baseline, proposed


def test_snapshot_is_immutable_and_reopens_with_content_identity(
    policies: PolicyStore, candidate: Policy,
) -> None:
    [rule] = policies.state.rules()
    policies.state.save_rule(replace(rule, text="A different learned rule after the snapshot."))
    reopened = PolicyStore(OrchState(policies.state.store.path))
    assert reopened.get(candidate.id).rules[0].text == LESSON
    assert reopened.current() == policies.get(candidate.parent)
    tampered = asdict(candidate)
    tampered["rules"][0]["text"] = "tampered"
    with pytest.raises(ValueError, match="fingerprint"):
        Policy.decode(tampered)


def test_measured_promotion_is_atomic_stale_evidence_cannot_win_and_rollback_is_audited(
    policies: PolicyStore, candidate: Policy,
) -> None:
    baseline, proposed = evidence_reports(candidate)
    result = policies.promote(candidate.id, baseline, proposed)
    assert (result.pairs, result.cases, result.candidate_tokens) == (12, 6, 11000)
    assert policies.current() == candidate
    with pytest.raises(Conflict, match="changed"):
        policies.promote(candidate.id, baseline, proposed)
    assert policies.rollback(candidate.parent).id == candidate.parent
    assert [row["action"] for row in policies.history()] == ["enabled", "promoted", "rolled_back"]
    assert policies.get(candidate.id) == candidate


def test_unaccepted_version_is_not_a_rollback_target(
    policies: PolicyStore, candidate: Policy,
) -> None:
    with pytest.raises(ValueError, match="never"):
        policies.rollback(candidate.id)
    assert policies.current() == policies.get(candidate.parent)


@pytest.mark.parametrize("stamp", [123.123456789, 1793306562.4160068])
def test_snapshot_round_trip_uses_shared_float_normalization(
    policies: PolicyStore, stamp: float,
) -> None:
    policies.state.clock = lambda: stamp
    policy = policies.snapshot(("r1",))
    assert policies.get(policy.id) == policy


def test_legacy_policy_identity_and_tuning_validation(policies: PolicyStore) -> None:
    active = policies.current()
    assert active is not None
    body = asdict(active)
    body.pop("id")
    body.pop("tuning")
    legacy = Policy.decode({**body, "id": digest(dumps(body))})
    policies.save(legacy)
    assert policies.get(legacy.id) == legacy and legacy.tuning is None
    with pytest.raises(ValueError, match="tuning"):
        Tuning(jobs=True)
    candidate = policies.snapshot(tuning=Tuning(jobs=8, test_jobs=2, cache_mode="off"))
    assert policies.get(candidate.id).tuning == candidate.tuning
    assert policies.current() == active and active.id == candidate.parent


@pytest.fixture
def scheduling_evidence(policies: PolicyStore) -> tuple[Policy, dict[str, Any], dict[str, Any]]:
    candidate = policies.snapshot(tuning=Tuning(jobs=1, verify_jobs=1))
    before, after = evidence_reports(candidate)
    after["totals"]["known_tokens"]["total_tokens"] = 12000  # gain comes from elapsed time
    for report, policy, jobs, elapsed in (
        (before, policies.get(candidate.parent), 2, 10), (after, candidate, 1, 8),
    ):
        report["policy_snapshot"] = asdict(policy)
        for row in report["trials"]:
            row.update(elapsed_seconds=elapsed, execution_config={
                "jobs": jobs, "cache_mode": "auto", "verify_jobs": 1, "policy_id": policy.id})
    return candidate, before, after


@pytest.mark.parametrize("defect", ["none", "missing_time", "infinite_time", "slow", "no_gain",
                                   "overridden", "partial_acceptance", "snapshot"])
def test_scheduling_promotion_needs_complete_delivery_and_actual_frozen_execution(
    policies: PolicyStore, scheduling_evidence: tuple[Policy, dict[str, Any], dict[str, Any]],
    defect: str,
) -> None:
    candidate, before, after = scheduling_evidence
    if defect == "missing_time":
        after["trials"][0].pop("elapsed_seconds")
    elif defect in {"slow", "no_gain", "infinite_time"}:
        for row in after["trials"]:
            row["elapsed_seconds"] = {"slow": 12, "no_gain": 10,
                                      "infinite_time": float("inf")}[defect]
    elif defect == "overridden":
        after["trials"][0]["execution_config"]["jobs"] = 4
    elif defect == "partial_acceptance":
        for report in (before, after):
            report["trials"][0].update(status="failed", acceptance_passed=False)
    elif defect == "snapshot":
        after["policy_snapshot"]["tuning"]["jobs"] = 4
    if defect != "none":
        with pytest.raises(ValueError):
            policies.promote(candidate.id, before, after)
        assert policies.current() == policies.get(candidate.parent)
    else:
        result = policies.promote(candidate.id, before, after)
        assert (result.baseline_seconds, result.candidate_seconds) == (120, 96)
        assert policies.rollback(candidate.parent).tuning is None


@pytest.mark.parametrize("observation,expected", [
    ("backlog", 8), ("rate_limit", 2), ("serial", None), ("resumed", None), ("unknown", None),
])
def test_observed_scheduling_hypothesis_stages_one_parameter_without_activation(
    orchestrator: Orchestrator, planned: Run, observation: str, expected: int | None,
) -> None:
    registry = orchestrator.policies
    parent = registry.enable(tuning=Tuning())
    state = orchestrator.state
    state.update_run(planned.id, "fixture.completed", {}, status="done", policy_id=parent.id)
    state.record(planned.id, "run.started", {"jobs": 4})
    state.record(planned.id, "run.metrics", {
        "peak_generating": 4, "ready_backlog_peak": int(observation != "serial")})
    if observation == "resumed":
        state.record(planned.id, "run.started", {"jobs": 4})
    if observation in {"rate_limit", "unknown"}:
        for number in range(2):
            receipt = Call(f"fixture-{number}", planned.id, "dsh", "deepseek-flash", "build", 0,
                           scope="execution")
            with state.usage.capture(receipt):
                receipt.outcome = "rate_limited" if observation == "rate_limit" else "timeout"
                receipt.usage = {"settled_cents": 0} if observation == "rate_limit" else {}
    if expected is None:
        with pytest.raises(ValueError):
            registry.suggest(planned.id)
    else:
        candidate = registry.suggest(planned.id)
        assert candidate.tuning == replace(Tuning(), jobs=expected)
        assert digest(planned.spec) in candidate.training_specs
        assert registry.history()[-1]["action"] == "suggested"
        with pytest.raises(ValueError, match="never"):
            registry.rollback(candidate.id)
    assert registry.current() == parent


def test_promoted_evaluation_specs_are_unavailable_to_future_candidates(
    policies: PolicyStore, candidate: Policy,
) -> None:
    policies.promote(candidate.id, *evidence_reports(candidate))
    following = policies.snapshot()
    assert digest("held out specification 0") in following.training_specs
    with pytest.raises(ValueError, match="overlaps"):
        evaluate(following, *evidence_reports(following))


def test_failed_planning_is_also_training_exposure(
    orchestrator: Orchestrator, caller: ScriptedCaller,
) -> None:
    registry = orchestrator.policies
    registry.enable()
    caller.responses["plan"] = [NativeResult("auth_required", {})]
    with pytest.raises(PlanError):
        orchestrator.plan("held out specification 0")
    assert not orchestrator.state.runs()
    policy = registry.snapshot()
    assert digest("held out specification 0") in policy.training_specs
    with pytest.raises(ValueError, match="overlaps"):
        evaluate(policy, *evidence_reports(policy))


@pytest.mark.parametrize("case", ["mock", "synthetic", "incomplete", "unknown_tokens",
                                 "legacy", "config", "regression", "acceptance",
                                 "duplicate_trial", "few_specs", "no_gain", "token_bloat",
                                 "inputs", "wrong_policy", "error", "api_unknown", "api_missing",
                                 "cost_bloat", "api_negative"])
def test_evidence_gate_rejects_incomparable_or_unsafe_results(candidate: Policy, case: str) -> None:
    baseline, proposed = evidence_reports(candidate)
    if case == "mock":
        proposed["mode"] = "mock"
    elif case == "synthetic":
        proposed["case_kind"] = "synthetic"
    elif case == "incomplete":
        proposed["success_rate"] = None
    elif case == "unknown_tokens":
        proposed["totals"]["reported_calls"]["total_tokens"] = 11
    elif case == "legacy":
        proposed["totals"]["legacy_calls"] = 1
    elif case == "config":
        proposed["config"]["engine_source_hash"] = "other"
    elif case == "regression":
        proposed["trials"][0].update(status="failed", acceptance_passed=False)
    elif case == "acceptance":
        proposed["trials"][0]["acceptance_passed"] = None
    elif case == "duplicate_trial":
        proposed["trials"][0] = proposed["trials"][1]
    elif case == "few_specs":
        for row in proposed["suite_snapshot"]["cases"]:
            row["spec"] = "one repeated specification"
    elif case in {"no_gain", "token_bloat"}:
        proposed["totals"]["known_tokens"]["total_tokens"] = 12000 if case == "no_gain" else 14000
    elif case == "inputs":
        proposed["trials"][0]["acceptance_hash"] = "changed"
    elif case == "wrong_policy":
        proposed["policy_id"] = "wrong"
    elif case == "error":
        proposed["trials"][0].update(status="error", acceptance_passed=False)
    elif case == "api_unknown":
        proposed["api_accounting"]["unsettled_calls"] = 1
    elif case == "api_missing":
        proposed.pop("api_accounting")
    elif case == "cost_bloat":
        proposed["api_accounting"]["settled_cents"] = 111
    elif case == "api_negative":
        proposed["api_accounting"]["settled_cents"] = -1
    with pytest.raises(ValueError):
        evaluate(candidate, baseline, proposed)


def test_training_overlap_and_oversized_rule_layer_are_rejected(
    policies: PolicyStore, candidate: Policy,
) -> None:
    body = asdict(candidate)
    body.pop("id")
    body["training_specs"] = (digest("held out specification 0"),)
    contaminated = Policy.decode({"id": digest(dumps(body)), **body})
    with pytest.raises(ValueError, match="overlaps"):
        evaluate(contaminated, *evidence_reports(contaminated))
    for number in range(7):
        policies.state.add_rule(f"{number} " + "x" * 230, "training")
    with pytest.raises(ValueError, match="oversized"):
        policies.snapshot(tuple(f"r{number}" for number in range(1, 9)))


def test_resume_keeps_the_planned_policy_and_new_runs_use_the_promoted_version(
    orchestrator: Orchestrator, caller: ScriptedCaller, math_plan: dict[str, object],
) -> None:
    registry = orchestrator.policies
    original = registry.enable()
    registry.state.add_rule(LESSON, "training")
    proposed = registry.snapshot(("r1",))
    caller.responses.update({
        "plan": [NativeResult("ok", copy.deepcopy(math_plan)) for _ in range(2)],
        "dsh:T-001": [ADD_DESIGN, GOOD_ADD, ADD_DESIGN, GOOD_ADD],
        "dsh:T-002": [DOUBLE_DESIGN, GOOD_DOUBLE, DOUBLE_DESIGN, GOOD_DOUBLE]})
    first = orchestrator.plan("first spec")
    registry.promote(proposed.id, *evidence_reports(proposed))
    first = orchestrator.execute(first.id)
    assert first.status == "done" and first.policy_id == original.id
    assert LESSON not in caller.calls[-1][2]
    second = orchestrator.execute(orchestrator.plan("second spec").id)
    assert second.status == "done" and second.policy_id == proposed.id
    assert LESSON in caller.calls[-1][2]
    assert registry.state.rules()[0].status == "candidate"
    assert not any(event.kind == "rule.trial" for event in registry.state.events(second.id))
