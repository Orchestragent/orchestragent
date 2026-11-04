"""Plans in any language: requirements, tasks that own disjoint files, and their DAG.

The planner proposes; these helpers read and score the structure. Validation of a plan's
content is language-neutral (orch_generic); verification is by the checks the test
designers declared (orch_verify).
"""

import re
from typing import cast

from orchestragent.orch_graph import closure as closure
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
        "components": {"type": "array",
                       "items": object_schema({"id": TEXT, "dir": TEXT, "test_command": TEXT})},
        "files": {"type": "array", "items": object_schema({"path": TEXT, "content": TEXT})},
        "tasks": {
            "type": "array",
            "items": object_schema(
                {
                    "id": TEXT,
                    "goal": TEXT,
                    "component": TEXT,
                    "reqs": STRINGS,
                    "files": STRINGS,
                    "depends_on": STRINGS,
                    "interface_depends_on": STRINGS,
                    "tests": STRINGS,
                    "contract": TEXT,
                }
            ),
        },
    }
)


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
            component=str(raw.get("component", "") or ""),
        )
        for raw in cast(list[dict[str, object]], plan["tasks"])
    ]


def plan_files(plan: dict[str, object]) -> dict[str, str]:
    return {
        str(item["path"]): str(item["content"])
        for item in cast(list[dict[str, str]], plan["files"])
    }
