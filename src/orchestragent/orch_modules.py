"""Hierarchical planning for large builds: module boundaries first, then one sub-plan
per module in parallel, merged deterministically.

One planner answer cannot hold hundreds of tasks with their stubs and frozen tests,
so the top level only fixes modules, their public interfaces and which requirements
each proves. Every module is then planned on its own against those interfaces; the
merge unions files and tasks and derives cross-module task dependencies from the
stubs' imports (code, not a model, decides who must wait for whom). The merged plan
passes the same lint as any plan.
"""

import ast
import re
from typing import cast

from orchestragent.live import TEXT, object_schema
from orchestragent.live_workspace import LiveWorkspace
from orchestragent.native import ContextPrompt
from orchestragent.orch_build import SPEC_AUTHORITY, spec_context
from orchestragent.orch_index import RepoIndex
from orchestragent.orch_plan import PLAN_INSTRUCTIONS, PLAN_SCHEMA, REQ_ID, TASK_ID, plan_files
from orchestragent.serialization import dumps

MODULE_NAME = re.compile(r"[a-z][a-z0-9_]{0,40}")
MAX_MODULES = 40
MODULES_SCHEMA = object_schema(
    {
        "summary": TEXT,
        "requirements": cast(dict[str, object], PLAN_SCHEMA["properties"])["requirements"],
        "modules": {
            "type": "array",
            "items": object_schema(
                {
                    "name": TEXT,
                    "goal": TEXT,
                    "reqs": {"type": "array", "items": TEXT},
                    "depends_on": {"type": "array", "items": TEXT},
                    "interface": cast(dict[str, object], PLAN_SCHEMA["properties"])["files"],
                }
            ),
        },
    }
)
MODULES_INSTRUCTIONS = SPEC_AUTHORITY + (
    "You are the top-level planner of a LARGE build that will be planned module by "
    "module. Do not write tasks or tests here. Return JSON only:\n"
    "- requirements: every atomic, testable requirement, ids R1, R2, ...\n"
    "- modules: packages at the repository root (lowercase identifiers), each with a "
    "goal, the requirements it alone proves (every requirement in exactly one module), "
    "depends_on (modules whose public interface it imports; no cycles), and its public "
    "interface: stub files under <name>/ with final signatures, type hints, docstrings "
    "and bodies `raise NotImplementedError`. Other modules may import only these files.\n"
    "Prefer modules of 5-30 tasks with narrow interfaces.\n"
)


def modules_prompt(spec: str, repo_map: str, errors: list[str], rules: str = "") -> str:
    parts = [MODULES_INSTRUCTIONS]
    parts += [f"\nLEARNED PLANNING RULES:\n{rules}"] if rules else []
    parts += ["\nREPOSITORY MAP (existing code):\n", repo_map or "(empty)\n"]
    parts += [spec_context(spec)]
    dynamic = ["\nReturn the module boundaries.\n"]
    if errors:
        dynamic += ["\nYOUR PREVIOUS MODULE PLAN FAILED LINT; fix every item:\n"]
        dynamic += [f"- {error}\n" for error in errors]
    return ContextPrompt("".join(parts), "".join(dynamic))


def module_lint(top: object, existing: frozenset[str] = frozenset()) -> list[str]:
    """Every reason a module split cannot be sub-planned; empty means accepted."""
    if not isinstance(top, dict):
        return ["module plan is not an object"]
    errors: list[str] = []
    reqs = [str(item.get("id")) for item in top.get("requirements") or []
            if isinstance(item, dict)]
    if not reqs or any(not REQ_ID.fullmatch(r) for r in reqs) or len(set(reqs)) != len(reqs):
        errors.append("requirements: need unique ids R1, R2, ...")
    modules = top.get("modules")
    if not isinstance(modules, list) or not 1 <= len(modules) <= MAX_MODULES:
        return [*errors, f"modules: need 1-{MAX_MODULES} modules"]
    names: list[str] = []
    claimed: dict[str, str] = {}
    graph: dict[str, list[str]] = {}
    for module in modules:
        if not isinstance(module, dict) or not MODULE_NAME.fullmatch(str(module.get("name"))):
            errors.append(f"modules: bad module name {module.get('name')!r}"
                          if isinstance(module, dict) else "modules: invalid entry")
            continue
        name = str(module["name"])
        if name in names:
            errors.append(f"modules: duplicate module {name}")
        names.append(name)
        if any(path == name or path.startswith(f"{name}/") or path == f"{name}.py"
               for path in existing):
            errors.append(f"{name}: already exists in the repository; choose a new package")
        graph[name] = [str(dep) for dep in module.get("depends_on") or []]
        for rid in module.get("reqs") or []:
            if str(rid) in claimed:
                errors.append(f"{rid}: claimed by {claimed[rid]} and {name}")
            claimed[str(rid)] = name
        errors += [f"{name}: {problem}" for problem in _interface_errors(name, module)]
    errors += [f"{rid}: no module proves it" for rid in reqs if rid not in claimed]
    errors += [f"{rid}: not a requirement" for rid in claimed if rid not in reqs]
    for name, deps in graph.items():
        errors += [f"{name}: unknown module dependency {dep}" for dep in deps
                   if dep not in graph or dep == name]
    seen: set[str] = set()

    def cyclic(name: str, trail: tuple[str, ...]) -> bool:
        if name in trail:
            return True
        if name in seen:
            return False
        seen.add(name)
        return any(cyclic(dep, (*trail, name)) for dep in graph.get(name, []) if dep in graph)

    errors += [f"modules: dependency cycle through {name}" for name in sorted(graph)
               if cyclic(name, ())][:1]
    return errors


def _interface_errors(name: str, module: dict[str, object]) -> list[str]:
    interface = module.get("interface")
    if not isinstance(interface, list):
        return ["interface must be a list of files"]
    problems = []
    for item in interface:
        path = item.get("path") if isinstance(item, dict) else None
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(path, str) or not isinstance(content, str):
            problems.append("interface entries need a path and content")
            continue
        if (not LiveWorkspace.safe_path(path) or not path.endswith(".py")
                or not path.startswith(f"{name}/") or LiveWorkspace.is_test_file(path)):
            problems.append(f"interface file {path} must be a non-test .py file under {name}/")
            continue
        try:
            ast.parse(content)
        except SyntaxError as error:
            problems.append(f"{path} does not parse (line {error.lineno})")
    return problems


def id_range(index: int, count: int) -> tuple[int, int]:
    """Task numbers reserved for the `index`-th of `count` modules (T-001..T-999)."""
    size = max(1, 999 // max(1, count))
    return index * size + 1, (index + 1) * size


def submodule_prompt(
    top: dict[str, object], module: dict[str, object], first: int, last: int,
    errors: list[str], rules: str = "", *, original_spec: str = "",
) -> str:
    name = str(module["name"])
    modules = cast(list[dict[str, object]], top["modules"])
    deps = [m for m in modules if m["name"] in cast(list[str], module.get("depends_on") or [])]
    wanted = set(cast(list[str], module.get("reqs") or []))
    reqs = [r for r in cast(list[dict[str, str]], top["requirements"]) if r["id"] in wanted]
    common = PLAN_INSTRUCTIONS + spec_context(original_spec)
    common += f"\nLEARNED PLANNING RULES:\n{rules}" if rules else ""
    parts = [
        f"\nYou plan ONE module of a larger build: `{name}` ({module.get('goal', '')}).\n"
        f"- Include its PUBLIC INTERFACE files below unchanged; tasks own and implement them.\n"
        f"- Every other file you add lives under {name}/ (private modules) or is a frozen test "
        f"under tests/{name}/ named test_*.py.\n"
        f"- Prove exactly these requirements and no others; number tasks T-{first:03d} to "
        f"T-{last:03d}; depends_on names only your own tasks.\n"
        "- Your code and tests may import the DEPENDENCY INTERFACES below (never edit them); "
        "dependencies on the tasks that build them are added automatically.\n",
    ]
    parts += [f"\nREQUIREMENTS:\n{dumps(reqs)}\n"]
    shared = [{"module": m["name"], "files": m["interface"]} for m in deps]
    parts += [f"\nPUBLIC INTERFACE of {name}:\n{dumps(module.get('interface', []))}\n"]
    parts += [f"\nDEPENDENCY INTERFACES:\n{dumps(shared)}\n"]
    dynamic = ["\nReturn the plan for this module.\n"]
    if errors:
        dynamic += ["\nYOUR PREVIOUS PLAN FOR THIS MODULE FAILED LINT; fix every item:\n"]
        dynamic += [f"- {error}\n" for error in errors]
    return ContextPrompt(common, "".join([*parts, *dynamic]))


def submodule_lint(
    top: dict[str, object], module: dict[str, object], first: int, last: int, sub: object
) -> list[str]:
    """Module-boundary checks for one sub-plan (the merged plan gets the full lint)."""
    if not isinstance(sub, dict):
        return ["module plan is not an object"]
    name = str(module["name"])
    errors: list[str] = []
    try:
        files = plan_files(sub)
    except (KeyError, TypeError):
        return ["files: invalid entries"]
    for item in cast(list[dict[str, str]], module.get("interface") or []):
        if files.get(item["path"]) != item["content"]:
            errors.append(f"interface file {item['path']} must be included unchanged")
    for path in files:
        if not (path.startswith(f"{name}/") or path.startswith(f"tests/{name}/")):
            errors.append(f"{path}: outside {name}/ and tests/{name}/")
    wanted = set(cast(list[str], module.get("reqs") or []))
    given = {str(r.get("id")) for r in cast(list[dict[str, object]], sub.get("requirements")
                                              or []) if isinstance(r, dict)}
    if given != wanted:
        errors.append(f"requirements must be exactly {sorted(wanted)}")
    for task in cast(list[dict[str, object]], sub.get("tasks") or []):
        tid = str(task.get("id", ""))
        if not TASK_ID.fullmatch(tid) or not first <= int(tid[2:]) <= last:
            errors.append(f"task id {tid} outside T-{first:03d}..T-{last:03d}")
    return errors


def merge(top: dict[str, object], subs: list[dict[str, object]]) -> dict[str, object]:
    """One plan from module sub-plans. Dependencies are derived from imports: a task
    whose stubs import a file another task owns waits for that task, and a task whose
    frozen tests import another module's file waits for that file's owner."""
    files: dict[str, str] = {}
    tasks: list[dict[str, object]] = []
    for sub in subs:
        files.update(plan_files(sub))
        tasks += [dict(task) for task in cast(list[dict[str, object]], sub["tasks"])]
    owner = {path: str(task["id"]) for task in tasks for path in cast(list[str], task["files"])}
    index = RepoIndex.from_sources({p: s for p, s in files.items() if p.endswith(".py")})

    def module(path: str) -> str:
        parts = path.split("/")
        return parts[1] if parts[0] == "tests" and len(parts) > 2 else parts[0]

    for task in tasks:
        tid, own = str(task["id"]), cast(list[str], task["files"])
        home = module(own[0]) if own else ""
        tests = {index.modules.get(test.rsplit(".", 2)[0], "")
                 for test in cast(list[str], task["tests"])}
        needed = {owner[t] for path in own for t in index.deps.get(path, ()) if t in owner}
        needed |= {owner[t] for path in tests for t in index.deps.get(path, ())
                   if t in owner and module(t) != home}
        task["depends_on"] = sorted({*cast(list[str], task["depends_on"]), *needed} - {tid})
    return {
        "summary": str(top.get("summary", "")),
        "requirements": top["requirements"],
        "files": [{"path": path, "content": files[path]} for path in sorted(files)],
        "tasks": sorted(tasks, key=lambda task: str(task["id"])),
    }
