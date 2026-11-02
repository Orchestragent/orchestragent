import pytest

from orchestragent.native import ContextPrompt
from orchestragent.orch_build import SPEC_AUTHORITY
from orchestragent.orch_graph import replacement_frontier
from orchestragent.orch_ladder import (
    PARK,
    next_level,
    next_task_id,
    replan_prompt,
    review_prompt,
    samples,
    subtree,
    thicken_prompt,
)
from orchestragent.orch_mutate import harden_prompt
from orchestragent.orch_plan import plan_prompt, tasks_from
from orchestragent.orch_state import OrchTask


@pytest.mark.parametrize("shape,valid", [("helper", True), ("report", False),
                                        ("renamed_output", False), ("two_outputs", True)])
def test_replacement_graph_preserves_the_original_output_frontier(shape: str, valid: bool) -> None:
    original = {"api.py"}
    graph = {"implementation": ["helper"], "helper": []}
    owned = {"implementation": ["api.py"], "helper": ["private.py"]}
    if shape == "report":
        graph = {"implementation": [], "helper": ["implementation"]}
        owned["helper"] = ["report.txt"]
    elif shape == "renamed_output":
        owned["implementation"] = ["new_api.py"]
    elif shape == "two_outputs":
        original.add("other.py")
        graph["second"] = []
        owned["second"] = ["other.py"]
    assert (not replacement_frontier(original, graph, owned)) is valid


@pytest.mark.parametrize(
    ("used", "repeated", "pass_rate", "expected"),
    [
        ([0], False, 0.5, 1),
        ([0, 1], False, 0.5, 2),
        ([0, 1], True, 0.5, 3),
        ([0, 1], False, 0.2, 4),
        ([0, 1, 4], False, 0.2, 3),
        ([0, 1, 2], False, 0.5, 3),
        ([0, 1, 3], True, 0.5, 4),
        ([0, 1, 2, 3, 4], False, 0.5, 5),
        ([0, 1, 2, 3, 4, 5], False, 0.5, PARK),
        ([0, 1, 3, 4, 5], True, 0.1, PARK),
    ],
)
def test_next_level_climbs_each_rung_once(
    used: list[int], repeated: bool, pass_rate: float, expected: int
) -> None:
    assert next_level(used, repeated, pass_rate) == expected


@pytest.mark.parametrize(
    ("pass_rate", "expected"),
    [(0.8, 2), (0.5, 4), (0.3, 4), (0.9, 2), (1.0, 2), (0.0, 4), (0.6, 3)],
)
def test_sample_count_follows_the_formula_within_bounds(pass_rate: float, expected: int) -> None:
    assert samples(pass_rate) == expected


@pytest.fixture
def dag(math_plan: dict[str, object]) -> list[OrchTask]:
    tasks = tasks_from(math_plan)
    extra = OrchTask("T-007", "x", ["R1"], ["x.py"], ["T-002"], [], "c", status="PLANNED")
    return [*tasks, extra]


@pytest.mark.parametrize("stage", ["plan", "replan", "harden"])
def test_dynamic_feedback_preserves_context_boundary(
    math_plan: dict[str, object], dag: list[OrchTask], stage: str,
) -> None:
    spec = "Original constraints must survive later planning and test strengthening."
    if stage == "plan":
        first = plan_prompt(spec, "index", [], hint="first")
        second = plan_prompt(spec, "index", ["ERR_SENTINEL_42"], hint="second")
    elif stage == "replan":
        first = replan_prompt(math_plan, dag, [dag[0].id], 8, dag[0], "first", original_spec=spec)
        second = replan_prompt(math_plan, dag, [dag[1].id], 9, dag[1], "ERR_SENTINEL_42",
                               original_spec=spec)
    else:
        first = harden_prompt(dag[0], {"R1": "add"}, {}, {}, [], original_spec=spec)
        second = harden_prompt(dag[0], {"R1": "add"}, {}, {}, [{"reason": "ERR_SENTINEL_42"}],
                               original_spec=spec)
    assert isinstance(first, ContextPrompt) and isinstance(second, ContextPrompt)
    assert first.shared == second.shared
    assert spec in first.shared and SPEC_AUTHORITY in first.shared
    assert "ERR_SENTINEL_42" in second.dynamic and "ERR_SENTINEL_42" not in second.shared


def test_subtree_takes_planned_owners_and_all_dependents(dag: list[OrchTask]) -> None:
    assert subtree(dag, "T-001", []) == ["T-001", "T-002", "T-007"]
    assert subtree(dag, "T-002", ["T-001"]) == ["T-002", "T-001", "T-007"]
    dag[0].status = "DONE"
    assert subtree(dag, "T-002", ["T-001"]) == ["T-002", "T-007"]
    assert next_task_id(dag) == 8


@pytest.mark.parametrize("stage", ["review", "thicken"])
def test_exception_context_shares_original_spec_without_other_task_evidence(
    dag: list[OrchTask], stage: str,
) -> None:
    spec = "ORIGINAL_REQUIREMENT_42"
    if stage == "review":
        first = review_prompt(dag[0], {}, {"a.py": "TEST_A"}, "first", original_spec=spec)
        second = review_prompt(dag[1], {}, {"a.py": "TEST_A"}, "second", original_spec=spec)
    else:
        first = thicken_prompt(dag[0], {}, {}, {"a.py": "SOURCE_A"}, original_spec=spec)
        second = thicken_prompt(dag[1], {}, {}, {"b.py": "SOURCE_B"}, original_spec=spec)
    assert isinstance(first, ContextPrompt) and isinstance(second, ContextPrompt)
    assert first.shared == second.shared and spec in first.shared
    assert dag[0].id in first.dynamic and dag[0].id not in second.shared
    assert first.dynamic != second.dynamic


def test_review_cache_boundary_tracks_frozen_files_and_keeps_disputes_dynamic(
    dag: list[OrchTask],
) -> None:
    first = review_prompt(dag[0], {}, {"a.py": "TEST_A"}, "first", original_spec="spec")
    other = review_prompt(dag[1], {}, {"b.py": "TEST_B"}, "second", original_spec="spec")
    assert isinstance(first, ContextPrompt) and isinstance(other, ContextPrompt)
    assert first.shared != other.shared and "TEST_A" in first.shared
    assert "TEST_A" not in first.dynamic and "second" not in other.shared


def test_exception_operations_share_context_but_keep_operation_in_user_message(
    dag: list[OrchTask],
) -> None:
    review = review_prompt(dag[0], {}, {}, "disputed", original_spec="spec")
    thicken = thicken_prompt(dag[0], {}, {}, {}, original_spec="spec")
    assert isinstance(review, ContextPrompt) and isinstance(thicken, ContextPrompt)
    assert review.shared == thicken.shared
    assert "contract=''" in review.dynamic and "verdict=contract" in thicken.dynamic
