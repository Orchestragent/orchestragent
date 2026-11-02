"""Boundary and test-shard validation for projects in any language (or several).

No language is assumed: the planner chooses languages and paths, and each test designer
returns its test files with the command that runs them (orch_verify.Check). Only
language-neutral structure is checked here: safe paths, single ownership, requirement
ownership, acyclic dependencies, and test files that do not overwrite stubs.
"""

from __future__ import annotations

import re
from typing import Any, cast

from orchestragent.live_workspace import safe_relative
from orchestragent.orch_plan import MAX_PLAN_BYTES, MAX_PLAN_FILES
from orchestragent.orch_verify import Check
from orchestragent.orch_verify import parse as parse_check
from orchestragent.serialization import TEXT, object_schema

TASK_ID = re.compile(r"T-[0-9]{3}")
REQ_ID = re.compile(r"R[1-9][0-9]*")
BOUNDARY_KEYS = {"summary", "requirements", "files", "tasks"}
TASK_KEYS = {"id", "goal", "reqs", "files", "depends_on", "contract", "tests"}
# OrchTask.entry() omits interface_depends_on when empty; `cases` are the planner's
# worked examples, read by test design only.
OPTIONAL_TASK_KEYS = {"interface_depends_on", "cases"}
MAX_COMMAND = 2000
GENERIC_TEST_SCHEMA = object_schema({
    "files": {"type": "array", "items": object_schema({"path": TEXT, "content": TEXT})},
    "command": TEXT,
})


safe_path = safe_relative


def boundary_errors(value: object, existing: frozenset[str], *,
                    existing_edits: bool = False) -> list[str]:
    """Structural problems of a boundary plan; [] when it is ready for test design."""
    if not isinstance(value, dict) or set(value) != BOUNDARY_KEYS:
        return [f"Boundary must have exactly the keys {sorted(BOUNDARY_KEYS)}"]
    errors: list[str] = []
    requirements = value["requirements"]
    if not isinstance(requirements, list) or not requirements:
        return ["requirements must be a non-empty list"]
    rids = [r.get("id") if isinstance(r, dict) else None for r in requirements]
    if any(not isinstance(rid, str) or not REQ_ID.fullmatch(rid) for rid in rids):
        errors.append("every requirement needs an id R1, R2, ...")
    if any(not isinstance(r, dict) or not isinstance(r.get("text"), str)
           or not r["text"].strip() for r in requirements):
        errors.append("every requirement needs its text")
    if len(set(rids)) != len(rids):
        errors.append("requirement ids must be unique")
    files = value["files"]
    if not isinstance(files, list):
        return [*errors, "files must be a list"]
    paths: list[str] = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "content"} \
                or not isinstance(item.get("path"), str) \
                or not isinstance(item.get("content"), str):
            errors.append("each file is {path, content}")
            continue
        path = item["path"]
        paths.append(path)
        if not safe_path(path):
            errors.append(f"{path}: not a safe relative path")
        elif path in existing and not existing_edits:
            errors.append(f"{path}: already exists; describe changes in a task contract")
        if not item["content"].strip():
            errors.append(f"{path}: empty stub file")
    if len(set(paths)) != len(paths):
        errors.append("file paths must be unique")
    if len(paths) > MAX_PLAN_FILES or sum(len(str(f)) for f in files) > MAX_PLAN_BYTES:
        errors.append("boundary is too large; use fewer, more cohesive stub files")
    tasks = value["tasks"]
    if not isinstance(tasks, list) or not tasks:
        return [*errors, "tasks must be a non-empty list"]
    owners: dict[str, str] = {}
    writers: dict[str, str] = {}
    ids: list[str] = []
    for task in tasks:
        if (not isinstance(task, dict) or not TASK_KEYS <= set(task)
                or not set(task) <= TASK_KEYS | OPTIONAL_TASK_KEYS):
            errors.append(f"each task has exactly {sorted(TASK_KEYS)}"
                          f" (and optionally {sorted(OPTIONAL_TASK_KEYS)})")
            continue
        for key in ("reqs", "files", "depends_on", "interface_depends_on", "cases"):
            value = task.get(key, [])
            if (not isinstance(value, list) or not all(isinstance(v, str) for v in value)
                    or len(set(value)) != len(value)):
                errors.append(f"{task['id']}: {key} must be a list of unique strings")
        if task["id"] in task["depends_on"]:
            errors.append(f"{task['id']}: a task cannot depend on itself")
        tid = str(task["id"])
        ids.append(tid)
        if not TASK_ID.fullmatch(tid):
            errors.append(f"{tid}: task ids are T-001, T-002, ...")
        if task["tests"] != []:
            errors.append(f"{tid}: task.tests must be [] before test design")
        reqs = task["reqs"]
        if not isinstance(reqs, list) or not reqs:
            errors.append(f"{tid}: every task lists at least one requirement")
            reqs = []
        for rid in reqs:
            if rid in owners:
                errors.append(f"{rid}: accepted by both {owners[rid]} and {tid}")
            owners[str(rid)] = tid
        for path in task["files"] if isinstance(task["files"], list) else []:
            if path in writers:
                errors.append(f"{path}: written by both {writers[path]} and {tid}")
            writers[str(path)] = tid
            if path not in paths and not (existing_edits and path in existing):
                errors.append(f"{tid}: owns {path}, which is neither a stub nor an existing file")
    if len(set(ids)) != len(ids):
        errors.append("task ids must be unique")
    missing = sorted({str(r) for r in rids if isinstance(r, str)} - set(owners),
                     key=lambda r: int(r[1:]))
    if missing:
        errors.append("requirements without an accepting task: " + ", ".join(missing))
    unknown = sorted(set(owners) - {str(r) for r in rids})
    if unknown:
        errors.append("tasks list unknown requirements: " + ", ".join(unknown))
    orphans = sorted(set(paths) - set(writers))
    if orphans:
        errors.append("stub files without a writer task: " + ", ".join(orphans))
    graph = {str(t["id"]): [str(d) for d in t["depends_on"]] for t in tasks
             if isinstance(t, dict) and isinstance(t.get("depends_on"), list)}
    for tid, deps in graph.items():
        for dep in deps:
            if dep not in graph:
                errors.append(f"{tid}: depends on unknown task {dep}")
    if _cyclic(graph):
        errors.append("depends_on must be acyclic")
    for task in tasks:
        if isinstance(task, dict) and isinstance(task.get("interface_depends_on", []), list) \
                and not set(task.get("interface_depends_on", [])) <= set(task.get("depends_on")
                                                                          or []):
            errors.append(f"{task.get('id')}: interface_depends_on must be within depends_on")
    return errors


def _cyclic(graph: dict[str, list[str]]) -> bool:
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        if state.get(node) == 1:
            return True
        if state.get(node) == 2:
            return False
        state[node] = 1
        if any(visit(dep) for dep in graph.get(node, []) if dep in graph):
            return True
        state[node] = 2
        return False

    return any(visit(node) for node in graph)


def test_shard(boundary: dict[str, object], task_id: str, design: object, *,
               existing: frozenset[str] = frozenset()) -> tuple[list[dict[str, str]], Check]:
    """A task's frozen test files and the command that runs exactly them."""
    if not isinstance(design, dict) or set(design) != {"files", "command"}:
        raise ValueError("Return {files, command}: your test files and the shell command, "
                         "run from the repository root, that runs exactly these tests")
    files, command = design["files"], design["command"]
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND:
        raise ValueError("command must be one non-empty shell command line")
    if not isinstance(files, list) or not files:
        raise ValueError("Return at least one test file")
    stubs = {f["path"] for f in cast(list[dict[str, str]], boundary["files"])}
    owned = {p for t in cast(list[dict[str, Any]], boundary["tasks"]) for p in t["files"]}
    result: list[dict[str, str]] = []
    for item in files:
        if (not isinstance(item, dict) or set(item) != {"path", "content"}
                or not isinstance(item["path"], str) or not isinstance(item["content"], str)):
            raise ValueError("each test file is {path, content}")
        path = item["path"]
        if not safe_path(path):
            raise ValueError(f"{path}: not a safe relative path")
        if path in stubs or path in owned or path in existing:
            raise ValueError(f"{path}: test files must be new files, not stubs or existing code")
        if not item["content"].strip() or len(item["content"].encode()) > MAX_PLAN_BYTES:
            raise ValueError(f"{path}: empty or too large")
        result.append({"path": path, "content": item["content"]})
    if len({f["path"] for f in result}) != len(result):
        raise ValueError("test file paths must be unique")
    return result, Check(command.strip(), tuple(sorted(f["path"] for f in result)))


def assemble(boundary: dict[str, object], shards: list[tuple[str, list[dict[str, str]], str]],
             existing: frozenset[str], *, existing_edits: bool = False) -> dict[str, object]:
    """The frozen plan: boundary stubs plus each task's test files and check ids."""
    problems = boundary_errors(boundary, existing, existing_edits=existing_edits)
    if problems:
        raise ValueError("; ".join(problems))
    stubs = cast(list[dict[str, str]], boundary["files"])
    occupied = {p.casefold() for p in existing} | {f["path"].casefold() for f in stubs}
    tasks = {t["id"]: {**t, "tests": []} for t in cast(list[dict[str, Any]], boundary["tasks"])}
    tests: list[dict[str, str]] = []
    for task_id, files, check in shards:
        if task_id not in tasks:
            raise ValueError(f"unknown task {task_id}")
        for file in files:
            if file["path"].casefold() in occupied:
                raise ValueError(f"Frozen test {file['path']} collides with another file")
            occupied.add(file["path"].casefold())
            tests.append(file)
        tasks[task_id]["tests"].append(check)
    return {**boundary, "files": [*stubs, *tests], "tasks": list(tasks.values()),
            "engine": "generic"}


def plan_errors(plan: dict[str, object]) -> list[str]:
    """Structural problems of a frozen plan (stubs, checks, tasks) in any language."""
    tasks = cast(list[dict[str, Any]], plan.get("tasks", []))
    files = cast(list[dict[str, str]], plan.get("files", []))
    checks = [(task["id"], test) for task in tasks if isinstance(task, dict)
              for test in task.get("tests", [])]
    parsed = {test: parse_check(test) for _, test in checks}
    errors = [f"{tid}: {test[:80]} is not a verification check" for tid, test in checks
              if parsed[test] is None]
    test_files = {f for check in parsed.values() if check is not None for f in check.files}
    present = {f["path"] for f in files}
    errors += [f"check file {path} is not in the plan" for path in sorted(test_files - present)]
    boundary = {
        "summary": plan.get("summary", ""), "requirements": plan.get("requirements", []),
        "files": [f for f in files if f["path"] not in test_files],
        "tasks": [{**task, "tests": []} for task in tasks],
    }
    errors += [f"{task['id']}: every task needs at least one verification check"
               for task in tasks if isinstance(task, dict) and not task.get("tests")]
    owned = {p for task in tasks for p in task.get("files", [])}
    errors += [f"{path}: a test file is also owned by a task"
               for path in sorted(test_files & owned)]
    return errors + boundary_errors(boundary, frozenset(owned - present), existing_edits=True)
