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
# A component is a directory with its own build, language and test runner. Its tasks' files
# and tests live under it; its `test_command` (run from the repository root; "" when the
# project has no usable suite) is the final gate's view of the component as a whole.
OPTIONAL_BOUNDARY_KEYS = {"components"}
COMPONENT_KEYS = {"id", "dir", "test_command"}
TASK_KEYS = {"id", "goal", "reqs", "files", "depends_on", "contract", "tests"}
# OrchTask.entry() omits interface_depends_on when empty; `component` is implied when the
# boundary declares none (one implicit component at the repository root).
OPTIONAL_TASK_KEYS = {"interface_depends_on", "component"}
MAX_COMMAND = 2000
FILES = {"type": "array", "items": object_schema({"path": TEXT, "content": TEXT})}
# One test designer per component answers with a shard per task of that component.
COMPONENT_TEST_SCHEMA = object_schema({
    "shards": {"type": "array", "items": object_schema({"task": TEXT, "files": FILES,
                                                        "command": TEXT})},
})
# The planner's integration checks: each proves tasks agree where they meet.
INTEGRATION_SCHEMA = object_schema({
    "checks": {"type": "array",
               "items": object_schema({"files": FILES, "command": TEXT,
                                       "tasks": {"type": "array", "items": TEXT}})},
})
GENERIC_TEST_SCHEMA = object_schema({"files": FILES, "command": TEXT})



def shape_error(what: str, item: object, keys: set[str]) -> str:
    """'each <what> is {keys}', plus what this one lacked or added: a designer told only
    the shape resent a shard without its command (Meridian, after-step7c)."""
    wanted = "{" + ", ".join(sorted(keys)) + "}"
    if not isinstance(item, dict):
        return f"each {what} is an object {wanted}, not {type(item).__name__}"
    missing, extra = sorted(keys - set(item)), sorted(set(item) - keys)
    detail = "; ".join(part for part in (
        f"missing {', '.join(missing)}" if missing else "",
        f"unexpected {', '.join(map(str, extra))}" if extra else "") if part)
    return f"each {what} is {wanted}" + (f" ({detail})" if detail else
                                         " (path and content are strings)")

def components(boundary: dict[str, object]) -> list[dict[str, str]]:
    """The boundary's components; a boundary without them is one component at the root."""
    declared = boundary.get("components")
    if isinstance(declared, list) and declared:
        return [{"id": str(c.get("id", "")), "dir": _norm_dir(str(c.get("dir", "."))),
                 "test_command": str(c.get("test_command", ""))}
                for c in declared if isinstance(c, dict)]
    return [{"id": "", "dir": ".", "test_command": ""}]


def _norm_dir(path: str) -> str:
    path = path.strip().strip("/")
    return "." if path in {"", "."} else path


def under(path: str, directory: str) -> bool:
    return directory == "." or path == directory or path.startswith(directory + "/")


def component_of(boundary: dict[str, object], task: dict[str, Any]) -> dict[str, str]:
    wanted = str(task.get("component", ""))
    found = components(boundary)
    return next((c for c in found if c["id"] == wanted), found[0])


def component_errors(value: dict[str, object]) -> list[str]:
    declared = value.get("components")
    if declared is None:
        return []
    if not isinstance(declared, list) or not declared:
        return ["components must be a non-empty list"]
    errors: list[str] = []
    dirs: dict[str, str] = {}
    for item in declared:
        if (not isinstance(item, dict) or set(item) != COMPONENT_KEYS
                or not all(isinstance(item[k], str) for k in COMPONENT_KEYS)):
            errors.append("each component is {id, dir, test_command}")
            continue
        if not item["id"].strip():
            errors.append("every component needs an id")
        directory = _norm_dir(item["dir"])
        if directory != "." and not safe_path(directory):
            errors.append(f"component {item['id']}: {item['dir']} is not a safe relative path")
        for other, other_dir in dirs.items():
            if under(directory, other_dir) or under(other_dir, directory):
                errors.append(f"components {other} and {item['id']} overlap: "
                              f"{other_dir} and {directory}")
        dirs[item["id"]] = directory
        if len(item["test_command"]) > MAX_COMMAND:
            errors.append(f"component {item['id']}: test_command too long")
    ids = [str(c.get("id")) for c in declared if isinstance(c, dict)]
    if len(set(ids)) != len(ids):
        errors.append("component ids must be unique")
    return errors


safe_path = safe_relative


def boundary_errors(value: object, existing: frozenset[str], *,
                    existing_edits: bool = False) -> list[str]:
    """Structural problems of a boundary plan; [] when it is ready for test design."""
    if (not isinstance(value, dict) or not BOUNDARY_KEYS <= set(value)
            or not set(value) <= BOUNDARY_KEYS | OPTIONAL_BOUNDARY_KEYS):
        return [f"Boundary must have exactly the keys {sorted(BOUNDARY_KEYS)}"
                f" (and optionally {sorted(OPTIONAL_BOUNDARY_KEYS)})"]
    boundary = value  # the task loop below reuses the name `value` for field values
    errors: list[str] = component_errors(boundary)
    known = {c["id"] for c in components(boundary)}
    declared_components = isinstance(boundary.get("components"), list)
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
            errors.append(shape_error("file", item, {"path", "content"}))
            continue
        path = item["path"]
        paths.append(path)
        if not safe_path(path):
            errors.append(f"{path}: not a safe relative path")
        elif path in existing and not existing_edits:
            errors.append(f"{path}: already exists; describe changes in a task contract")
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
        for key in ("reqs", "files", "depends_on", "interface_depends_on"):
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
        if declared_components and not errors:
            if str(task.get("component", "")) not in known:
                errors.append(f"{tid}: component {task.get('component')!r} is not declared")
            else:
                home = component_of(boundary, task)["dir"]
                for path in task["files"] if isinstance(task["files"], list) else []:
                    if not under(str(path), home):
                        errors.append(f"{tid}: {path} lies outside its component's directory "
                                      f"{home}")
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
    owner = {p: str(t["id"]) for t in cast(list[dict[str, Any]], boundary["tasks"])
             for p in t["files"]}
    result: list[dict[str, str]] = []
    for item in files:
        if (not isinstance(item, dict) or set(item) != {"path", "content"}
                or not isinstance(item["path"], str) or not isinstance(item["content"], str)):
            raise ValueError(shape_error("test file", item, {"path", "content"}))
        path = item["path"]
        if not safe_path(path):
            raise ValueError(f"{path}: not a safe relative path")
        # Say whose file it is and what to do instead: a designer told only "must be new"
        # resent the same path three times (Meridian, integration/harness.py).
        if path in owner or path in stubs:
            raise ValueError(
                f"{path} is an implementation file of the plan"
                + (f", written by {owner[path]}" if path in owner else "")
                + ". Tests may import or run it, never replace it: put this test code "
                "(helpers and fixtures too) in a new file at another path")
        if path in existing:
            raise ValueError(
                f"{path} already exists in the repository. Tests may import or run it, "
                "never replace it: put this test code in a new file at another path")
        if not item["content"].strip() or len(item["content"].encode()) > MAX_PLAN_BYTES:
            raise ValueError(f"{path}: empty or too large")
        result.append({"path": path, "content": item["content"]})
    if len({f["path"] for f in result}) != len(result):
        raise ValueError("test file paths must be unique")
    return result, Check(command.strip(), tuple(sorted(f["path"] for f in result)))


def component_shards(boundary: dict[str, object], component: dict[str, str], design: object, *,
                     existing: frozenset[str] = frozenset(), only: list[str] | None = None,
                     ) -> list[tuple[str, list[dict[str, str]], Check]]:
    """One designer's answer for a component: exactly one shard per task of the component
    (or per task in `only`, when the rest were already accepted), every test file under the
    component's directory."""
    tasks = [t for t in cast(list[dict[str, Any]], boundary["tasks"])
             if component_of(boundary, t)["id"] == component["id"]]
    allowed = [t["id"] for t in tasks]  # a repair may resend an accepted shard: it is re-checked
    wanted = [t["id"] for t in tasks if only is None or t["id"] in only]
    if not isinstance(design, dict) or set(design) != {"shards"} or not isinstance(
            design["shards"], list):
        raise ValueError("Return {shards: [{task, files, command}]}: one shard per task of "
                         f"this component, for tasks {wanted}")
    seen: list[str] = []
    out: list[tuple[str, list[dict[str, str]], Check]] = []
    taken: set[str] = set()
    for item in design["shards"]:
        if not isinstance(item, dict) or set(item) != {"task", "files", "command"}:
            raise ValueError(shape_error("shard", item, {"task", "files", "command"}))
        tid = str(item["task"])
        if tid not in allowed:
            raise ValueError(f"shard for {tid}: not a task of this component ({allowed})")
        if tid in seen:
            raise ValueError(f"two shards for {tid}; return exactly one per task")
        seen.append(tid)
        files, check = test_shard(boundary, tid, {"files": item["files"],
                                                 "command": item["command"]}, existing=existing)
        for file in files:
            if not under(file["path"], component["dir"]):
                raise ValueError(f"{file['path']}: test files of component {component['id']} "
                                 f"live under {component['dir']}/")
            if file["path"].casefold() in taken:
                raise ValueError(f"{file['path']}: used by two shards")
            taken.add(file["path"].casefold())
        out.append((tid, files, check))
    missing = [tid for tid in wanted if tid not in seen]
    if missing:
        raise ValueError(f"no shard for {missing}; return one per task of this component")
    return out


def integration_checks(boundary: dict[str, object], design: object, *,
                       existing: frozenset[str] = frozenset()
                       ) -> list[tuple[list[str], list[dict[str, str]], Check]]:
    """The planner's integration checks: (covered task ids, files, check) each."""
    if not isinstance(design, dict) or set(design) != {"checks"} or not isinstance(
            design["checks"], list):
        raise ValueError("Return {checks: [{files, command, tasks}]}")
    ids = {t["id"] for t in cast(list[dict[str, Any]], boundary["tasks"])}
    out: list[tuple[list[str], list[dict[str, str]], Check]] = []
    taken: set[str] = set()
    for item in design["checks"]:
        if not isinstance(item, dict) or set(item) != {"files", "command", "tasks"}:
            raise ValueError(shape_error("check", item, {"files", "command", "tasks"}))
        covered = item["tasks"]
        if (not isinstance(covered, list) or len(covered) < 2
                or not all(isinstance(t, str) and t in ids for t in covered)
                or len(set(covered)) != len(covered)):
            raise ValueError("each integration check names two or more existing tasks")
        files, check = test_shard(boundary, str(covered[0]), {"files": item["files"],
                                                             "command": item["command"]},
                                  existing=existing)
        for file in files:
            if file["path"].casefold() in taken:
                raise ValueError(f"{file['path']}: used by two checks")
            taken.add(file["path"].casefold())
        out.append((list(covered), files, check))
    return out


def joins(boundary: dict[str, object]) -> list[str]:
    """Tasks where independent work meets: depending on two or more tasks, or in a
    different component than one of their dependencies."""
    tasks = cast(list[dict[str, Any]], boundary["tasks"])
    by_id = {t["id"]: t for t in tasks}
    found = []
    for task in tasks:
        deps = [by_id[d] for d in task["depends_on"] if d in by_id]
        if len(deps) >= 2 or any(component_of(boundary, d)["id"]
                                 != component_of(boundary, task)["id"] for d in deps):
            found.append(task["id"])
    return found


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
