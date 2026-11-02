"""Plans in any language: requirements, tasks that own disjoint files, and their DAG.

The planner proposes; these helpers read and score the structure. Validation of a plan's
content is language-neutral (orch_generic); verification is by the checks the test
designers declared (orch_verify).
"""

import re
from typing import cast

from orchestragent.orch_graph import closure as closure
from orchestragent.orch_index import Coupling
from orchestragent.orch_state import OrchTask
from orchestragent.serialization import TEXT, object_schema

STRINGS = {"type": "array", "items": TEXT}


def _strings(value: object) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return cast(list[str], value)
    return None


REQ_ID = re.compile(r"R[1-9][0-9]{0,2}")


TASK_ID = re.compile(r"T-[0-9]{3}")


# A plan may be assembled from module sub-plans, so it may hold far more files than one
# generated bundle; each task must still fit one builder prompt (checked separately).
MAX_PLAN_FILES = 2000


MAX_PLAN_BYTES = 16_000_000


PLAN_SCHEMA = object_schema(
    {
        "summary": TEXT,
        "requirements": {"type": "array", "items": object_schema({"id": TEXT, "text": TEXT})},
        "files": {"type": "array", "items": object_schema({"path": TEXT, "content": TEXT})},
        "tasks": {
            "type": "array",
            "items": object_schema(
                {
                    "id": TEXT,
                    "goal": TEXT,
                    "reqs": STRINGS,
                    "files": STRINGS,
                    "depends_on": STRINGS,
                    "interface_depends_on": STRINGS,
                    "tests": STRINGS,
                    "contract": TEXT,
                    "cases": STRINGS,
                }
            ),
        },
    }
)


COUPLED = 0.5  # co-change weight above which two files should not be split blindly


def coupling_text(coupling: list[Coupling], limit: int = 20) -> str:
    return "".join(
        f"- {c.a} <-> {c.b}: {c.together} commits together ({c.weight:.2f})\n"
        for c in coupling[:limit] if c.weight >= COUPLED
    )


def coupled_splits(plan: dict[str, object], coupling: list[Coupling]) -> list[str]:
    """Strongly co-changing files owned by tasks that do not depend on each other: the
    pair will likely need one change, which only one of the two tasks may make."""
    tasks = cast(list[dict[str, object]], plan.get("tasks") or [])
    owner = {path: str(t["id"]) for t in tasks for path in cast(list[str], t["files"])}
    graph = {str(t["id"]): list(cast(list[str], t["depends_on"])) for t in tasks}
    found = []
    for pair in coupling:
        first, second = owner.get(pair.a), owner.get(pair.b)
        if pair.weight < COUPLED or not first or not second or first == second:
            continue
        if first not in closure(graph, second) and second not in closure(graph, first):
            found.append(f"{pair.a} ({first}) and {pair.b} ({second})")
    return found


def chain_depths(graph: dict[str, list[str]]) -> dict[str, int]:
    """Remaining downstream chain length, shared by scheduling, scoring and observation."""
    users: dict[str, list[str]] = {tid: [] for tid in graph}
    for tid, deps in graph.items():
        for dep in set(deps):
            if dep not in graph:
                raise ValueError(f"{tid}: unknown dependency {dep}")
            users[dep].append(tid)
    depth = dict.fromkeys(graph, 1)
    pending = {tid: len(children) for tid, children in users.items()}
    leaves = [tid for tid, count in pending.items() if count == 0]
    while leaves:
        tid = leaves.pop()
        for parent in set(graph[tid]):
            depth[parent] = max(depth[parent], depth[tid] + 1)
            pending[parent] -= 1
            if not pending[parent]:
                leaves.append(parent)
    if any(pending.values()):
        raise ValueError("Dependency cycle prevents chain measurement")
    return depth


def plan_score(
    plan: dict[str, object], vacuous: int, splits: int, repairs: int
) -> tuple[float, dict[str, float]]:
    """Deterministic proxy for how well a lint-clean plan will build: parallelism, test
    density per requirement, no vacuous tests, no split coupling, no lint repairs.

    This is the score that picks among plan samples and the proxy planner rules are
    judged on; `orch evolve --check` tests it against real BLOCKED rates.
    """
    tasks = tasks_from(plan)
    graph = {task.id: task.depends_on for task in tasks}
    longest = max(chain_depths(graph).values(), default=1)
    reqs = [str(item["id"]) for item in cast(list[dict[str, str]], plan["requirements"])]
    proving = {rid: sum(len(task.tests) for task in tasks if rid in task.reqs) for rid in reqs}
    parts = {
        "parallelism": min(1.0, len(tasks) / longest / 4),
        "test_density": sum(min(count, 3) / 3 for count in proving.values()) / max(1, len(reqs)),
        "vacuous": vacuous / max(1, len(tasks)),
        "splits": float(splits),
        "repairs": float(repairs),
    }
    score = (
        0.35 * parts["parallelism"] + 0.35 * parts["test_density"]
        + 0.3 * (1 - parts["vacuous"]) - 0.1 * splits - 0.1 * repairs
    )
    return round(max(0.0, min(1.0, score)), 3), parts


def dependency_errors(
    graph: dict[str, list[str]], interfaces: dict[str, object] | None = None,
) -> list[str]:
    """The same DAG checks at planning, recovery and atomic dependency handoff."""
    errors = [f"{tid}: unknown dependency {dep}" for tid, deps in graph.items()
            for dep in deps if dep not in graph or dep == tid] + [
        f"tasks: dependency cycle through {tid}" for tid in _cycle(graph)
    ]
    for tid, raw in (interfaces or {}).items():
        edges = _strings(raw)
        if (edges is None or len(edges) != len(set(edges))
                or not set(edges) <= set(graph.get(tid, []))):
            errors.append(f"{tid}: interface_depends_on must be a unique subset of depends_on")
    return errors


def _cycle(graph: dict[str, list[str]]) -> list[str]:
    """Nodes left after Kahn's algorithm; empty for a DAG."""
    known = {tid: [d for d in deps if d in graph and d != tid] for tid, deps in graph.items()}
    pending = {tid: len(deps) for tid, deps in known.items()}
    users: dict[str, list[str]] = {tid: [] for tid in known}
    for tid, deps in known.items():
        for dep in deps:
            users[dep].append(tid)
    ready = [tid for tid, count in pending.items() if count == 0]
    while ready:
        for user in users[ready.pop()]:
            pending[user] -= 1
            if pending[user] == 0:
                ready.append(user)
    return sorted(tid for tid, count in pending.items() if count)


def tasks_from(plan: dict[str, object]) -> list[OrchTask]:
    """Only call on a plan that passed lint."""
    return [
        OrchTask(
            id=str(raw["id"]),
            goal=str(raw["goal"]),
            reqs=list(cast(list[str], raw["reqs"])),
            files=list(cast(list[str], raw["files"])),
            depends_on=list(cast(list[str], raw["depends_on"])),
            tests=list(cast(list[str], raw["tests"])),
            contract=str(raw["contract"]),
            interface_depends_on=list(cast(list[str], raw.get("interface_depends_on", []))),
        )
        for raw in cast(list[dict[str, object]], plan["tasks"])
    ]


def plan_files(plan: dict[str, object]) -> dict[str, str]:
    return {
        str(item["path"]): str(item["content"])
        for item in cast(list[dict[str, str]], plan["files"])
    }
