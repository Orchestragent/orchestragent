import copy
from dataclasses import replace

import pytest

from orchestragent.native import NativeResult
from orchestragent.orch_build import parse_trailers
from orchestragent.orch_rules import RULE_BYTES, Rule, arm, layer, lesson, verdict
from orchestragent.orch_run import Orchestrator
from orchestragent.orch_state import Run
from orchestragent.workspace import Workspace
from tests.conftest import (
    ADD_DESIGN,
    BAD_ADD,
    DOUBLE_DESIGN,
    GOOD_ADD,
    GOOD_DOUBLE,
    ScriptedCaller,
)

LESSON = "Return the arithmetic result directly; never invert the requested operator."


def rule(stats: tuple[int, int, int, int], status: str = "candidate", rid: str = "r1") -> Rule:
    return Rule(rid, f"rule {rid}", "src", 0.0, status, *stats)


@pytest.mark.parametrize(
    ("stats", "status", "expected"),
    [
        ((6, 6, 2, 6), "candidate", "active"),
        ((5, 5, 2, 6), "candidate", "candidate"),
        ((4, 6, 3, 6), "candidate", "candidate"),
        ((1, 6, 4, 6), "candidate", "retired"),
        ((0, 6, 6, 6), "active", "active"),
    ],
)
def test_candidates_move_only_on_measured_evidence(
    stats: tuple[int, int, int, int], status: str, expected: str
) -> None:
    assert verdict(rule(stats, status))[0] == expected


def test_active_layer_keeps_the_best_rules_within_budget() -> None:
    long = "x" * (RULE_BYTES // 2)
    rules = [
        replace(rule((6, 6, 3, 6), "active", "r1"), text=long),
        replace(rule((6, 6, 1, 6), "active", "r2"), text=long),
        replace(rule((6, 6, 2, 6), "active", "r3"), text=long),
        rule((6, 6, 0, 6), "candidate", "r4"),
    ]
    text, ids, evicted = layer(rules)
    assert (ids, [r.id for r in evicted]) == (["r2"], ["r3", "r1"])
    assert text == f"- {long}\n"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(f"  {LESSON}\n", LESSON), ("too short", ""), ("x" * 241, ""), (None, "")],
)
def test_lessons_are_single_bounded_lines(value: object, expected: str) -> None:
    assert lesson(value) == expected


def test_arms_are_deterministic_and_balanced() -> None:
    shown = [arm("r1", f"T-{n:03d}", 1) for n in range(200)]
    assert shown == [arm("r1", f"T-{n:03d}", 1) for n in range(200)]
    assert 70 <= sum(shown) <= 130


def taught(result: NativeResult) -> NativeResult:
    return replace(result, data={**result.data, "lesson": LESSON})


def test_successful_claude_patch_becomes_a_candidate_rule(
    orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run
) -> None:
    caller.responses.update(
        {"dsh:T-001": [BAD_ADD, BAD_ADD], "claude:T-001": [taught(GOOD_ADD)],
         "dsh:T-002": [GOOD_DOUBLE]}
    )
    orchestrator.execute(planned.id)
    [learned] = orchestrator.state.rules()
    assert (learned.id, learned.status, learned.text) == ("r1", "candidate", LESSON)
    assert learned.source.startswith(f"{planned.id}/T-001/a3 test:")


def test_candidates_are_trialled_then_promoted_into_the_shared_prefix(
    orchestrator: Orchestrator, caller: ScriptedCaller, math_plan: dict[str, object]
) -> None:
    orchestrator.state.add_rule(LESSON, "seed")
    caller.responses.update(
        {"plan": [NativeResult("ok", copy.deepcopy(math_plan))] * 2,
         "dsh:T-001": [ADD_DESIGN, GOOD_ADD, ADD_DESIGN, GOOD_ADD],
         "dsh:T-002": [DOUBLE_DESIGN, GOOD_DOUBLE, DOUBLE_DESIGN, GOOD_DOUBLE]}
    )
    first = orchestrator.execute(orchestrator.plan("spec").id)
    trialled = orchestrator.state.rules()[0]
    shown = [arm("r1", task, 1) for task in ("T-001", "T-002")]
    assert first.status == "done"
    assert (trialled.with_total, trialled.without_total) == (sum(shown), 2 - sum(shown))
    prompts = [prompt for _, key, prompt in caller.calls  # builders, not test designers
               if key.startswith("dsh:") and "ASSIGNED TEST SHARD" not in prompt]
    assert ["EXTRA RULE (on trial)" in prompt for prompt in prompts] == shown

    orchestrator.state.save_rule(replace(trialled, with_passed=6, with_total=6,
                                         without_passed=2, without_total=6))
    second = orchestrator.execute(orchestrator.plan("spec").id)
    head = Workspace.git(orchestrator.repo, "log", "-1", "--format=%B", second.branch)
    assert orchestrator.state.rules()[0].status == "active"
    assert "## LEARNED RULES\n- " + LESSON in caller.calls[-1][2]
    assert parse_trailers(head)["Orch-Rules"] == "r1"
