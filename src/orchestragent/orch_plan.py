"""Verifiable planning: Claude proposes, deterministic lint decides before any code is written.

A plan is a compilable skeleton (interface stubs), frozen unittest files whose test
names carry their requirement (`test_r3_...` is the stdlib form of `@req("R3")`),
and a DAG of tasks that own disjoint files.
"""

import ast
import re
import sys
from pathlib import PurePosixPath
from typing import cast

from orchestragent.live import TEXT, object_schema
from orchestragent.live_workspace import LiveWorkspace
from orchestragent.native import ContextPrompt
from orchestragent.orch_build import SPEC_AUTHORITY, spec_context
from orchestragent.orch_graph import closure as closure
from orchestragent.orch_index import Coupling, RepoIndex, python_module
from orchestragent.orch_state import OrchTask
from orchestragent.orch_testlint import fixture_errors

REQ_ID = re.compile(r"R[1-9][0-9]{0,2}")
TASK_ID = re.compile(r"T-[0-9]{3}")
REQ_TEST = re.compile(r"test_r([1-9][0-9]{0,2})_[A-Za-z0-9_]+")
STRINGS = {"type": "array", "items": TEXT}
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
                }
            ),
        },
    }
)
PLAN_INSTRUCTIONS = SPEC_AUTHORITY + (
    "Plan a Python standard-library build; return JSON only, matching the schema. "
    "Builders see their task, not your reasoning.\n"
    "Use root packages, absolute imports and unittest (no src/ layout). Tests run as "
    "`python -m unittest <ids>` with only the repository root on sys.path.\n"
    "- requirements: R1, R2, ...; each describes a distinct, testable behavior. Group "
    "its normal, boundary and error variants without losing any requested behavior "
    "or constraint. Do not create a requirement for every example.\n"
    "- files: new interface stubs with final signatures, type hints, short purpose "
    "or invariant docstrings, and bodies `raise NotImplementedError`; frozen "
    "test_*.py unittest.TestCase files. Avoid restating specifications and examples "
    "in docstrings.\n"
    "- tasks: T-001, T-002, ...; DISJOINT non-test files editable only by their owner. "
    "Prefer cohesive module-and-helper ownership; split independent work or oversized "
    "modules, not each function or example. List reqs, depends_on for every imported "
    "task, tests as dotted ids (tests/test_x.py -> tests.test_x.Class.method), and a "
    "concise contract for interface and cross-module choices. Original requirements remain "
    "normative; refer to their IDs rather than rewriting their validation rules or formulas. "
    "Record only algorithm choices other tasks must rely on, leaving internal implementation "
    "to builders. Fix inputs/outputs, exceptions, mutation and ordering used by other tasks. "
    "interface_depends_on is a subset of depends_on: list a dependency there ONLY when "
    "its frozen interface and behavioral contract suffice to implement this task before "
    "that dependency is built. Use [] when implementation details or generated artifacts "
    "are needed, or when uncertain. Integration still waits for every depends_on edge. Refer "
    "to R ids instead of copying requirement prose or test tables. Keep the DAG shallow.\n"
    "- tests: every requirement RN needs one or more test_r<N>_<what> methods. Use "
    "table-driven cases with self.subTest and shared setUp/helpers to avoid repeated "
    "setup and assertions. Preserve explicit expected results or exceptions for "
    "normal, edge and failure cases; tests must fail against the stubs. Never drop "
    "cases, weaken assertions or special-case test inputs to save space. Split large "
    "test files by cohesive behavior. Tests use no network, files, subprocesses or "
    "unlisted third-party packages.\n"
    "Every requirement has a task and frozen test; every test belongs to exactly one "
    "task whose reqs include its R id. Never assign a test file to a task.\n"
)


def importable(path: str) -> bool:
    """unittest loads tests by dotted name: every path part must be an ASCII identifier."""
    return all(part.isascii() and part.isidentifier() for part in python_module(path).split("."))


def shadowed(path: str) -> str:
    """The stdlib module a new top-level file or package would hide, if any."""
    top = PurePosixPath(path).parts[0].removesuffix(".py")
    return top if top in sys.stdlib_module_names else ""


TESTCASE_ROOTS = frozenset({"TestCase", "IsolatedAsyncioTestCase"})


def _test_classes(source: str) -> tuple[dict[str, list[str]], list[str]]:
    """Test names visible on each runnable top-level class, and unsupported shapes.

    Runnable: a base is an imported unittest TestCase, or an earlier class of this file
    that is runnable. Test methods defined with `def` on earlier classes of this file
    (mixins) are inherited. A class with a base from elsewhere is kept, as before, but
    reported: its inherited tests cannot be listed without running code.
    """
    tree = ast.parse(source)
    modules: set[str] = set()
    roots: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules |= {alias.asname or alias.name for alias in node.names
                        if alias.name == "unittest"}
        elif isinstance(node, ast.ImportFrom) and node.module == "unittest":
            roots |= {alias.asname or alias.name for alias in node.names
                      if alias.name in TESTCASE_ROOTS}
    visible: dict[str, list[str]] = {}
    kinds: dict[str, str] = {}  # case | plain | unknown
    ancestors: dict[str, set[str]] = {}
    problems: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        names = [item.name for item in node.body
                 if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and item.name.startswith("test")]
        problems += [
            f"class {node.name}: {target.id} is assigned, not defined with def"
            for item in node.body if isinstance(item, (ast.Assign, ast.AnnAssign))
            for target in (item.targets if isinstance(item, ast.Assign) else [item.target])
            if isinstance(target, ast.Name) and target.id.startswith("test")
        ]
        kind, lineage = "plain", set()
        for base in node.bases:
            if (isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
                    and base.value.id in modules and base.attr in TESTCASE_ROOTS
                    or isinstance(base, ast.Name) and base.id in roots):
                kind = "case"
            elif isinstance(base, ast.Name) and base.id in kinds:
                names += [name for name in visible[base.id] if name not in names]
                lineage |= {base.id, *ancestors[base.id]}
                if kinds[base.id] != "plain" and kind != "case":
                    kind = kinds[base.id]
            elif not (isinstance(base, ast.Name) and base.id == "object"):
                if not names and not _testlike(base):
                    # A helper such as BoomError(Exception) or a counting subclass of a
                    # project class defines no tests and inherits none: it is plain. Runs
                    # at 6d701aa/4b12bef lost revisions (once the whole plan) to this.
                    continue
                problems.append(f"class {node.name}: base {ast.unparse(base)} is not "
                                "unittest.TestCase or a class defined earlier in this file")
                kind = "case" if kind == "case" else "unknown"
        visible[node.name], kinds[node.name], ancestors[node.name] = names, kind, lineage
    inherited = {name for cls, kind in kinds.items() if kind != "plain"
                 for name in ancestors[cls]}
    problems += [f"class {cls} defines tests but is not a unittest.TestCase, and no TestCase "
                 "in this file inherits it, so they never run"
                 for cls, kind in kinds.items()
                 if kind == "plain" and visible[cls] and cls not in inherited]
    return {cls: visible[cls] for cls, kind in kinds.items() if kind != "plain"}, problems


def _testlike(base: ast.expr) -> bool:
    """A base from elsewhere that may carry test methods: its name says Test or Case."""
    name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
    return "test" in name.lower() or name.endswith("Case")


def unittest_ids(path: str, source: str) -> list[str]:
    """Runnable dotted ids of a unittest module's test methods, in file order.

    Mixin methods are listed on each TestCase that inherits them, never on the mixin.
    """
    module = python_module(path)
    return [f"{module}.{cls}.{name}"
            for cls, names in _test_classes(source)[0].items() for name in names]


def unittest_shape_errors(source: str) -> list[str]:
    """Test-class shapes whose runnable ids cannot be listed statically; fix before freezing."""
    return _test_classes(source)[1]


# Variation for parallel plan samples; scoring, not discussion, picks the winner.
PLAN_HINTS = (
    "",
    "Prefer many small tasks with shallow dependencies.",
    "Prefer cohesive tasks that each own one module and its helpers.",
    "Minimize the critical path: maximize tasks that can be built in parallel.",
)


def plan_prompt(
    spec: str, repo: str, errors: list[str], rules: str = "", trial: str = "",
    coupling: str = "", hint: str = "",
) -> str:
    """Instructions, learned planning rules and the repository first (stable across
    plans of one repo), then the spec and this attempt's corrections."""
    parts = [PLAN_INSTRUCTIONS]
    parts += [f"\nLEARNED PLANNING RULES:\n{rules}"] if rules else []
    parts += ["\nREPOSITORY MAP (existing code):\n", repo or "(empty)\n"]
    parts += [f"\nFILES THAT CHANGE TOGETHER (git history; keep each pair in one task "
              f"or make one task depend on the other):\n{coupling}"] if coupling else []
    parts += [spec_context(spec)]
    dynamic = ["\nReturn the plan for this specification.\n"]
    dynamic += [f"\nEXTRA PLANNING RULE (on trial):\n- {trial}\n"] if trial else []
    dynamic += [f"\nAPPROACH:\n{hint}\n"] if hint else []
    if errors:
        dynamic += ["\nYOUR PREVIOUS PLAN FAILED LINT; fix every item:\n"]
        dynamic += [f"- {error}\n" for error in errors]
    return ContextPrompt("".join(parts), "".join(dynamic))


# Deterministic planner lessons for lint rejections: a planning mistake the linter
# names is a rule the planner can learn without any model deciding what it meant.
LINT_LESSONS = (
    ("already owned by", "Give every non-test file exactly one owning task; put shared "
     "helpers in their own module owned by a task the others depend on."),
    ("lacks a test_r<N>_ requirement marker", "Name every test method test_r<N>_<what> "
     "after the requirement it proves."),
    ("no test proves it", "Write at least one frozen test for every requirement, including "
     "its failure cases."),
    ("no task implements it", "Assign every requirement to at least one task."),
    ("dependency cycle", "Keep the task graph acyclic: shared code goes into a lower task "
     "that both sides depend on."),
    ("unknown dependency", "Depend only on task ids defined in the same plan."),
    ("may not own", "Never assign test files (test_*.py) or unsupported paths to a task."),
    ("does not prove one of its reqs", "Give each task only tests that prove its own "
     "requirements."),
    ("has no owning task", "Every stub that raises NotImplementedError needs an owning task."),
    ("belongs to no task", "Every frozen test method belongs to exactly one task."),
    ("would shadow the standard library", "Never name a module or package after a "
     "standard-library module."),
    ("the plan may only add files", "Only add new files; change existing modules through "
     "tasks that own them, never by rewriting them in the plan."),
    ("is not importable", "Put tests at importable paths: identifiers only, no dashes."),
    ("to depends_on", "When a stub imports another task's module, list that task in depends_on."),
    ("-byte builder prompt", "Keep each task's files and frozen tests small enough for one "
     "builder prompt; split large test files by module."),
    ("does not parse", "Every generated file must be valid Python."),
    ("not in a frozen test file", "List each task's tests as dotted ids that exist in the "
     "frozen test files (path.Class.method from the repository root)."),
)


def lint_lessons(errors: list[str]) -> list[str]:
    """Planner lessons for the lint errors seen, in LINT_LESSONS order, deduplicated."""
    return [text for marker, text in LINT_LESSONS if any(marker in e for e in errors)]


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
    proving = {
        rid: sum(1 for task in tasks for test in task.tests
                 if (m := REQ_TEST.fullmatch(test.rsplit(".", 1)[1])) and f"R{m.group(1)}" == rid)
        for rid in reqs
    }
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


def _strings(value: object) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return cast(list[str], value)
    return None


def lint(
    plan: object, existing: frozenset[str] = frozenset(), *, fixture_paths: set[str] | None = None,
) -> list[str]:
    """Every reason this plan cannot be built safely; empty means accepted.

    `existing` lists paths already in the repository: the plan may only add files.
    Fixture checks apply to all generated tests, or just explicitly authored paths
    when validating a revision of an older frozen plan.
    """
    if not isinstance(plan, dict):
        return ["plan is not an object"]
    errors: list[str] = []
    requirements = plan.get("requirements")
    reqs: dict[str, str] = {}
    if not isinstance(requirements, list) or not requirements:
        errors.append("requirements: need at least one")
    else:
        for item in requirements:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                errors.append("requirements: invalid entry")
                continue
            rid = str(item["id"])
            if not REQ_ID.fullmatch(rid) or rid in reqs:
                errors.append(f"requirements: bad or duplicate id {rid!r}")
            reqs[rid] = str(item.get("text", ""))
    try:
        files = LiveWorkspace.validate_bundle(plan.get("files"), MAX_PLAN_FILES, MAX_PLAN_BYTES)
    except ValueError as error:
        return [*errors, f"files: {error}"]
    sources = {item["path"]: item["content"] for item in files}
    owned_paths = [
        path
        for raw in plan.get("tasks") or []
        if isinstance(raw, dict)
        for path in _strings(raw.get("files")) or []
    ]
    errors += [
        f"files: {path} would shadow the standard library module {shadowed(path)!r}"
        for path in sorted({*sources, *owned_paths} - existing)
        if shadowed(path)
    ]
    errors += [
        f"files: {path} already exists; the plan may only add files"
        for path in sorted(set(sources) & existing)
    ]
    errors += [
        f"files: {path} is below existing file {parent}; the plan may only add files"
        for path in sorted(sources)
        for parent in PurePosixPath(path).parents if str(parent) in existing
    ]
    tests: dict[str, str] = {}
    stubs: set[str] = set()
    for path, source in sources.items():
        if not path.endswith(".py"):
            continue
        try:
            ast.parse(source)
            if (LiveWorkspace.is_test_file(path)
                    and (fixture_paths is None or path in fixture_paths)):
                errors += [f"tests: {path}: {problem}" for problem in fixture_errors(source)]
        except SyntaxError as error:
            errors.append(f"files: {path} does not parse (line {error.lineno})")
            continue
        if LiveWorkspace.is_test_file(path):
            if not importable(path):
                errors.append(f"tests: {path} is not importable as a dotted unittest module")
                continue
            if fixture_paths is None or path in fixture_paths:
                errors += [f"tests: {path}: {problem}"
                           for problem in unittest_shape_errors(source)]
            for test in unittest_ids(path, source):
                tests[test] = path
                if not REQ_TEST.fullmatch(test.rsplit(".", 1)[1]):
                    errors.append(f"tests: {test} lacks a test_r<N>_ requirement marker")
        elif "NotImplementedError" in source:
            # Only real stubs need an owner; package markers and constants may stand alone.
            stubs.add(path)
    raw_tasks = plan.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return [*errors, "tasks: need at least one"]
    owner: dict[str, str] = {}
    claimed: dict[str, str] = {}
    graph: dict[str, list[str]] = {}
    interface_graph: dict[str, object] = {}
    for raw in raw_tasks:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            errors.append("tasks: invalid entry")
            continue
        tid = str(raw["id"])
        if not TASK_ID.fullmatch(tid) or tid in graph:
            errors.append(f"tasks: bad or duplicate id {tid!r}")
        lists = {key: _strings(raw.get(key)) for key in ("reqs", "files", "depends_on", "tests")}
        if any(value is None for value in lists.values()) or not all(
            isinstance(raw.get(key), str) and str(raw[key]).strip() for key in ("goal", "contract")
        ):
            errors.append(f"{tid}: goal, contract, reqs, files, depends_on, tests are required")
            graph[tid] = []
            continue
        task_reqs, task_files, deps, task_tests = (
            cast(list[str], lists[key]) for key in ("reqs", "files", "depends_on", "tests")
        )
        graph[tid] = deps
        interface_graph[tid] = raw.get("interface_depends_on", [])
        missing = [name for name, value in (("reqs", task_reqs), ("files", task_files),
                                            ("tests", task_tests)) if not value]
        if missing:
            errors.append(f"{tid}: needs reqs, files and tests (missing {', '.join(missing)})")
        errors += [f"{tid}: unknown requirement {r}" for r in task_reqs if r not in reqs]
        for path in task_files:
            if not LiveWorkspace.safe_path(path) or LiveWorkspace.is_test_file(path):
                errors.append(f"{tid}: may not own {path!r}")
            elif path in owner:
                errors.append(f"{tid}: {path} already owned by {owner[path]}")
            else:
                owner[path] = tid
        for test in task_tests:
            match = REQ_TEST.fullmatch(test.rsplit(".", 1)[-1])
            if test not in tests:
                errors.append(f"{tid}: test {test} is not in a frozen test file")
            elif test in claimed:
                errors.append(f"{tid}: test {test} already belongs to {claimed[test]}")
            elif match is None or f"R{match.group(1)}" not in task_reqs:
                errors.append(f"{tid}: test {test} does not prove one of its reqs")
            else:
                claimed[test] = tid
    errors += dependency_errors(graph, interface_graph)
    errors += [f"files: stub {path} has no owning task" for path in sorted(stubs - set(owner))]
    errors += _static_dependencies(sources, owner, graph)
    errors += [f"tests: {test} belongs to no task" for test in sorted(set(tests) - set(claimed))]
    proven = {
        f"R{match.group(1)}"
        for test in claimed
        if (match := REQ_TEST.fullmatch(test.rsplit(".", 1)[1]))
    }
    errors += [f"{rid}: no test proves it" for rid in sorted(set(reqs) - proven)]
    tasked = {
        rid
        for raw in raw_tasks
        if isinstance(raw, dict)
        for rid in _strings(raw.get("reqs")) or []
    }
    errors += [f"{rid}: no task implements it" for rid in sorted(set(reqs) - tasked)]
    return errors


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


def _static_dependencies(
    sources: dict[str, str], owner: dict[str, str], graph: dict[str, list[str]]
) -> list[str]:
    """A stub importing another task's module must depend on that task, or its build
    would start while the import still raises NotImplementedError."""
    index = RepoIndex.from_sources({p: s for p, s in sources.items() if p.endswith(".py")})
    errors = []
    for path, tid in sorted(owner.items()):
        for target in sorted(index.deps.get(path, ())):
            other = owner.get(target)
            if other and other != tid and other not in closure(graph, tid):
                errors.append(
                    f"{tid}: {path} imports {target}, owned by {other}; add {other} to depends_on"
                )
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
