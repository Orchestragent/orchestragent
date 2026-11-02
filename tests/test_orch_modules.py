import copy
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from orchestragent.native import ContextPrompt, NativeResult
from orchestragent.orch_build import SPEC_AUTHORITY
from orchestragent.orch_modules import (
    id_range,
    merge,
    module_lint,
    modules_prompt,
    submodule_lint,
    submodule_prompt,
)
from orchestragent.orch_plan import lint
from orchestragent.orch_run import Orchestrator, PlanError
from tests.conftest import Clock, edit_result, local_runner

Plan = dict[str, object]
CORE = "def add(a: int, b: int) -> int:\n    raise NotImplementedError\n"
EXT = ("from mathcore.adder import add\n\n\ndef double(a: int) -> int:\n"
       "    raise NotImplementedError\n")
CORE_TESTS = ("import unittest\n\nfrom mathcore.adder import add\n\n\nclass AddTest("
              "unittest.TestCase):\n    def test_r1_adds(self) -> None:\n"
              "        self.assertEqual(add(2, 3), 5)\n")
EXT_TESTS = ("import unittest\n\nfrom mathext.doubler import double\n\n\nclass DoubleTest("
             "unittest.TestCase):\n    def test_r2_doubles(self) -> None:\n"
             "        self.assertEqual(double(4), 8)\n")


@pytest.fixture
def top() -> Plan:
    return {
        "summary": "math in two modules",
        "requirements": [{"id": "R1", "text": "add"}, {"id": "R2", "text": "double"}],
        "modules": [
            {"name": "mathcore", "goal": "arithmetic", "reqs": ["R1"], "depends_on": [],
             "interface": [{"path": "mathcore/adder.py", "content": CORE}]},
            {"name": "mathext", "goal": "derived ops", "reqs": ["R2"],
             "depends_on": ["mathcore"],
             "interface": [{"path": "mathext/doubler.py", "content": EXT}]},
        ],
    }


def sub(name: str, path: str, stub: str, tests: str, test_id: str, tid: str, req: str) -> Plan:
    return {
        "summary": name, "requirements": [{"id": req, "text": req}],
        "files": [{"path": path, "content": stub},
                  {"path": f"tests/{name}/test_{name}.py", "content": tests}],
        "tasks": [{"id": tid, "goal": f"build {name}", "reqs": [req], "files": [path],
                   "depends_on": [], "tests": [f"tests.{name}.test_{name}.{test_id}"],
                   "contract": "as specified"}],
    }


@pytest.fixture
def subs() -> dict[str, Plan]:
    return {
        "mathcore": sub("mathcore", "mathcore/adder.py", CORE, CORE_TESTS,
                        "AddTest.test_r1_adds", "T-001", "R1"),
        "mathext": sub("mathext", "mathext/doubler.py", EXT, EXT_TESTS,
                       "DoubleTest.test_r2_doubles", "T-500", "R2"),
    }


def test_a_clean_module_split_passes(top: Plan) -> None:
    assert module_lint(top) == []
    assert id_range(0, 2) == (1, 499) and id_range(1, 2) == (500, 998)


@pytest.mark.parametrize("stage", ["top", "submodule"])
def test_module_lint_feedback_is_outside_cached_context(top: Plan, stage: str) -> None:
    module = cast(list[Plan], top["modules"])[0]
    first = (modules_prompt("spec", "index", []) if stage == "top"
             else submodule_prompt(top, module, 1, 100, []))
    retry = (modules_prompt("spec", "index", ["ERR_SENTINEL_42"]) if stage == "top"
             else submodule_prompt(top, module, 1, 100, ["ERR_SENTINEL_42"]))
    assert isinstance(first, ContextPrompt) and isinstance(retry, ContextPrompt)
    assert first.shared == retry.shared
    assert "ERR_SENTINEL_42" in retry.dynamic and "ERR_SENTINEL_42" not in retry.shared


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda t: t["modules"][1].update(depends_on=["nope"]),
         "mathext: unknown module dependency nope"),
        (lambda t: t["modules"][0].update(depends_on=["mathext"]),
         "modules: dependency cycle through mathcore"),
        (lambda t: t["modules"][1].update(reqs=["R1"]), "R1: claimed by mathcore and mathext"),
        (lambda t: t["modules"][0].update(reqs=[]), "R1: no module proves it"),
        (lambda t: t["modules"][0].update(name="Math-Core"), "bad module name 'Math-Core'"),
        (lambda t: t["modules"][0]["interface"][0].update(path="elsewhere/adder.py"),
         "interface file elsewhere/adder.py must be a non-test .py file under mathcore/"),
        (lambda t: t["modules"][0]["interface"][0].update(content="def add(:\n"),
         "mathcore/adder.py does not parse"),
    ],
)
def test_module_lint_names_every_unbuildable_split(
    top: Plan, mutate: Callable[[dict[str, list[dict[str, object]]]], None], expected: str
) -> None:
    broken = copy.deepcopy(top)
    mutate(cast(dict[str, list[dict[str, object]]], broken))
    assert any(expected in error for error in module_lint(broken)), module_lint(broken)


def test_module_lint_refuses_packages_that_already_exist(top: Plan) -> None:
    assert module_lint(top, frozenset({"mathcore/old.py"})) == [
        "mathcore: already exists in the repository; choose a new package"
    ]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda s: s["files"][0].update(content=CORE + "# edited\n"),
         "interface file mathcore/adder.py must be included unchanged"),
        (lambda s: s["files"].append({"path": "other/x.py", "content": ""}),
         "other/x.py: outside mathcore/ and tests/mathcore/"),
        (lambda s: s["tasks"][0].update(id="T-600"), "task id T-600 outside T-001..T-499"),
        (lambda s: s["requirements"].append({"id": "R2", "text": "x"}),
         "requirements must be exactly ['R1']"),
    ],
)
def test_sub_plans_stay_inside_their_module(
    top: Plan, subs: dict[str, Plan],
    change: Callable[[dict[str, list[dict[str, object]]]], None], expected: str,
) -> None:
    module = cast(list[dict[str, object]], top["modules"])[0]
    plan = copy.deepcopy(subs["mathcore"])
    change(cast(dict[str, list[dict[str, object]]], plan))
    assert expected in submodule_lint(top, module, 1, 499, plan)


def test_merge_derives_cross_module_dependencies_from_imports(
    top: Plan, subs: dict[str, Plan]
) -> None:
    merged = merge(top, [subs["mathcore"], subs["mathext"]])
    tasks = cast(list[dict[str, object]], merged["tasks"])
    assert [(t["id"], t["depends_on"]) for t in tasks] == [("T-001", []), ("T-500", ["T-001"])]
    assert lint(merged) == []


@dataclass
class Planners:
    """Answers the module split, each module's sub-plan and the builder by run id."""

    top: Plan
    subs: dict[str, list[Plan]]
    seen: list[str] = field(default_factory=list)
    prompts: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, provider: str, model: str, prompt: str, schema: dict[str, object],
                 run_id: str, cancelled: Callable[[], bool], *, effort: str = "") -> NativeResult:
        self.seen.append(run_id)
        self.prompts.append((run_id, prompt))
        if "-modules-" in run_id:
            return NativeResult("ok", self.top)
        found = re.search(r"-module-([a-z]+)-", run_id)
        if found:
            return NativeResult("ok", self.subs[found.group(1)].pop(0))
        task = re.search(r"T-[0-9]{3}", run_id)
        assert task is not None
        body = {"T-001": ("mathcore/adder.py", "return a + b"),
                "T-500": ("mathext/doubler.py", "return add(a, a)")}[task.group(0)]
        return edit_result(*body)


def test_hierarchical_plan_runs_end_to_end(
    orch_repo: Path, top: Plan, subs: dict[str, Plan]
) -> None:
    wrong = copy.deepcopy(subs["mathext"])
    cast(list[dict[str, object]], wrong["tasks"])[0]["id"] = "T-002"
    planners = Planners(top, {"mathcore": [subs["mathcore"]], "mathext": [wrong, subs["mathext"]]})
    orchestrator = Orchestrator(orch_repo, planners, local_runner, notify=lambda line: None,
                                clock=Clock())
    spec = "Original: implement addition and doubling; preserve every supplied argument."
    run = orchestrator.execute(orchestrator.plan(spec, modules=True).id)
    proxy = next(e.data for e in orchestrator.state.events(run.id) if e.kind == "plan.proxy")
    assert run.status == "done"
    assert [t.id for t in orchestrator.state.tasks(run.id)] == ["T-001", "T-500"]
    assert orchestrator.state.task(run.id, "T-500").depends_on == ["T-001"]
    assert proxy["repairs"] == 1  # the sub-plan that numbered outside its range
    assert sum("-module-mathext-" in run_id for run_id in planners.seen) == 2
    prompts = [prompt for run_id, prompt in planners.prompts
               if "-module-" in run_id and isinstance(prompt, ContextPrompt)]
    assert len(prompts) == 3 and len({prompt.shared for prompt in prompts}) == 1
    assert all(spec in prompt.shared and SPEC_AUTHORITY in prompt.shared for prompt in prompts)
    assert all("PUBLIC INTERFACE" in prompt.dynamic for prompt in prompts)
    calls = orchestrator.state.usage_calls(run.id)
    assert sorted((c.purpose, c.sample, c.attempt) for c in calls if not c.task_id) == [
        ("module", "mathcore", 1), ("module", "mathext", 1), ("module", "mathext", 2),
        ("modules", "", 1),
    ]


def test_a_module_split_that_never_lints_is_refused(orch_repo: Path, top: Plan) -> None:
    broken = copy.deepcopy(top)
    cast(list[dict[str, object]], broken["modules"])[0]["reqs"] = []
    orchestrator = Orchestrator(orch_repo, Planners(broken, {}), local_runner,
                                notify=lambda line: None)
    with pytest.raises(PlanError) as refused:
        orchestrator.plan("big spec", modules=True, attempts=2)
    assert refused.value.errors == ["R1: no module proves it"]
