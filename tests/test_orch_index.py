import copy
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest

from orchestragent.native import NativeResult
from orchestragent.orch_audit import audit
from orchestragent.orch_build import common_failures, failed_in, signature
from orchestragent.orch_cli import main
from orchestragent.orch_index import (
    Index,
    RepoIndex,
    assertions,
    dependency_code,
    grammar,
    index_file,
)
from orchestragent.orch_plan import lint
from orchestragent.orch_run import AttemptFailed, Orchestrator, Signal, Waiting
from orchestragent.workspace import Workspace
from tests.conftest import (
    ADDER_STUB,
    DOUBLER_STUB,
    GOOD_ADD,
    GOOD_DOUBLE,
    MATH_TESTS,
    Clock,
    ScriptedCaller,
    edit_result,
    local_runner,
)

needs_tree_sitter = pytest.mark.skipif(grammar("typescript") is None, reason="no tree-sitter")
ADDER = ADDER_STUB.replace("raise NotImplementedError", "return a + b")


@pytest.mark.parametrize("anchor", ["feature.py", "feature.solve", "solve", "unmatched topic"])
def test_planning_map_prefers_anchored_code_over_unrelated_centrality(anchor: str) -> None:
    sources = {"feature.py": "from helper import value\ndef solve(x): return value(x)\n",
               "helper.py": "def value(x): return x\n",
               "test_feature.py": "from feature import solve\ndef test_it(): assert solve(1)==1\n",
               **{f"unrelated{i}.py": "import central\ndef irrelevant(x): return x\n"
                  for i in range(30)},
               "central.py": "def central_api(): pass\n"}
    index = RepoIndex.from_sources(sources)
    mapped = index.planning_map(anchor, 500)
    assert len(mapped.encode()) <= 500
    assert mapped == index.planning_map(anchor, 500)
    if anchor != "unmatched topic":
        assert all(path in mapped for path in ("feature.py", "helper.py", "test_feature.py"))
    else:
        assert "No exact code anchors" in mapped and "central.py" in mapped


@pytest.mark.parametrize(
    ("path", "source", "expected"),
    [
        ("m.py", "class A(B):\n    def m(self) -> None:\n        pass\n\ndef f() -> int:\n"
         "    return 1\n", [("A", "class"), ("A.m", "method"), ("f", "function")]),
        ("broken.py", "def ok(x):\n    return x\ndef bad(:\n    pass\n",
         [("ok", "function"), ("bad", "function")]),
        ("web/app.ts", "export function f(a: number): number { return a }\n"
         "class A { m(x: string): void {} }\ninterface I { x: number }\n"
         "const g = (a: number) => a;\n",
         [("f", "function"), ("A", "class"), ("A.m", "method"), ("I", "type"), ("g", "function")]),
        ("svc/main.go", "package main\nfunc F(a int) int { return a }\ntype T struct{}\n"
         "func (t *T) M() {}\n", [("F", "function"), ("T", "type"), ("T.M", "method")]),
        ("lib.rs", "fn f(a: i32) -> i32 { a }\nstruct S;\nimpl S { fn m(&self) {} }\n",
         [("f", "function"), ("S", "type"), ("S.m", "method")]),
        ("A.java", "class A { int m(int x) { return x; } }\n", [("A", "class"), ("A.m", "method")]),
    ],
    ids=["python", "broken-python", "typescript", "go", "rust", "java"],
)
def test_symbols_cover_every_language_with_a_grammar(
    path: str, source: str, expected: list[tuple[str, str]]
) -> None:
    found = index_file(path, source.encode())
    if found.backend != "ast" and grammar(found.language) is None:
        pytest.skip("tree-sitter grammar not installed")
    assert [(s.name, s.kind) for s in found.symbols] == expected
    assert found.backend == ("ast" if path == "m.py" else "tree-sitter")


def test_python_semantics_resolve_imports_and_references() -> None:
    source = (
        "import importlib\nfrom . import helpers\nfrom .core import Engine as E\n\n\n"
        "def run(x: int) -> int:\n    return helpers.scale(E().go(x))\n\n\n"
        "def load(name: str) -> object:\n    return importlib.import_module(name)\n"
    )
    found = index_file("pkg/api.py", source.encode())
    assert found.imports == [
        "importlib", "pkg", "pkg.core", "pkg.core.Engine", "pkg.helpers",
    ]
    assert found.refs == {"run": ["pkg.core.Engine", "pkg.helpers.scale"],
                          "load": ["importlib.import_module"],
                          "": ["pkg.core.Engine", "pkg.helpers"]}
    assert found.dynamic
    assert [(s.name, s.line, s.end, s.body) for s in found.symbols] == [
        ("run", 6, 7, 7), ("load", 10, 11, 11)
    ]


@pytest.mark.parametrize("barrel,query,expected", [
    ("from core import target", "facade.target", ("core.py", "target")),
    ("from core import target as renamed", "facade.renamed", ("core.py", "target")),
    ("import core as module", "facade.module.target", ("core.py", "target")),
    ("from core import *", "facade.target", ("core.py", "target")),
    ("from core import *\nfrom other import *", "facade.target", ("facade.py", "")),
    ("from core import target\nfrom other import *", "facade.target", ("facade.py", "")),
    ("from core import target\ndef target(): return 4", "facade.target", ("facade.py", "target")),
    ("from core import target\ntarget = None", "facade.target", ("facade.py", "")),
])
def test_python_exports_resolve_only_unambiguous_static_bindings(
    barrel: str, query: str, expected: tuple[str, str],
) -> None:
    index = RepoIndex.from_sources({
        "core.py": "def target():\n    return 7\n", "other.py": "def target():\n    return 9\n",
        "facade.py": barrel,
    })
    assert index.resolve(query) == expected


@pytest.mark.parametrize("exported", [True, False])
def test_wildcard_exports_respect_explicit_all_including_private_aliases(exported: bool) -> None:
    index = RepoIndex.from_sources({
        "core.py": "def target():\n    return 7\n",
        "facade.py": "from core import target as _hidden\n"
                     + ("__all__ = ['_hidden']\n" if exported else "__all__ = []\n"),
        "api.py": "from facade import *\n",
    })
    assert index.resolve("api._hidden") == (("core.py", "target") if exported else ("api.py", ""))


@pytest.mark.parametrize("first", ["a", "b"])
def test_cyclic_export_search_does_not_poison_later_resolution(first: str) -> None:
    index = RepoIndex.from_sources({
        "a.py": "from b import *\nfrom core import *\n",
        "b.py": "from a import *\n", "core.py": "def target():\n    return 7\n",
    })
    assert index.resolve(first + ".target") == ("core.py", "target")
    assert {index.resolve(name + ".target") for name in ("a", "b")} == {("core.py", "target")}


@pytest.mark.parametrize("mutation", ["__all__.clear()", "__all__ += names",
                                     "if condition:\n    __all__ = []"])
def test_dynamic_all_is_not_guessed_or_executed(mutation: str) -> None:
    index = RepoIndex.from_sources({
        "core.py": "def target(): return 7\n",
        "facade.py": "from core import *\n__all__ = ['target']\n" + mutation,
        "api.py": "from facade import *\n",
    })
    assert index.resolve("api.target") == ("api.py", "")


@pytest.fixture
def mathops() -> RepoIndex:
    return RepoIndex.from_sources({
        "adder.py": ADDER,
        "doubler.py": DOUBLER_STUB,
        "test_adder.py": "import unittest\n\nfrom adder import add\n\n\nclass AddTest("
                         "unittest.TestCase):\n    def test_r1_adds(self) -> None:\n"
                         "        self.assertEqual(add(2, 3), 5)\n",
        "test_mathops.py": MATH_TESTS,
        "plugins.py": "import importlib\n\nimport adder\n\n\ndef load() -> object:\n"
                      "    return importlib.import_module('x')\n",
        "test_plugins.py": "import unittest\n\nimport plugins\n\n\nclass P(unittest.TestCase):"
                           "\n    def test_r3_loads(self) -> None:\n        plugins.load()\n",
    })


TESTS = ["test_adder.AddTest.test_r1_adds", "test_mathops.DoubleTest.test_r2_doubles",
         "test_plugins.P.test_r3_loads"]


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (["doubler.py"], TESTS[1:]),
        (["adder.py"], TESTS),
        (["notes.md"], TESTS),
        (["other.py"], TESTS[2:]),
    ],
    ids=["unrelated-module-skipped", "shared-dependency", "non-python-change", "dynamic-import"],
)
def test_impact_analysis_selects_only_tests_that_can_observe_a_change(
    mathops: RepoIndex, changed: list[str], expected: list[str]
) -> None:
    assert mathops.affected(TESTS, changed) == expected


def test_dependency_code_follows_imports_and_calls_but_skips_stubs(mathops: RepoIndex) -> None:
    sources = {"adder.py": ADDER, "doubler.py": DOUBLER_STUB}
    roots = mathops.roots(["doubler.py"], ["test_mathops.DoubleTest.test_r2_doubles"])
    assert roots == [
        ("doubler.py", ""),
        ("test_mathops.py", "DoubleTest"),
        ("test_mathops.py", "DoubleTest.test_r2_doubles"),
    ]
    add = 'def add(a: int, b: int) -> int:\n    """Return a + b."""\n    return a + b\n'
    packed = dependency_code(mathops, lambda paths: sources, roots, {"doubler.py"}, 5000)
    assert packed == {"adder.py::add": add}  # the stub's import; double itself is a stub
    assert dependency_code(mathops, lambda paths: sources, roots, {"doubler.py"}, 20) == {}


@pytest.mark.parametrize(("available", "missing"), [((), ["numpy"]), (("numpy",), [])])
def test_missing_imports_name_third_party_packages_only(
    available: tuple[str, ...], missing: list[str],
) -> None:
    index = RepoIndex.from_sources({
        "pkg/mod.py": "import json\nimport numpy as np\nfrom . import sibling\nfrom pkg import x\n",
        "pkg/sibling.py": "",
        "ns/deep/leaf.py": "import ns.deep\n",
    })
    assert index.missing_imports("pkg/mod.py", available) == missing
    assert index.missing_imports("ns/deep/leaf.py") == []


def test_import_roots_reuse_blob_parses_and_partition_revision_cache(
    orch_repo: Path, tmp_path: Path,
) -> None:
    source = orch_repo / "src" / "pkg"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("from .mod import value\n")
    (source / "mod.py").write_text("def value():\n    return 1\n")
    (orch_repo / "consumer.py").write_text("from pkg import value\n")
    Workspace.git(orch_repo, "add", ".")
    Workspace.git(orch_repo, "commit", "-m", "src layout")
    revision = Workspace.git(orch_repo, "rev-parse", "HEAD")
    index = Index(orch_repo, tmp_path / "index.sqlite")
    original = index.at(revision)
    count = index.parsed
    index.source_roots = (".", "src")
    rooted = index.at(revision)
    assert rooted.missing_imports("consumer.py") == []
    assert rooted.deps["consumer.py"] == frozenset({"src/pkg/__init__.py"})
    assert rooted.deps["src/pkg/__init__.py"] == frozenset({"src/pkg/mod.py"})
    assert original.missing_imports("consumer.py") == ["pkg"] and index.parsed == count
    index.source_roots = (".",)
    assert index.at(revision) is original


def test_import_roots_reject_ambiguous_modules() -> None:
    with pytest.raises(ValueError, match="Ambiguous project import"):
        RepoIndex.from_sources({"pkg.py": "", "src/pkg.py": ""}, (".", "src"))


def test_repo_map_keeps_the_most_used_files_when_over_budget(mathops: RepoIndex) -> None:
    assert mathops.repo_map(100_000).startswith(
        "adder.py\n  def add(a: int, b: int) -> int\ndoubler.py\n"
    )
    assert mathops.repo_map(120) == (
        "adder.py\n  def add(a: int, b: int) -> int\nplugins.py\n  def load() -> object\n"
        "(repo map truncated: 4 of 6 files omitted)\n"
    )


def test_assertions_are_counted_per_test_method() -> None:
    source = MATH_TESTS + (
        "\n\nclass More(unittest.TestCase):\n    def test_r1_many(self) -> None:\n"
        "        assert add(1, 1) == 2\n        with self.assertRaises(TypeError):\n"
        "            add('a', 1)\n        self.assertTrue(add(0, 0) == 0)\n"
        "    def helper(self) -> None:\n        self.assertEqual(1, 1)\n"
    )
    assert assertions(source) == {
        "AddTest.test_r1_adds": 1, "DoubleTest.test_r2_doubles": 1, "More.test_r1_many": 3,
    }


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    Workspace.git(repo, "add", "-A")
    Workspace.git(repo, "commit", "-m", message)
    return Workspace.git(repo, "rev-parse", "HEAD")


def test_index_parses_each_blob_once_and_persists_across_processes(
    orch_repo: Path, tmp_path: Path
) -> None:
    first = commit(orch_repo, {"adder.py": ADDER, "doubler.py": DOUBLER_STUB}, "two")
    index = Index(orch_repo, tmp_path / "index.sqlite")
    assert sorted(index.at(first).files) == ["adder.py", "doubler.py"] and index.parsed == 2
    second = commit(orch_repo, {"doubler.py": DOUBLER_STUB.replace("raise NotImplementedError",
                                                                   "return add(a, a)")}, "impl")
    assert index.at(second).files["doubler.py"].refs == {"double": ["adder.add"],
                                                         "": ["adder.add"]}
    assert index.parsed == 3  # only the changed blob
    again = Index(orch_repo, tmp_path / "index.sqlite")
    again.at(second)
    assert again.parsed == 0 and again.read(second, ["adder.py", "nope.py"]) == {
        "adder.py": ADDER
    }
    assert index.snapshot(first, "r1/plan") == 2 and again.snapshots() == [(first, "r1/plan", 2)]


def test_cochange_graph_comes_from_git_history(orch_repo: Path, tmp_path: Path) -> None:
    for round_ in range(3):
        commit(orch_repo, {"a.py": f"A = {round_}\n", "b.py": f"B = {round_}\n"}, "pair")
    commit(orch_repo, {"a.py": "A = 9\n"}, "alone")
    commit(orch_repo, {f"bulk{n}.py": "" for n in range(40)} | {"b.py": "B = 9\n"}, "bulk")
    coupling = Index(orch_repo, tmp_path / "i.sqlite").cochange("HEAD")
    # b.py's fourth change sits in a 41-file commit, which says nothing about coupling.
    assert [(c.a, c.b, c.together, c.weight) for c in coupling] == [("a.py", "b.py", 3, 1.0)]


@pytest.mark.parametrize(
    ("other", "same"),
    [
        ('  File "/x/.git/orch/r1/work/T-001-a2-ffffff/adder.py", line 9, in add', True),
        ('  File "/x/.git/orch/r1/work/T-001-a1-abcdef/adder.py", line 3, in sub', False),
        ('  File "/usr/lib/python3.12/unittest/case.py", line 58, in testPartExecutor', True),
        ('  File "/work/adder.py", line 4, in add', True),
    ],
    ids=["moved-line-other-worktree", "other-function", "stdlib-frame-ignored", "bwrap-mount"],
)
def test_failure_signatures_use_symbols_not_lines_or_paths(other: str, same: bool) -> None:
    report = ("ERROR: test_r1_adds (m.AddTest.test_r1_adds)\nTraceback:\n"
              '  File "/x/.git/orch/r1/work/T-001-a1-abcdef/adder.py", line 3, in add\n'
              "TypeError: boom\n")
    lines = report.splitlines()
    changed = "\n".join([*lines[:2], other if "adder.py" in other else lines[2], *lines[3:]])
    if "unittest" in other:
        changed = report.replace("Traceback:\n", f"Traceback:\n{other}\n")
    assert (signature("test", report) == signature("test", changed)) is same


SPLIT_TESTS = {
    "test_adder.py": "import unittest\n\nfrom adder import add\n\n\nclass AddTest("
                     "unittest.TestCase):\n    def test_r1_adds(self) -> None:\n"
                     "        self.assertEqual(add(2, 3), 5)\n",
    "test_doubler.py": "import unittest\n\nfrom doubler import double\n\n\nclass DoubleTest("
                       "unittest.TestCase):\n    def test_r2_doubles(self) -> None:\n"
                       "        self.assertEqual(double(4), 8)\n",
}


@pytest.fixture
def split_plan(math_plan: dict[str, object]) -> dict[str, object]:
    plan = copy.deepcopy(math_plan)
    plan["files"] = [*cast(list[object], plan["files"])[:2],
                     *({"path": p, "content": c} for p, c in SPLIT_TESTS.items())]
    tasks = cast(list[dict[str, object]], plan["tasks"])
    tasks[0]["tests"] = ["test_adder.AddTest.test_r1_adds"]
    tasks[1]["tests"] = ["test_doubler.DoubleTest.test_r2_doubles"]
    return plan


def orchestrate(
    repo: Path, plan: dict[str, object], answers: dict[str, list[NativeResult]]
) -> tuple[Orchestrator, ScriptedCaller]:
    caller = ScriptedCaller({"plan": [NativeResult("ok", plan)], **answers})
    return Orchestrator(repo, caller, local_runner, notify=lambda line: None, clock=Clock()), caller


def test_builder_sees_merged_dependency_code_and_gates_skip_unaffected_tests(
    orch_repo: Path, split_plan: dict[str, object]
) -> None:
    orchestrator, caller = orchestrate(
        orch_repo, split_plan, {"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]}
    )
    run = orchestrator.execute(orchestrator.plan("spec").id, jobs=1)
    prompt = next(text for _, key, text in caller.calls if key == "dsh:T-002")
    gates = {e.task_id: e.data for e in orchestrator.state.events(run.id) if e.kind == "task.gate"}
    assert run.status == "done"
    # A building-on task reads its dependency's merged file whole (since dfdc631).
    context = prompt.split("REQUESTED READ-ONLY CONTEXT", 1)[1]
    assert "--- adder.py ---" in context and "return a + b" in context
    assert gates == {"T-001": {"tests": 1, "skipped": 0}, "T-002": {"tests": 1, "skipped": 1}}


def test_final_gate_bisects_a_regression_the_impact_analysis_missed(
    orch_repo: Path, split_plan: dict[str, object]
) -> None:
    def runner(execution: Path, scratch: Path, revision: str, names: list[str] | None,
               *, timeout: float = 25.0) -> tuple[bool, str]:
        hidden = "HIDDEN" in (execution / "doubler.py").read_text()
        if hidden and names and "test_adder.AddTest.test_r1_adds" in names:
            return False, ("FAIL: test_r1_adds (test_adder.AddTest.test_r1_adds)\n"
                           "Ran 1 test\n\nFAILED (failures=1)\n")
        return local_runner(execution, scratch, revision, names)

    sneaky = edit_result("doubler.py", "return add(a, a)  # HIDDEN coupling")
    orchestrator, _ = orchestrate(
        orch_repo, split_plan, {"dsh:T-001": [GOOD_ADD], "dsh:T-002": [sneaky, GOOD_DOUBLE]}
    )
    orchestrator.runner = runner
    run = orchestrator.execute(orchestrator.plan("spec").id, jobs=1)
    regression = [e.data for e in orchestrator.state.events(run.id) if e.kind == "run.regression"]
    assert (run.status, regression) == ("done", [{"culprit": "T-002", "reopened": ["T-002"]}])
    assert "HIDDEN" not in Workspace.git(orch_repo, "show", f"{run.branch}:doubler.py")


def test_final_gate_parks_instead_of_blaming_when_tests_interfere(
    orch_repo: Path, split_plan: dict[str, object]
) -> None:
    def runner(execution: Path, scratch: Path, revision: str, names: list[str] | None,
               *, timeout: float = 25.0) -> tuple[bool, str]:
        together = names and {"test_adder.AddTest.test_r1_adds",
                              "test_doubler.DoubleTest.test_r2_doubles"} <= set(names)
        if together:  # one test leaks state into the other only when both run
            return False, ("FAIL: test_r1_adds (test_adder.AddTest.test_r1_adds)\n"
                           "Ran 2 tests\n\nFAILED (failures=1)\n")
        return local_runner(execution, scratch, revision, names)

    orchestrator, _ = orchestrate(
        orch_repo, split_plan, {"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]}
    )
    orchestrator.runner = runner
    run = orchestrator.execute(orchestrator.plan("spec").id, jobs=1)
    kinds = [e.kind for e in orchestrator.state.events(run.id)]
    assert (run.status, "run.regression" in kinds) == ("parked", False)
    assert run.message.startswith(
        "test_adder.AddTest.test_r1_adds fails only together with other tests"
    )


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ("FAIL: t (m.A.test_r1_x)\nERROR: u (m.A.test_r2_y)\nFAILED (failures=1, errors=1)\n",
         ["m.A.test_r1_x", "m.A.test_r2_y"]),
        ("ERROR: test_m (unittest.loader._FailedTest.test_m)\nFAILED (errors=1)\n",
         ["pkg.test_m.A.test_r1_x", "pkg.test_m.A.test_r2_y"]),
        ("ERROR: u (m.A.test_r2_y)\nFAILED (failures=3, errors=1)\n", None),
        ("Ran 2 tests\n", None),
    ],
    ids=["named", "import-error", "cut-tail", "no-summary"],
)
def test_failures_are_attributed_only_from_complete_reports(
    report: str, expected: list[str] | None
) -> None:
    tests = ["m.A.test_r1_x", "m.A.test_r2_y", "pkg.test_m.A.test_r1_x", "pkg.test_m.A.test_r2_y"]
    assert failed_in(report, tests) == expected


@pytest.mark.parametrize("second,expected", [
    ("FAIL: t (m.A.test_r1_x)\nFAILED (failures=1)\n", ["m.A.test_r1_x"]),
    ("FAIL: t (m.A.test_r2_y)\nFAILED (failures=1)\n", []),
    ("FAIL: t (m.A.test_r1_x)\nFAILED (failures=2)\n", []),
    ("FAIL: t (m.A.test_r1_x)\n", []),
])
def test_candidate_agreement_requires_complete_shared_failure_evidence(
    second: str, expected: list[str],
) -> None:
    first = "FAIL: t (m.A.test_r1_x)\nFAILED (failures=1)\n"
    tests = ["m.A.test_r1_x", "m.A.test_r2_y"]
    assert common_failures([first, second], tests) == expected
    assert common_failures([first], tests) == []


def test_impact_analysis_follows_package_init_chains() -> None:
    index = RepoIndex.from_sources({
        "pkg/__init__.py": "from pkg.core import X\n",
        "pkg/core.py": "X = 1\n",
        "pkg/mod.py": "def f() -> int:\n    return 1\n",
        "tests/test_m.py": "import unittest\n\nfrom pkg.mod import f\n",
    })
    tests = ["tests.test_m.T.test_r1_a"]
    assert index.affected(tests, ["pkg/core.py"]) == tests  # via pkg/__init__.py
    assert index.deps["pkg/mod.py"] == frozenset()  # task order ignores the __init__


def test_imports_outside_the_sandbox_fail_the_attempt_with_a_clear_error(
    orch_repo: Path, math_plan: dict[str, object]
) -> None:
    third_party = edit_result("adder.py", "import numpy\n    return a + b")
    orchestrator, caller = orchestrate(
        orch_repo, math_plan, {"dsh:T-001": [third_party, GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]}
    )
    run = orchestrator.execute(orchestrator.plan("spec").id)
    failed = [e.data for e in orchestrator.state.events(run.id, "T-001") if e.kind == "task.failed"]
    retry = next(text for _, key, text in caller.calls[2:] if key == "dsh:T-001")
    assert (run.status, [f["category"] for f in failed]) == ("done", ["import"])
    assert "adder.py imports numpy" in retry and "pinned environment" in retry


@pytest.fixture
def implicit_import_plan(math_plan: dict[str, object]) -> dict[str, object]:
    plan = copy.deepcopy(math_plan)
    stub = "def triple(a: int) -> int:\n    raise NotImplementedError\n"
    files = cast(list[dict[str, str]], plan["files"])
    files.append({"path": "tripler.py", "content": stub})
    files[2]["content"] = MATH_TESTS.replace(
        "from doubler import double\n", "from doubler import double\nfrom tripler import triple\n"
    ) + (
        "\n\nclass TripleTest(unittest.TestCase):\n    def test_r3_triples(self) -> None:\n"
        "        self.assertEqual(triple(2), 6)\n"
    )
    cast(list[object], plan["requirements"]).append({"id": "R3", "text": "triple"})
    cast(list[object], plan["tasks"]).append({
        "id": "T-003", "goal": "triple", "reqs": ["R3"], "files": ["tripler.py"],
        "depends_on": ["T-001"], "tests": ["test_mathops.TripleTest.test_r3_triples"],
        "contract": "3a",
    })
    return plan


@pytest.mark.parametrize("interrupted", [False, True])
def test_dependency_handoff_reuses_code_without_model_communication_even_after_restart(
    orch_repo: Path, implicit_import_plan: dict[str, object],
    monkeypatch: pytest.MonkeyPatch, interrupted: bool,
) -> None:
    triple = edit_result("tripler.py", "from doubler import double\n    return double(a) + a")
    # T-003 may generate while T-002 still verifies (generation and verification are
    # separate pools); its immutable candidate waits for T-002, then replays without inference.
    orchestrator, caller = orchestrate(
        orch_repo, implicit_import_plan,
        {"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE], "dsh:T-003": [triple]},
    )
    handed_off = Event()
    check, wait_for = orchestrator._check, orchestrator._wait

    def checked(*args: Any, **kwargs: Any) -> AttemptFailed | Signal | None:
        if args[2].id == "T-002":
            assert handed_off.wait(5), "dependency handoff deadlocked"
        return check(*args, **kwargs)

    def waiting(run_id: str, task_id: str, number: int, outcome: Waiting) -> None:
        wait_for(run_id, task_id, number, outcome)
        if interrupted:
            orchestrator.cancel.set()
        handed_off.set()

    monkeypatch.setattr(orchestrator, "_check", checked)
    monkeypatch.setattr(orchestrator, "_wait", waiting)
    run = orchestrator.execute(orchestrator.plan("spec").id, jobs=2)
    if interrupted:
        assert run.status == "paused"
        orchestrator = Orchestrator(orch_repo, caller, local_runner, notify=lambda _: None)
        run = orchestrator.execute(run.id, jobs=2)
    task = orchestrator.state.task(run.id, "T-003")
    kinds = [e.kind for e in orchestrator.state.events(run.id, "T-003")]
    assert (run.status, task.depends_on, task.level) == ("done", ["T-001", "T-002"], 0)
    assert "task.failed" not in kinds and task.attempts == 1  # a wait costs no rung
    assert handed_off.is_set() and "task.candidate_reused" in kinds
    assert caller.keys().count("dsh:T-003") == 1 and len(caller.calls) == 4
    assert audit(orchestrator, run.id, run_tests=False) == []


@pytest.mark.parametrize("dependency_in_candidate", [False, True])
def test_done_dependency_must_be_present_in_candidate_revision(
    orch_repo: Path, implicit_import_plan: dict[str, object],
    monkeypatch: pytest.MonkeyPatch, dependency_in_candidate: bool,
) -> None:
    orchestrator, _ = orchestrate(orch_repo, implicit_import_plan, {})
    run = orchestrator.plan("snapshot race")
    with orchestrator.worktree(run.id, "completed-dependency", run.checkpoint) as tree:
        (tree / "doubler.py").write_text(DOUBLER_STUB.replace(
            "raise NotImplementedError", "return a * 2"))
        Workspace.git(tree, "commit", "-am", "completed sibling")
        completed = Workspace.git(tree, "rev-parse", "HEAD")
    base = completed if dependency_in_candidate else run.checkpoint
    with orchestrator.worktree(run.id, "candidate", base) as tree:
        (tree / "tripler.py").write_text(
            "from doubler import double\ndef triple(a: int) -> int:\n    return double(a) + a\n")
        Workspace.git(tree, "commit", "-am", "candidate imports sibling")
        candidate = Workspace.git(tree, "rev-parse", "HEAD")
    tasks, owners = orchestrator._owners(run.id)
    tasks["T-002"] = replace(tasks["T-002"], status="DONE", commit=completed)
    monkeypatch.setattr(orchestrator, "_owners", lambda _: (tasks, owners))
    outcome = orchestrator._imports(run.id, tasks["T-003"], candidate, "")
    assert (type(outcome).__name__ if outcome else "ready") == (
        "ready" if dependency_in_candidate else "Waiting")


@pytest.mark.parametrize(("reverse", "expected"), [(False, "Waiting"), (True, "AttemptFailed")])
def test_importing_an_unbuilt_sibling_waits_unless_that_would_be_a_cycle(
    orch_repo: Path, math_plan: dict[str, object], reverse: bool, expected: str
) -> None:
    plan = copy.deepcopy(math_plan)
    cast(list[dict[str, str]], plan["files"]).append({"path": "extra.py", "content": "Y = 1\n"})
    tasks = cast(list[dict[str, object]], plan["tasks"])
    tasks[0]["files"] = ["adder.py", "extra.py"]  # T-001 may import doubler (T-002)
    orchestrator, _ = orchestrate(orch_repo, plan, {})
    run = orchestrator.plan("spec")
    with orchestrator.worktree(run.id, "T-001-a1", run.checkpoint) as tree:
        (tree / "extra.py").write_text("from doubler import double\n\nY = double(1)\n")
        Workspace.git(tree, "commit", "-am", "imports a sibling")
        commit = Workspace.git(tree, "rev-parse", "HEAD")
    task = orchestrator.state.task(run.id, "T-001")
    if not reverse:  # without T-002 -> T-001 the wait is safe
        orchestrator.state.transition(run.id, "T-002", "PLANNED", "test.setup", {},
                                      depends_on=[])
    found = orchestrator._imports(run.id, task, commit, "")
    assert type(found).__name__ == expected
    assert found is not None and ("T-002" in found.detail)


def test_lint_requires_a_dependency_for_every_imported_stub(math_plan: dict[str, object]) -> None:
    cast(list[dict[str, object]], math_plan["tasks"])[1]["depends_on"] = []
    assert lint(math_plan) == [
        "T-002: doubler.py imports adder.py, owned by T-001; add T-001 to depends_on"
    ]


def test_test_corrections_may_not_delete_assertions(
    orch_repo: Path, math_plan: dict[str, object]
) -> None:
    files = cast(list[dict[str, str]], math_plan["files"])
    files[2]["content"] = MATH_TESTS.replace("3), 5", "3), 6")
    weaken = NativeResult("ok", {
        "verdict": "tests_wrong", "explanation": "drop it",
        "edits": [{"path": "test_mathops.py", "search": "        self.assertEqual(add(2, 3), 6)\n",
                   "replace": "        pass\n"}],
    })
    dispute = edit_result("adder.py", "", "TEST_DISPUTE")
    stop = NativeResult("quota_exhausted", {}, message="stop here")
    orchestrator, calls = orchestrate(
        orch_repo, math_plan, {"dsh:T-001": [dispute, stop], "claude:T-001": [weaken]}
    )
    run = orchestrator.execute(orchestrator.plan("spec").id)
    failed = [e.data for e in orchestrator.state.events(run.id, "T-001") if e.kind == "task.failed"]
    task = orchestrator.state.task(run.id, "T-001")
    assert (run.status, [f["category"] for f in failed]) == ("paused", ["dispute"])
    assert task.last_error == (
        "test correction removes assertions from ['test_mathops.py::AddTest.test_r1_adds']"
    )
    assert "3), 6" in Workspace.git(orch_repo, "show", f"{run.branch}:test_mathops.py")
    assert task.pending_review == ""
    assert calls.keys().count("claude:T-001") == 1


def test_index_and_trace_commands_use_the_symbol_index(
    orch_repo: Path, split_plan: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    orchestrator, _ = orchestrate(
        orch_repo, split_plan, {"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]}
    )
    run = orchestrator.execute(orchestrator.plan("spec").id)
    capsys.readouterr()
    assert main(["--repo", str(orch_repo), "index", "add"]) == 0
    found = capsys.readouterr().out.splitlines()
    assert main(["--repo", str(orch_repo), "trace", "double"]) == 0
    traced = capsys.readouterr().out.splitlines()
    plan_commit = orchestrator.git(orch_repo, "rev-parse", f"orch/{run.id}/plan")[:10]
    task_commit = orchestrator.state.task(run.id, "T-002").commit[:10]
    assert found == [
        "adder.py:4  function def add(a: int, b: int) -> int",
        "  used by doubler.py::<module>",
        "  used by doubler.py::double",
        "  used by test_adder.py::<module>",
        "  used by test_adder.py::AddTest.test_r1_adds",
    ]
    assert traced[0] == f"doubler.py:8 <- commit {task_commit}"
    assert traced[-3:] == [
        "  function double lines 6-8:",
        f"    {plan_commit}   2 line(s)  (not an orch task commit)",
        f"    {task_commit}   1 line(s)  T-002",
    ]
