from pathlib import Path

import pytest

from orchestragent.native import NativeResult
from orchestragent.orch_build import common_failures, failed_tests, signature
from orchestragent.orch_cli import main
from orchestragent.orch_index import Index, RepoIndex, grammar, index_file
from orchestragent.orch_run import Orchestrator
from orchestragent.workspace import Workspace
from tests.conftest import (
    ADD_TEST,
    ADD_TESTS,
    ADDER_STUB,
    DOUBLE_TEST,
    DOUBLE_TESTS,
    DOUBLER_STUB,
    GOOD_ADD,
    GOOD_DOUBLE,
    Clock,
    ScriptedCaller,
    edit_result,
    local_runner,
    scripted,
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
        "test_add.py": ADD_TESTS,
        "test_double.py": DOUBLE_TESTS,
        "plugins.py": "import importlib\n\nimport adder\n\n\ndef load() -> object:\n"
                      "    return importlib.import_module('x')\n",
        "test_plugins.py": "import unittest\n\nimport plugins\n\n\nclass P(unittest.TestCase):"
                           "\n    def test_r3_loads(self) -> None:\n        plugins.load()\n",
    })


@pytest.mark.parametrize("changed", [["doubler.py"], ["adder.py"], ["notes.md"], ["web/a.ts"]])
def test_merge_gates_always_rerun_declared_checks(
    mathops: RepoIndex, changed: list[str],
) -> None:
    # A shell command can observe any file, so no declared check is ever skipped.
    assert mathops.affected([ADD_TEST, DOUBLE_TEST], changed) == [ADD_TEST, DOUBLE_TEST]


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
    assert rooted.deps["consumer.py"] == frozenset({"src/pkg/__init__.py"})
    assert rooted.deps["src/pkg/__init__.py"] == frozenset({"src/pkg/mod.py"})
    assert original.deps["consumer.py"] == frozenset() and index.parsed == count
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


def test_repo_map_lists_files_without_a_parser_by_path(orch_repo: Path, tmp_path: Path) -> None:
    revision = commit(orch_repo, {"db/schema.sql": "CREATE TABLE t (id INT);\n",
                                  "adder.py": ADDER}, "mixed")
    mapped = Index(orch_repo, tmp_path / "index.sqlite").at(revision).repo_map(100_000)
    assert "db/schema.sql" in mapped and "README.md" in mapped and "def add" in mapped


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
    assert sorted(index.at(first).files) == ["README.md", "adder.py", "doubler.py"]
    assert index.parsed == 2
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
    assert index.snapshot(first, "r1/plan") == 3 and again.snapshots() == [(first, "r1/plan", 3)]


def test_cochange_graph_comes_from_git_history(orch_repo: Path, tmp_path: Path) -> None:
    for round_ in range(3):
        commit(orch_repo, {"a.py": f"A = {round_}\n", "b.py": f"B = {round_}\n"}, "pair")
    commit(orch_repo, {"a.py": "A = 9\n"}, "alone")
    commit(orch_repo, {f"bulk{n}.py": "" for n in range(40)} | {"b.py": "B = 9\n"}, "bulk")
    coupling = Index(orch_repo, tmp_path / "i.sqlite").cochange("HEAD")
    # b.py's fourth change sits in a 41-file commit, which says nothing about coupling.
    assert [(c.a, c.b, c.together, c.weight) for c in coupling] == [("a.py", "b.py", 3, 1.0)]


FAILED_ADD = (f"FAIL (exit 1): {ADD_TEST}\n\n--- python3 -m unittest -q test_add\n"
              '  File "/x/.git/orch/r1/work/T-001-a1-abcdef/adder.py", line 3, in add\n'
              "TypeError: boom\n")


@pytest.mark.parametrize(
    ("other", "same"),
    [
        ('  File "/x/.git/orch/r1/work/T-001-a2-ffffff/adder.py", line 9, in add', True),
        ('  File "/x/.git/orch/r1/work/T-001-a1-abcdef/adder.py", line 3, in sub', False),
    ],
    ids=["moved-line-other-worktree", "other-function"],
)
def test_failure_signatures_ignore_worktree_paths_and_numbers(other: str, same: bool) -> None:
    changed = FAILED_ADD.replace(FAILED_ADD.splitlines()[3], other)
    assert (signature("test", FAILED_ADD) == signature("test", changed)) is same


def orchestrate(
    repo: Path, plan: dict[str, object], answers: dict[str, list[NativeResult]]
) -> tuple[Orchestrator, ScriptedCaller]:
    caller = scripted(plan, answers)
    return Orchestrator(repo, caller, local_runner, notify=lambda line: None, clock=Clock()), caller


def test_builder_sees_merged_dependency_code_and_gates_rerun_merged_checks(
    orch_repo: Path, math_plan: dict[str, object], unsandboxed: None,
) -> None:
    orchestrator, caller = orchestrate(
        orch_repo, math_plan, {"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]}
    )
    run = orchestrator.execute(orchestrator.plan("spec").id, jobs=1)
    prompt = [text for _, key, text in caller.calls if key == "dsh:T-002"][-1]
    gates = {e.task_id: e.data for e in orchestrator.state.events(run.id) if e.kind == "task.gate"}
    assert run.status == "done"
    # A building-on task reads its dependency's merged file whole (since dfdc631).
    context = prompt.split("REQUESTED READ-ONLY CONTEXT", 1)[1]
    assert "--- adder.py ---" in context and "return a + b" in context
    assert gates == {"T-001": {"tests": 1, "skipped": 0}, "T-002": {"tests": 2, "skipped": 0}}


def test_final_gate_bisects_a_regression_the_merge_gates_missed(
    orch_repo: Path, math_plan: dict[str, object], unsandboxed: None,
) -> None:
    def runner(execution: Path, scratch: Path, revision: str, names: list[str] | None,
               *, timeout: float = 25.0) -> tuple[bool, str]:
        hidden = "HIDDEN" in (execution / "doubler.py").read_text()
        if hidden and execution.name.startswith("final") and names and ADD_TEST in names:
            return False, f"FAIL (exit 1): {ADD_TEST}\n\n--- add\nAssertionError\n"
        return local_runner(execution, scratch, revision, names)

    sneaky = edit_result("doubler.py", "return add(a, a)  # HIDDEN coupling")
    orchestrator, _ = orchestrate(
        orch_repo, math_plan, {"dsh:T-001": [GOOD_ADD], "dsh:T-002": [sneaky, GOOD_DOUBLE]}
    )
    orchestrator.runner = runner
    run = orchestrator.execute(orchestrator.plan("spec").id, jobs=1)
    regression = [e.data for e in orchestrator.state.events(run.id) if e.kind == "run.regression"]
    assert (run.status, regression) == ("done", [{"culprit": "T-002", "reopened": ["T-002"]}])
    assert "HIDDEN" not in Workspace.git(orch_repo, "show", f"{run.branch}:doubler.py")


def test_final_gate_parks_instead_of_blaming_when_tests_interfere(
    orch_repo: Path, math_plan: dict[str, object], unsandboxed: None,
) -> None:
    def runner(execution: Path, scratch: Path, revision: str, names: list[str] | None,
               *, timeout: float = 25.0) -> tuple[bool, str]:
        together = names and {ADD_TEST, DOUBLE_TEST} <= set(names)
        if together and execution.name.startswith("final"):
            # one check leaks state into the other only when both run at the end
            return False, f"FAIL (exit 1): {ADD_TEST}\nPASS: {DOUBLE_TEST}\n"
        return local_runner(execution, scratch, revision, names)

    orchestrator, _ = orchestrate(
        orch_repo, math_plan, {"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]}
    )
    orchestrator.runner = runner
    run = orchestrator.execute(orchestrator.plan("spec").id, jobs=1)
    kinds = [e.kind for e in orchestrator.state.events(run.id)]
    assert (run.status, "run.regression" in kinds) == ("parked", False)
    assert run.message.startswith(f"{ADD_TEST} fails only together with other tests")


@pytest.mark.parametrize(
    ("report", "candidates", "expected"),
    [
        (f"FAIL (exit 1): {ADD_TEST}\nTIMEOUT: {DOUBLE_TEST}\n", None, [ADD_TEST, DOUBLE_TEST]),
        (f"PASS: {ADD_TEST}\nFAIL (exit 2): {DOUBLE_TEST}\n", None, [DOUBLE_TEST]),
        (f"FAIL (exit 1): {DOUBLE_TEST}\n", [ADD_TEST], []),
        ("FAIL: test_r1_x (m.A.test_r1_x)\nFAILED (failures=1)\n", None, []),
        (f"--- output\nFAIL (exit 1): {ADD_TEST} was mentioned\n", [ADD_TEST], []),
    ],
    ids=["fail-and-timeout", "pass-ignored", "outside-candidates", "framework-output",
         "not-a-report-line"],
)
def test_failures_are_attributed_only_from_check_report_lines(
    report: str, candidates: list[str] | None, expected: list[str],
) -> None:
    assert failed_tests(report, candidates) == expected


@pytest.mark.parametrize("second,expected", [
    (f"FAIL (exit 1): {ADD_TEST}\nPASS: {DOUBLE_TEST}\n", [ADD_TEST]),
    (f"PASS: {ADD_TEST}\nFAIL (exit 1): {DOUBLE_TEST}\n", []),
    (f"PASS: {ADD_TEST}\nPASS: {DOUBLE_TEST}\n", []),
    ("timed out after 25s", []),
])
def test_candidate_agreement_requires_shared_failure_evidence(
    second: str, expected: list[str],
) -> None:
    first = f"FAIL (exit 1): {ADD_TEST}\nPASS: {DOUBLE_TEST}\n"
    tests = [ADD_TEST, DOUBLE_TEST]
    assert common_failures([first, second], tests) == expected
    assert common_failures([first], tests) == []


def test_index_and_trace_commands_use_the_symbol_index(
    orch_repo: Path, math_plan: dict[str, object], capsys: pytest.CaptureFixture[str],
    unsandboxed: None,
) -> None:
    orchestrator, _ = orchestrate(
        orch_repo, math_plan, {"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]}
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
        "  used by test_add.py::<module>",
        "  used by test_add.py::AddTest.test_r1_adds",
    ]
    assert traced[0] == f"doubler.py:8 <- commit {task_commit}"
    assert traced[-3:] == [
        "  function double lines 6-8:",
        f"    {plan_commit}   2 line(s)  (not an orch task commit)",
        f"    {task_commit}   1 line(s)  T-002",
    ]


def test_tree_sitter_indexing_survives_a_generated_go_test_file() -> None:
    """tree-sitter 0.26.0 corrupted the heap on this model-written Go test file and orch died
    with SIGSEGV (polyglot-ledger N9-N11); the pinned version must index it repeatedly."""
    from dataclasses import asdict

    from orchestragent.orch_index import grammar, index_file

    if grammar("go") is None:
        pytest.skip("tree-sitter Go grammar not installed")
    source = (Path(__file__).parent / "data" / "generated_go_test.go.txt").read_bytes()
    for _ in range(200):
        found = index_file("api/ledger/parse_t001_test.go", source)
        asdict(found)
    assert found.backend == "tree-sitter" and found.symbols
