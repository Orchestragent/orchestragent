import pytest

from orchestragent.orch_plan import tasks_from
from orchestragent.orch_state import OrchTask
from orchestragent.orch_supervisor import next_task_id, subtree


@pytest.fixture
def dag(math_plan: dict[str, object]) -> list[OrchTask]:
    tasks = tasks_from(math_plan)
    extra = OrchTask("T-007", "x", ["R1"], ["x.py"], ["T-002"], [], "c", status="PLANNED")
    return [*tasks, extra]


def test_subtree_takes_planned_owners_and_all_dependents(dag: list[OrchTask]) -> None:
    assert subtree(dag, "T-001", []) == ["T-001", "T-002", "T-007"]
    assert subtree(dag, "T-002", ["T-001"]) == ["T-002", "T-001", "T-007"]
    dag[0].status = "DONE"
    assert subtree(dag, "T-002", ["T-001"]) == ["T-002", "T-007"]


def test_next_task_id_follows_the_highest_numbered_task(dag: list[OrchTask]) -> None:
    assert next_task_id(dag) == 8
    assert next_task_id([]) == 1
    assert next_task_id([OrchTask("custom", "x", [], [], [], [], "c")]) == 1
