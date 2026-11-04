import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

import pytest

from orchestragent.native import NativeClients, NativeResult
from orchestragent.orch_cli import main
from orchestragent.orch_run import Orchestrator, PlanError
from orchestragent.orch_state import OrchState, Run
from orchestragent.orch_usage import (
    Call,
    output_cut,
    report,
    settled_json_failure,
    summary,
    tokens,
    transient,
    unresolved,
    usage_fields,
)
from tests.conftest import (
    ADD_SHARD,
    BAD_ADD,
    DOUBLE_SHARD,
    GOOD_ADD,
    GOOD_DOUBLE,
    Clock,
    ScriptedCaller,
    component_design,
    local_runner,
)

USAGE: dict[str, object] = {"input_tokens": 100, "output_tokens": 20,
                          "cache_read_input_tokens": 60, "cache_creation_input_tokens": 40}


@pytest.mark.parametrize(("outcome", "usage", "unknown"), [
    ("pending", {}, True), ("exception:TimeoutError", {}, True),
    ("timed_out", {}, True), ("timed_out", {"forwarded": 0}, False),
    ("timeout", {"forwarded": 0}, False), ("cancelled", {"forwarded": 1}, True),
    ("failed", {"forwarded": 1, "reserved_cents": 300}, True),
    ("failed", {"forwarded": 1, "reserved_cents": 300, "settled_cents": 1}, False),
    ("rate_limited", {"forwarded": 1, "reserved_cents": 300, "rejected": 429}, False),
    ("failed", {"forwarded": 1, "reserved_cents": 300, "rejected": 408}, True),
    ("failed", {"forwarded": 1, "reserved_cents": 300, "rejected": True}, True),
])
def test_reconciliation_distinguishes_unknown_exposure_from_proven_rejection(
    outcome: str, usage: dict[str, object], unknown: bool,
) -> None:
    assert unresolved(outcome, usage) is unknown


@pytest.mark.parametrize(
    ("provider", "usage", "expected"),
    [
        ("claude", USAGE, (200, 20, 220, 60, 40)),
        ("dsh", {**USAGE, "reasoning_tokens": 10, "total_tokens": 220},
         (200, 20, 220, 60, 40)),
        ("codex", {"input_tokens": 200, "output_tokens": 20, "cached_input_tokens": 60},
         (200, 20, 220, 60, None)),
        ("claude", {"input_tokens": 100, "output_tokens": 20},
         (None, 20, None, None, None)),
        ("dsh", {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
         (100, 20, 120, None, None)),
        ("codex", {"input_tokens": True, "output_tokens": -1},
         (None, None, None, None, None)),
        ("claude", {**USAGE, "total_tokens": 999}, (None, 20, None, 60, 40)),
        ("dsh", {"input_tokens": 100, "output_tokens": 20, "total_tokens": 90},
         (None, 20, None, None, None)),
    ],
)
def test_provider_accounting_keeps_cache_and_reasoning_from_double_counting(
    provider: str, usage: dict[str, object], expected: tuple[int | None, ...],
) -> None:
    result = tokens(provider, usage)
    assert (result.input_tokens, result.output_tokens, result.total_tokens,
            result.cache_read_tokens, result.cache_write_tokens) == expected


def test_partial_totals_do_not_turn_unknown_cache_into_a_zero_hit_rate() -> None:
    complete = Call("c1", "r1", "claude", "model", "plan", 1, usage=USAGE)
    missing = Call("c2", "r1", "claude", "model", "plan", 2)
    assert summary([complete])["cache_hit_rate"] == 0.3
    partial = summary([complete, missing])
    assert partial["cache_hit_rate"] is None
    assert partial["known_tokens"] == {
        "input_tokens": 200, "output_tokens": 20, "total_tokens": 220,
        "cache_read_tokens": 60, "cache_write_tokens": 40, "reasoning_tokens": 0,
    }
    assert partial["reported_calls"] == {
        "input_tokens": 1, "output_tokens": 1, "total_tokens": 1,
        "cache_read_tokens": 1, "cache_write_tokens": 1, "reasoning_tokens": 0,
    }


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_receipt_survives_provider_exceptions_before_a_plan_exists(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException],
) -> None:
    def broken(*args: object, **kwargs: object) -> NativeResult:
        raise failure("SECRET provider body")

    monkeypatch.setattr(orchestrator, "call", broken)
    with pytest.raises(failure):
        orchestrator.plan("SECRET user specification")
    [call] = orchestrator.state.usage.calls()  # the planner's look-around is the first call
    assert call.outcome == f"exception:{failure.__name__}"
    assert (call.purpose, call.scope, call.task_id, call.attempt) == ("look", "planning", "", 1)
    assert call.finished_at is not None and call.prompt_hash
    assert "SECRET" not in json.dumps(asdict(call))
    assert not orchestrator.state.runs()


def test_rejected_plans_remain_queryable_from_cli(
    orchestrator: Orchestrator, caller: ScriptedCaller, orch_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caller.responses["plan"] = [NativeResult("failed", {}, usage=USAGE)]
    with pytest.raises(PlanError):
        orchestrator.plan("spec")
    capsys.readouterr()
    assert main(["--repo", str(orch_repo), "cost", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["groups"]["[shared]"]["known_tokens"]["total_tokens"] == 220
    assert [c["outcome"] for c in result["calls"] if c["purpose"] == "plan"] == ["failed"]


def test_parallel_receipts_survive_reopening_without_losing_calls(
    orchestrator: Orchestrator,
) -> None:
    state = orchestrator.state

    def complete(number: int) -> None:
        with state.usage.capture(Call(str(number), "r-test", "dsh", "model", "build", 1)) as call:
            call.usage = {**USAGE, "secret": "DO_NOT_STORE", "cost_usd": float("nan")}
            call.outcome = "ok"

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(complete, range(20)))
    reopened = OrchState(state.store.path)
    calls = reopened.usage_calls("r-test")
    assert len(calls) == len({call.id for call in calls}) == 20
    assert summary(calls)["known_tokens"] == {
        "input_tokens": 4000, "output_tokens": 400, "total_tokens": 4400,
        "cache_read_tokens": 1200, "cache_write_tokens": 800, "reasoning_tokens": 0,
    }
    assert all(call.usage == USAGE for call in calls)


def test_inflight_receipt_is_visible_before_result_and_remains_unknown(
    orchestrator: Orchestrator,
) -> None:
    state = orchestrator.state
    with state.usage.capture(Call("inflight", "r-test", "claude", "model", "plan", 1)):
        [call] = OrchState(state.store.path).usage.calls("r-test")
        assert (call.outcome, call.finished_at, tokens(call.provider, call.usage).total_tokens) == (
            "pending", None, None,
        )


def test_retry_receipts_link_to_events_without_double_counting(
    orch_repo: Path, caller: ScriptedCaller, clock: Clock, math_plan: dict[str, object],
    capsys: pytest.CaptureFixture[str], unsandboxed: None,
) -> None:
    caller.responses = {
        "plan": [NativeResult("ok", math_plan, usage=USAGE)],
        "dsh:design:root": [replace(component_design(ADD_SHARD, DOUBLE_SHARD), usage=USAGE)],
        "dsh:T-001": [replace(BAD_ADD, usage=USAGE), replace(GOOD_ADD, usage=USAGE)],
        "dsh:T-002": [replace(GOOD_DOUBLE, usage=USAGE)],
    }
    orch = Orchestrator(orch_repo, caller, local_runner, clock=clock, notify=lambda _: None)
    run = orch.execute(orch.plan("spec").id)
    calls = orch.state.usage_calls(run.id)
    events = [e for e in orch.state.events(run.id) if e.kind == "attempt.result"]
    assert run.status == "done" and len(calls) == 6  # look, planner, designer, 3 builds
    assert {e.data["call_id"] for e in events} == {
        c.id for c in calls if c.task_id and c.scope == "execution"}
    assert cast(dict[str, int], summary(calls)["known_tokens"])["total_tokens"] == 1100
    assert all(not c.legacy for c in calls)
    task_calls = [c for c in calls if c.task_id == "T-001"]
    assert {c.purpose for c in task_calls} == {"build"}  # its test design is the component's
    assert [c.attempt for c in task_calls] == [1, 2]
    capsys.readouterr()
    assert main(["--repo", str(orch_repo), "cost", "--task", "T-001", "--json"]) == 0
    filtered = json.loads(capsys.readouterr().out)
    assert filtered["totals"]["known_tokens"]["total_tokens"] == 440
    assert len(filtered["calls"]) == 2
    assert main(["--repo", str(orch_repo), "cost", "--call", task_calls[0].id, "--json"]) == 0
    exact = json.loads(capsys.readouterr().out)
    assert [c["id"] for c in exact["calls"]] == [task_calls[0].id]
    assert main(["--repo", str(orch_repo), "cost", "--calls", "--task", "T-001"]) == 0
    detail = capsys.readouterr().out
    assert all(c.id in detail for c in task_calls)
    assert "reported_usage=" in detail and "prefix=" in detail


def test_legacy_events_remain_available_and_are_explicitly_incomplete(
    orchestrator: Orchestrator, planned: Run,
) -> None:
    orchestrator.state.record(planned.id, "attempt.result", {
        "route": "dsh/deepseek-flash", "usage": USAGE, "outcome": "ok",
    }, "T-001", 0)
    calls = orchestrator.state.usage_calls(planned.id)
    legacy = [call for call in calls if call.legacy]
    assert len(legacy) == 1 and legacy[0].task_id == "T-001"
    assert summary(calls)["legacy_calls"] == 1
    assert report(calls, "task")["schema_version"] == 1


def test_invalid_codex_answer_still_preserves_billed_usage() -> None:
    events = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "bad JSON"}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 5}},
    ]
    raw = "\n".join(json.dumps(event) for event in events).encode()
    result = NativeClients._decode("codex", 0, raw, b"")
    assert (result.outcome, result.usage) == (
        "failed", {"input_tokens": 100, "output_tokens": 5},
    )


@pytest.mark.parametrize(("raw", "kept"), [
    ({"wire_max_tokens": 8192, "wire_thinking": "disabled", "wire_effort": "absent",
      "stop_reason": "max_tokens", "turn_end": "max-tokens", "dsf_transport": "cli-account"},
     {"wire_max_tokens": 8192, "wire_thinking": "disabled", "wire_effort": "absent",
      "stop_reason": "max_tokens", "turn_end": "max-tokens", "dsf_transport": "cli-account"}),
    ({"wire_max_tokens": 0, "wire_thinking": "secret", "stop_reason": "<body>",
      "turn_end": 3, "dsf_transport": "proxy", "request_body": "x"}, {}),
    ({"wire_max_tokens": True, "wire_effort": ["high"]}, {}),
])
def test_wire_labels_survive_only_as_declared_enums_and_positive_limits(
    raw: dict[str, object], kept: dict[str, object],
) -> None:
    assert usage_fields(raw) == kept


@pytest.mark.parametrize(("usage", "cut"), [
    ({"stop_reason": "max_tokens"}, True),
    ({"turn_end": "max-tokens"}, True),
    ({"stop_reason": "end_turn", "turn_end": "completed"}, False),
    ({"output_tokens": 32768}, False),  # a round count alone is not a provider stop reason
])
def test_output_cut_needs_the_provider_stop_reason(usage: dict[str, object], cut: bool) -> None:
    assert output_cut(usage) is cut


@pytest.mark.parametrize(("raw", "kept"), [
    ({"turn_error": "TIMEOUT"}, {"turn_error": "TIMEOUT"}),
    ({"turn_error": "OTHER"}, {"turn_error": "OTHER"}),
    ({"turn_error": "timeout: SECRET"}, {}),
    ({"turn_error": ["TIMEOUT"]}, {}),
])
def test_turn_error_keeps_only_a_short_code(
    raw: dict[str, object], kept: dict[str, object],
) -> None:
    assert usage_fields(raw) == kept


def test_an_errored_turn_without_settlement_stays_unknown() -> None:
    usage: dict[str, object] = {"forwarded": 1, "reserved_cents": 300, "turn_end": "error",
                                "turn_error": "TIMEOUT"}
    message = "Native call did not complete successfully; stop=error; error_code=TIMEOUT"
    assert unresolved("failed", usage) and not settled_json_failure("failed", message, usage)
    assert not output_cut(usage)


@pytest.mark.parametrize(("outcome", "message", "usage", "expected"), [
    ("failed", "DSH process stopped without a valid result",
     {"forwarded": 1, "turn_error": "STREAM_CLOSED"}, True),
    ("failed", "DSH upstream HTTP 503", {"forwarded": 1}, True),
    ("failed", "DSH upstream HTTP 400", {"forwarded": 1, "rejected": 400}, False),
    ("rate_limited", "DSH upstream HTTP 429", {"forwarded": 1, "rejected": 429}, False),
    ("exception:OSError", "", {}, True),
    ("failed", "Invalid DSH JSON result", {"forwarded": 1, "settled_cents": 2}, False),
    ("failed", "", {"forwarded": 1, "turn_error": "TIMEOUT", "stop_reason": "max_tokens"}, False),
    ("timeout", "", {"forwarded": 1}, False),
    ("policy_blocked", "suite budget unavailable", {"forwarded": 0}, False),
    ("auth_required", "", {}, False),
])
def test_transient_names_only_failures_a_repeat_can_fix(
    outcome: str, message: str, usage: dict[str, object], expected: bool,
) -> None:
    assert transient(outcome, message, usage) is expected
