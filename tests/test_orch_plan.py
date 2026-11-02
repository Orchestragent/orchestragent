import ast
import copy
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from orchestragent.codeindex import signatures
from orchestragent.orch_build import (
    BUILDER_INSTRUCTIONS,
    MAX_FILE_BYTES,
    ContextTooLarge,
    EditError,
    apply_edits,
    commit_message,
    context_pack,
    contract_hash,
    diagnose_failure,
    implementation_errors,
    interface_errors,
    parse_trailers,
    render_files,
    signature,
    substitute,
)
from orchestragent.orch_excerpt import excerpt
from orchestragent.orch_index import RepoIndex, is_stub
from orchestragent.orch_plan import (
    chain_depths,
    lint,
    tasks_from,
    unittest_ids,
    unittest_shape_errors,
)
from orchestragent.orch_run import RunContext
from orchestragent.orch_state import OrchTask
from tests.conftest import ADDER_STUB, MATH_TESTS

Plan = dict[str, object]
ContextArgs = tuple[OrchTask, dict[str, str], str, dict[str, str], dict[str, str], dict[str, str]]


@pytest.mark.parametrize("graph", [{"A": ["missing"]}, {"A": ["B"], "B": ["A"]}])
def test_chain_measurement_rejects_invalid_graph(graph: dict[str, list[str]]) -> None:
    with pytest.raises(ValueError):
        chain_depths(graph)


def test_deep_dependency_chains_do_not_need_recursive_graph_walks() -> None:
    graph = {str(i): [str(i - 1)] if i else [] for i in range(1500)}
    graph["independent"] = []
    depths = chain_depths(graph)
    assert (depths["0"], depths["1499"], depths["independent"]) == (1500, 1, 1)


@pytest.mark.parametrize(("body", "stub"), [
    ("raise NotImplementedError", True),
    ('"doc"\n    raise builtins.NotImplementedError("todo")', True),
    ('raise ValueError("NotImplementedError is not supported")', False),
    ('raise CustomNotImplementedError', False),
    ('return NotImplemented', False),
])
def test_stub_recognition_uses_exception_ast_not_substrings(body: str, stub: bool) -> None:
    assert is_stub(ast.parse(f"def f():\n    {body}\n").body[0]) == stub


@pytest.mark.parametrize(("before", "current", "required", "problem"), [
    (ADDER_STUB, ADDER_STUB.replace("raise NotImplementedError", "return a + b") + ADDER_STUB,
     ADDER_STUB, "duplicate definitions"),
    (ADDER_STUB, ADDER_STUB, ADDER_STUB, "unimplemented concrete body"),
    ("", "async def f():\n    raise NotImplementedError\n",
     "async def f():\n    raise NotImplementedError\n", "unimplemented concrete body"),
    ("", "class Base:\n def hook(self): raise NotImplementedError\n"
     "class Concrete(Base):\n def hook(self): return 1\n", "", ""),
    ("", "class C:\n def f(self): return 1\n def f(self): return 2\n", "", "C.f: duplicate"),
    ("", "class C: pass\nclass C: pass\n", "", "C: duplicate"),
    ("", "return 1\n", "", "invalid Python"),
    ("def unfinished(", "def repaired(): return 1\n", "", ""),
    ("", "def f():\n nonlocal absent\n", "", "invalid Python"),
    (ADDER_STUB, "from typing import overload\n" +
     ADDER_STUB.replace("def add", "@overload\ndef add"),
     ADDER_STUB, "became declarative"),
    (ADDER_STUB, ADDER_STUB + "\n# unrelated change\n", "", ""),
    (ADDER_STUB, ADDER_STUB.replace("Return a + b.", "Updated extension documentation."), "", ""),
    ("", 'def f():\n raise ValueError("NotImplementedError")\n', "", ""),
    ("", "from abc import abstractmethod as am\nclass C:\n @am\n def f(self):\n"
     "  raise NotImplementedError\n", "", ""),
    ("", "from typing import Protocol as P\nclass C(P):\n def f(self):\n"
     "  raise NotImplementedError\n", "", ""),
    ("", "from typing import overload as ov\n@ov\ndef f(x: int): ...\n"
     "@ov\ndef f(x: str): ...\ndef f(x): return x\n", "", ""),
    ("", "class C:\n @property\n def x(self): return self._x\n @x.setter\n"
     " def x(self, value): self._x = value\n", "", ""),
    ("def f(): return 1\ndef f(): return 2\n", "def f(): return 1\ndef f(): return 2\n",
     "", ""),
    ("", "if condition:\n def f(): return 1\nelse:\n def f(): return 2\n", "", ""),
])
def test_candidate_structure_compares_pinned_code_and_preserves_declarations(
    before: str, current: str, required: str, problem: str,
) -> None:
    errors = implementation_errors(before, current, required)
    assert (any(problem in item for item in errors) if problem else not errors)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_partial_owned_edits_preserve_large_unseen_source_bytes(
    tmp_path: Path, large_owned_source: str, newline: str,
) -> None:
    source = large_owned_source.replace("\n", newline)
    target = tmp_path / "adder.py"
    target.write_bytes(source.encode())
    view = excerpt("adder.py", source, {"add"})
    apply_edits(tmp_path, ["adder.py"], [
        {"path": "adder.py", "search": "return a - b + helper()",
         "replace": "return a + b + helper()"},
    ], visible={"adder.py": view})
    assert target.read_bytes() == source.replace("a - b", "a + b").encode()


@pytest.mark.parametrize("search", ["", "def unrelated():"])
def test_partial_owned_views_cannot_replace_the_file_or_edit_unseen_code(
    tmp_path: Path, large_owned_source: str, search: str,
) -> None:
    target = tmp_path / "adder.py"
    target.write_bytes(large_owned_source.encode())
    with pytest.raises(EditError, match="NEED_FILES"):
        apply_edits(tmp_path, ["adder.py"], [
            {"path": "adder.py", "search": search, "replace": "def add(a,b): return a+b\n"},
        ], visible={"adder.py": excerpt("adder.py", large_owned_source, {"add"})})
    assert target.read_bytes() == large_owned_source.encode()


@pytest.mark.parametrize("existing", [False, True])
def test_file_growth_budget_applies_to_the_whole_edit_batch_before_writing(
    tmp_path: Path, large_owned_source: str, existing: bool,
) -> None:
    target = tmp_path / "adder.py"
    source = large_owned_source if existing else ""
    if existing:
        target.write_bytes(source.encode())
    with pytest.raises(EditError, match="growth limit"):
        apply_edits(tmp_path, ["adder.py"], [
            {"path": "adder.py", "search": "", "replace": source + "#" * (MAX_FILE_BYTES + 1)},
        ])
    assert target.exists() == existing
    if existing:
        assert target.read_bytes() == source.encode()


@pytest.mark.parametrize("invalid", ["return 1", "break", "def f():\n nonlocal missing"])
def test_edits_compile_all_files_before_writing_any(tmp_path: Path, invalid: str) -> None:
    (tmp_path / "a.py").write_text("original = 1\n")
    with pytest.raises(EditError, match="syntax error"):
        apply_edits(tmp_path, ["a.py", "b.py"], [
            {"path": "a.py", "search": "", "replace": "changed = 2\n"},
            {"path": "b.py", "search": "", "replace": invalid},
        ])
    assert (tmp_path / "a.py").read_text() == "original = 1\n"
    assert not (tmp_path / "b.py").exists()


@pytest.mark.parametrize(("kind", "origin", "exception", "dispute"), [
    ("ERROR", "/work/test_mathops.py", "NameError", True),
    ("ERROR", "/repo/.git/orch/r1/work/task-abc/test_mathops.py", "UnboundLocalError", True),
    ("ERROR", "/work/adder.py", "NameError", False),
    ("ERROR", "/usr/lib/python3/unittest/mock.py", "NameError", False),
    ("ERROR", "/work/test_mathops.py", "AttributeError", False),
    ("ERROR", "/work/test_mathops.py", "ImportError", False),
    ("FAIL", "/work/test_mathops.py", "AssertionError", False),
])
def test_fixture_diagnosis_requires_name_error_in_innermost_frozen_frame(
    kind: str, origin: str, exception: str, dispute: bool,
) -> None:
    report = (f"{kind}: test_r1_adds (test_mathops.AddTest.test_r1_adds)\n"
              'Traceback (most recent call last):\n'
              '  File "/work/test_mathops.py", line 1, in test_r1_adds\n'
              f'  File "{origin}", line 2, in broken\n{exception}: undefined\n')
    assert (diagnose_failure(report, {"test_mathops.py"}, {})[0] == "fixture") == dispute


@pytest.mark.parametrize(("source", "origin", "category"), [
    ("def hook():\n raise NotImplementedError\n", "/work/owned.py", "structure"),
    ("async def hook():\n raise builtins.NotImplementedError()\n", "/work/owned.py", "structure"),
    ("def hook():\n if unavailable: raise NotImplementedError\n", "/work/owned.py", "test"),
    ("def hook():\n raise NotImplementedError\n", "/work/dependency.py", "test"),
    ("def hook():\n raise NotImplementedError\n", "/usr/lib/library.py", "test"),
])
def test_runtime_diagnosis_requires_executed_owned_stub(
    source: str, origin: str, category: str,
) -> None:
    report = ('ERROR: test_hook (test_hooks.Hooks.test_hook)\n'
              'Traceback (most recent call last):\n'
              '  File "/work/test_hooks.py", line 1, in test_hook\n'
              f'  File "{origin}", line 2, in hook\nNotImplementedError\n')
    assert diagnose_failure(report, {"test_hooks.py"}, {"owned.py": source})[0] == category


def task_entry(plan: Plan, index: int) -> dict[str, object]:
    return cast(list[dict[str, object]], plan["tasks"])[index]


def file_entry(plan: Plan, index: int) -> dict[str, str]:
    return cast(list[dict[str, str]], plan["files"])[index]


def test_canonical_plan_passes_lint(math_plan: Plan) -> None:
    assert lint(math_plan) == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda p: task_entry(p, 1).update(files=["adder.py"]), "adder.py already owned by T-001"),
        (
            lambda p: file_entry(p, 2).update(content=MATH_TESTS.replace("test_r1_", "test_")),
            "lacks a test_r<N>_ requirement marker",
        ),
        (
            lambda p: cast(list[object], p["requirements"]).append({"id": "R3", "text": "x"}),
            "R3: no test proves it",
        ),
        (lambda p: task_entry(p, 0).update(depends_on=["T-002"]), "dependency cycle"),
        (lambda p: task_entry(p, 1).update(depends_on=["T-009"]), "unknown dependency T-009"),
        (
            lambda p: task_entry(p, 0).update(files=["adder.py", "test_mathops.py"]),
            "may not own 'test_mathops.py'",
        ),
        (
            lambda p: task_entry(p, 0).update(
                tests=[
                    "test_mathops.AddTest.test_r1_adds",
                    "test_mathops.DoubleTest.test_r2_doubles",
                ]
            ),
            "does not prove one of its reqs",
        ),
        (lambda p: task_entry(p, 1).update(files=["other.py"]), "stub doubler.py has no owning"),
        (lambda p: file_entry(p, 0).update(content="def add(:\n"), "adder.py does not parse"),
        (lambda p: task_entry(p, 0).update(contract=""), "goal, contract, reqs"),
        (lambda p: task_entry(p, 1).update(tests=["test_mathops.No.test_r2_x"]), "not in a frozen"),
        (lambda p: p.update(files=[]), "files: Expected 1–2000 generated files"),
        (
            lambda p: file_entry(p, 2).update(path="unit-tests/test_mathops.py"),
            "unit-tests/test_mathops.py is not importable",
        ),
        (
            lambda p: (file_entry(p, 0).update(path="string.py"),
                       task_entry(p, 0).update(files=["string.py"])),
            "string.py would shadow the standard library module 'string'",
        ),
    ],
)
def test_lint_rejects_unbuildable_plans(
    math_plan: Plan, mutate: Callable[[Plan], None], expected: str
) -> None:
    plan = copy.deepcopy(math_plan)
    mutate(plan)
    assert any(expected in error for error in lint(plan)), lint(plan)


def test_unowned_files_without_stub_bodies_are_allowed(math_plan: Plan) -> None:
    files = cast(list[dict[str, str]], math_plan["files"])
    files.append({"path": "pkg/__init__.py", "content": "VERSION = 1\n"})
    assert lint(math_plan) == []


def test_lint_forbids_overwriting_repository_files(math_plan: Plan) -> None:
    assert lint(math_plan, frozenset({"adder.py", "README.md"})) == [
        "files: adder.py already exists; the plan may only add files"
    ]


def test_tasks_and_test_ids_come_from_the_plan(math_plan: Plan) -> None:
    tasks = tasks_from(math_plan)
    assert ([t.id for t in tasks], tasks[1].depends_on) == (["T-001", "T-002"], ["T-001"])
    assert unittest_ids("test_mathops.py", MATH_TESTS) == [
        "test_mathops.AddTest.test_r1_adds",
        "test_mathops.DoubleTest.test_r2_doubles",
    ]


MIXIN = ("import unittest\nclass _Shared:\n    def setUp(self):\n        self.items = []\n"
         "    def test_r1_a(self):\n        pass\n    def test_r1_b(self):\n        pass\n"
         "class HeapCase(_Shared, unittest.TestCase):\n    pass\n"
         "class SortedCase(_Shared, unittest.TestCase):\n    def test_r1_c(self):\n        pass\n")


@pytest.mark.parametrize(("source", "ids", "problem"), [
    (MIXIN, ["HeapCase.test_r1_a", "HeapCase.test_r1_b", "SortedCase.test_r1_c",
             "SortedCase.test_r1_a", "SortedCase.test_r1_b"], ""),
    ("from unittest import TestCase as Case\nclass T(Case):\n    def test_r1_a(self):\n"
     "        pass\n", ["T.test_r1_a"], ""),
    ("import unittest as ut\nclass Base(ut.TestCase):\n    def test_r1_a(self):\n        pass\n"
     "class Child(Base):\n    def test_r1_b(self):\n        pass\n",
     ["Base.test_r1_a", "Child.test_r1_b", "Child.test_r1_a"], ""),
    ("import unittest\nclass M:\n    def test_r1_a(self):\n        pass\n"
     "class T(M, unittest.TestCase):\n    def test_r1_a(self):\n        pass\n",
     ["T.test_r1_a"], ""),
    ("import unittest\nclass Orphan:\n    def test_r1_a(self):\n        pass\n", [],
     "class Orphan defines tests but is not a unittest.TestCase"),
    ("from helpers import Base\nclass T(Base):\n    def test_r1_a(self):\n        pass\n",
     ["T.test_r1_a"], "class T: base Base is not unittest.TestCase"),
    ("import unittest\nclass T(unittest.TestCase):\n    def test_r1_a(self):\n        pass\n"
     "    test_r1_b = test_r1_a\n", ["T.test_r1_a"], "class T: test_r1_b is assigned"),
    ("class Helper:\n    def build(self):\n        pass\n", [], ""),
    ("import io\nfrom boltons.cacheutils import LRU\nclass BoomError(Exception):\n    pass\n"
     "class Counting(LRU):\n    def get(self, k):\n        return 1\n"
     "class Tracking(io.BytesIO):\n    pass\n", [], ""),
    ("from tests.base import SharedTests\nclass T(SharedTests):\n    pass\n",
     [], "class T: base SharedTests is not unittest.TestCase"),
], ids=["mixin", "alias-import", "chained-case", "override", "orphan-mixin", "external-base",
        "assigned-test", "plain-helper", "helper-subclasses", "external-test-base"])
def test_unittest_ids_list_only_runnable_cases_and_shapes_name_what_cannot_be_listed(
    source: str, ids: list[str], problem: str,
) -> None:
    assert unittest_ids("test_shape.py", source) == [f"test_shape.{name}" for name in ids]
    problems = unittest_shape_errors(source)
    assert (any(problem in item for item in problems) if problem else problems == [])


def test_plan_lint_rejects_test_shapes_that_would_not_run(math_plan: Plan) -> None:
    files = cast(list[dict[str, str]], math_plan["files"])
    files[2]["content"] = files[2]["content"].replace(
        "(unittest.TestCase)", "", 1)  # the first class loses its TestCase base
    errors = lint(math_plan)
    assert any("defines tests but is not a unittest.TestCase" in error for error in errors)
    assert any("no test proves it" in error or "is not in a frozen test file" in error
               for error in errors)


@pytest.mark.parametrize(("edges", "valid"), [
    ([], True), (["T-001"], True), (["T-001", "T-001"], False),
    (["T-002"], False), (["T-999"], False), ("T-001", False), ([None], False),
])
def test_interface_dependencies_are_explicit_validated_and_round_trip(
    math_plan: Plan, edges: object, valid: bool,
) -> None:
    original = tasks_from(math_plan)[1]
    task_entry(math_plan, 1)["interface_depends_on"] = edges
    assert (not lint(math_plan)) == valid
    if valid:
        parsed = tasks_from(math_plan)[1]
        assert parsed.interface_depends_on == edges
        assert parsed.entry().get("interface_depends_on", []) == edges
        assert (contract_hash(parsed) == contract_hash(original)) == (not edges)


@pytest.mark.parametrize(("change", "changed"), [
    ("contract", True), ("requirement", True), ("interface", True), ("baseline", True),
    ("tests", False), ("implementation", False),
])
def test_dependency_contract_versions_track_promises_not_build_progress(
    math_plan: Plan, change: str, changed: bool,
) -> None:
    task_entry(math_plan, 1)["interface_depends_on"] = ["T-001"]
    context = RunContext.of_plan(math_plan)
    task = context.tasks["T-002"]
    before = context.dependency_contracts(task, "base")
    if change == "contract":
        context.tasks["T-001"].contract += " Explicit overflow behavior."
    elif change == "requirement":
        context.requirements["R1"] += " Handle overflow."
    elif change == "interface":
        context.stubs["adder.py"] += "# Published invariant changed\n"
    elif change == "tests":
        context.files["test_mathops.py"] += "# corrected fixture\n"
    elif change == "implementation":
        context.tasks["T-001"].status = "DONE"
        context.tasks["T-001"].commit = "a" * 40
    assert (context.dependency_contracts(task, "next" if change == "baseline" else "base")
            != before) == changed


def test_signatures_and_repo_map_are_signature_level() -> None:
    source = "class A(B):\n    def m(self, x: int = 1) -> str:\n        return ''\n" + ADDER_STUB
    assert signatures(source) == [
        "class A(B)",
        "def A.m(self, x: int=1) -> str",
        "def add(a: int, b: int) -> int",
    ]
    sources = {"b.py": ADDER_STUB, "a.md": "x", "a.py": "def f(): pass\n"}
    assert RepoIndex.from_sources(sources).repo_map(1000) == (
        "a.py\n  def f()\nb.py\n  def add(a: int, b: int) -> int\n"
    )
    assert RepoIndex.from_sources({"a.py": ADDER_STUB}).repo_map(10) == (
        "(repo map truncated: 1 of 1 files omitted)\n"
    )


@pytest.mark.parametrize(
    ("edits", "expected"),
    [
        ([{"path": "a.py", "search": "1", "replace": "2"}], {"a.py": "x = 2\n"}),
        ([{"path": "b.py", "search": "", "replace": "y = 1\n"}], {"b.py": "y = 1\n"}),
        ([{"path": "c.py", "search": "", "replace": "z\n"}], "edit outside owned files: c.py"),
        ([{"path": "a.py", "search": "nope", "replace": "q"}], "found 0 times"),
        ([{"path": "a.py", "search": "x", "replace": "x = ("}], "syntax error in a.py"),
        ([{"path": "a.py"}], "malformed edit"),
        ([], "no edits returned"),
    ],
)
def test_apply_edits_only_writes_owned_files_atomically(
    tmp_path: Path, edits: list[dict[str, str]], expected: dict[str, str] | str
) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    if isinstance(expected, str):
        with pytest.raises(EditError, match=expected):
            apply_edits(tmp_path, ["a.py", "b.py"], edits)
        assert (tmp_path / "a.py").read_text() == "x = 1\n"
        return
    assert apply_edits(tmp_path, ["a.py", "b.py"], edits) == sorted(expected)
    assert {path: (tmp_path / path).read_text() for path in expected} == expected


@pytest.mark.parametrize(
    ("content", "search", "replace", "expected"),
    [
        ("a = 1\nb = 2\n", "b = 2", "b = 3", "a = 1\nb = 3\n"),
        ("def f():\n    x = 1   \n    return x\n", "    x = 1\n    return x", "    return 1",
         "def f():\n    return 1\n"),
        ("def f():\n    a = 1\n    b = 2\n", "a = 1\nb = 2", "a = 3\n\nb = 4",
         "def f():\n    a = 3\n\n    b = 4\n"),
        ("x = 1\ny = 2\nz = 3\n", "y = 2\n", "", "x = 1\nz = 3\n"),
        ("if x:\n    y = 1\nelse:\n    y = 1\n", "y = 1", "y = 2", "found 2 times"),
        ("if x:\n    y = 1  \nif z:\n    y = 1 \n", "y = 1\n", "y = 2", "found 2 times"),
        ("a = 1\n", "b = 1", "c", "found 0 times"),
        ("a = 1\n", "\n\n", "c", "found 0 times"),
    ],
)
def test_substitute_tolerates_whitespace_but_never_guesses(
    content: str, search: str, replace: str, expected: str
) -> None:
    if expected.startswith("found"):
        with pytest.raises(EditError, match=expected):
            substitute(content, search, replace, "f.py")
    else:
        assert substitute(content, search, replace, "f.py") == expected


def test_failure_signatures_ignore_line_numbers_but_not_tests() -> None:
    first = (
        "FAIL: test_r1_adds (m.AddTest.test_r1_adds)\n"
        '  File "a.py", line 3\nAssertionError: 1 != 5'
    )
    moved = first.replace("line 3", "line 9").replace("1 != 5", "2 != 5")
    other = first.replace("test_r1_adds", "test_r1_other")
    assert signature("test", first) == signature("test", moved) != signature("test", other)
    assert signature("edit", "x") != signature("test", "x")


def test_interface_check_allows_bodies_but_not_signatures() -> None:
    body = ADDER_STUB.replace("raise NotImplementedError", "return a + b")
    renamed = ADDER_STUB.replace("b: int", "c: int")
    assert interface_errors(ADDER_STUB, body) == []
    assert interface_errors(ADDER_STUB, renamed) == [
        "interface changed: def add(a: int, b: int) -> int"
    ]


@pytest.fixture
def add_task(math_plan: Plan) -> OrchTask:
    return tasks_from(math_plan)[0]


@pytest.fixture
def context_args(add_task: OrchTask) -> ContextArgs:
    return (add_task, {"R1": "add"}, "MAP" * 50, {"adder.py": ADDER_STUB},
            {"adder.py": ADDER_STUB}, {"test_mathops.py": MATH_TESTS})


def test_context_pack_orders_layers_and_sheds_shared_context(context_args: ContextArgs) -> None:
    full = context_pack(*context_args, 10_000)
    retry = context_pack(*context_args, 10_000, ("DIFF", "ERROR"))
    lean = context_pack(*context_args, len(full.text.encode()) - 100)
    assert full.text.startswith(BUILDER_INSTRUCTIONS) and "MAPMAP" in full.shared
    assert (retry.shared, retry.prefix_hash) == (full.shared, full.prefix_hash)
    assert "PREVIOUS ATTEMPT FAILED" in retry.retry and not full.retry
    assert "(omitted)" in lean.shared


def test_requested_dependency_is_not_repeated_in_optional_code(context_args: ContextArgs) -> None:
    source = "def helper():\n    return 'unique dependency body'\n"
    pack = context_pack(*context_args, 20_000, dependencies={"helper.py": source},
                        requested={"helper.py::helper": source})
    assert pack.text.count("unique dependency body") == 1
    with pytest.raises(ContextTooLarge):
        context_pack(*context_args[:4], {"adder.py": "x" * 5000}, {}, 2000)


@pytest.mark.parametrize(("selector", "body", "retained"), [
    ("helper.py", "FULL FILE", False),
    ("helper.py::Queue", "CLASS BODY", False),
    ("helper.py::Queue.pop", "METHOD BODY", False),
    ("helper.py::Queue.peek", "SIBLING BODY", True),
    ("other.py::Queue.pop", "POP_IMPLEMENTATION", True),
    ("helper.py", "# PARTIAL CONTEXT: full file exceeds request budget", True),
])
def test_symbol_dependencies_are_deduplicated_without_losing_siblings_or_catalog_bodies(
    context_args: ContextArgs, selector: str, body: str, retained: bool,
) -> None:
    pack = context_pack(*context_args, 20_000,
                        dependencies={"helper.py::Queue.pop": "POP_IMPLEMENTATION"},
                        requested={selector: body})
    assert ("POP_IMPLEMENTATION" in pack.task) == retained
    assert body in pack.task


def test_owned_source_covers_requested_and_inferred_symbol_reads(context_args: ContextArgs) -> None:
    pack = context_pack(*context_args, 20_000,
                        dependencies={"adder.py::add": "DUPLICATE_DEPENDENCY"},
                        requested={"adder.py::add": "DUPLICATE_REQUEST"})
    assert "DUPLICATE" not in pack.task


def test_task_interfaces_do_not_change_shared_prefix_or_disappear_under_pressure(
    context_args: ContextArgs,
) -> None:
    first = context_pack(*context_args, 10_000)
    other_stubs = {"adder.py": "def different():\n    raise NotImplementedError\n"}
    other = context_pack(*context_args[:3], other_stubs, *context_args[4:], 10_000)
    assert first.shared == other.shared and first.prefix_hash == other.prefix_hash
    assert "def different" in other.task and "def different" not in other.shared
    with pytest.raises(ContextTooLarge, match="interfaces"):
        context_pack(*context_args[:3], {"large.py": "# contract\n" * 500},
                     *context_args[4:], 2500)


@pytest.mark.parametrize("spec", ["Keep the original behaviour.", "原始需求不得被测试覆盖。"])
def test_project_prefix_survives_dependency_changes_but_not_spec_changes(
    context_args: ContextArgs, spec: str,
) -> None:
    first = context_pack(*context_args, 20_000, original_spec=spec)
    changed = context_pack(*context_args[:3], {**context_args[3], "dep.py": "VALUE = 2\n"},
                           *context_args[4:], 20_000, original_spec=spec)
    revised = context_pack(*context_args, 20_000, original_spec=spec + " Updated.")
    assert first.shared == changed.shared and first.prefix_hash == changed.prefix_hash
    assert "VALUE = 2" in changed.task and "VALUE = 2" not in changed.shared
    assert spec in first.shared and first.prefix_hash != revised.prefix_hash
    with pytest.raises(ContextTooLarge):
        context_pack(*context_args, 20_000, original_spec=spec * 20_000)


@pytest.mark.parametrize(("task_index", "expected"), [
    (0, {"adder.py", "shared.py"}),
    (1, {"adder.py", "doubler.py", "shared.py"}),
])
def test_interfaces_follow_task_dependencies_and_imports_not_the_whole_project(
    math_plan: Plan, task_index: int, expected: set[str],
) -> None:
    files = cast(list[dict[str, str]], math_plan["files"])
    files[0]["content"] = "from shared import LIMIT\n" + files[0]["content"]
    files.extend([
        {"path": "shared.py", "content": "LIMIT = 10\n"},
        {"path": "unrelated.py", "content": "# unrelated interface\n" * 2000},
    ])
    context = RunContext.of_plan(math_plan)
    selected = context.interfaces_for(tasks_from(math_plan)[task_index],
                                      RepoIndex.from_sources(context.stubs))
    assert set(selected) == expected
    assert all(source == context.stubs[path] for path, source in selected.items())


@pytest.mark.parametrize("value", ["plain text", "边界 café 📦"])
@pytest.mark.parametrize("retry", [("", ""), ("前差分", "échec 📦")])
def test_context_pack_retains_whole_dependencies_in_priority_order(
    context_args: ContextArgs, value: str, retry: tuple[str, str],
) -> None:
    small = f"def useful():\n    return {value!r}\n"
    first = {"z.py::useful": small}
    dependencies = {"oversize.py::big": "x" * 5000, **first, "a.py::later": small}
    expected = context_pack(*context_args, 20_000, retry, dependencies=first)
    budget = len(expected.text.encode())
    actual = context_pack(*context_args, budget, retry, dependencies=dependencies)
    base = context_pack(*context_args, 20_000, retry)
    assert actual == expected
    assert len(actual.text.encode()) == budget
    assert actual.shared == base.shared and actual.prefix_hash == base.prefix_hash
    assert actual.retry == base.retry
    assert render_files("OWNED FILES (current content; missing files are new)",
                        context_args[4]) in actual.task
    assert render_files("FROZEN TESTS", context_args[5]) in actual.task
    assert "oversize.py::big" not in actual.task and "a.py::later" not in actual.task
    assert small in actual.task


@pytest.mark.parametrize("retry", [("", ""), ("DIFF", "ERROR")])
def test_context_pack_ample_budget_preserves_full_rendering(
    context_args: ContextArgs, retry: tuple[str, str],
) -> None:
    dependencies = {"z.py::last": "LAST", "a.py::first": "FIRST"}
    base = context_pack(*context_args, 20_000, retry)
    full = context_pack(*context_args, 20_000, retry, dependencies=dependencies)
    assert full.text == (base.shared + base.task
                         + render_files("CODE YOU CAN CALL (merged, read-only)", dependencies)
                         + base.retry)
    assert full.prefix_hash == base.prefix_hash


@pytest.mark.parametrize("dependencies", [None, {}])
@pytest.mark.parametrize("retry", [("", ""), ("DIFF", "ERROR")])
def test_context_pack_without_dependencies_preserves_exact_budget(
    context_args: ContextArgs, dependencies: dict[str, str] | None, retry: tuple[str, str],
) -> None:
    full = context_pack(*context_args, 20_000, retry, dependencies=dependencies)
    exact = context_pack(*context_args, len(full.text.encode()), retry, dependencies=dependencies)
    assert exact == full
    assert "CODE YOU CAN CALL" not in exact.task
    assert render_files("FROZEN TESTS", context_args[5]) in exact.task


def test_commit_trailers_round_trip(add_task: OrchTask) -> None:
    message = commit_message("r1", "plan123", add_task, "a2", "dsh/deepseek-flash")
    assert message.startswith("orch T-001: implement add\n\n")
    assert parse_trailers(message) == {
        "Orch-Run": "r1",
        "Orch-Task": "T-001",
        "Orch-Req": "R1",
        "Orch-Attempt": "a2",
        "Orch-Plan": "plan123",
        "Orch-Contract": parse_trailers(message)["Orch-Contract"],
        "Orch-Model": "dsh/deepseek-flash",
        "Orch-Rules": "none",
    }
