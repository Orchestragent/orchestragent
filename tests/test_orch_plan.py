import copy
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from orchestragent import orch_generic
from orchestragent.orch_build import (
    BUILDER_INSTRUCTIONS,
    MAX_FILE_BYTES,
    ContextTooLarge,
    EditError,
    apply_edits,
    commit_message,
    context_pack,
    contract_hash,
    parse_trailers,
    render_files,
    signature,
    substitute,
)
from orchestragent.orch_plan import chain_depths, dependency_errors, tasks_from
from orchestragent.orch_run import RunContext
from orchestragent.orch_state import OrchTask
from tests.conftest import ADD_TEST, ADD_TESTS, ADDER_STUB, DOUBLE_TEST, DOUBLE_TESTS

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


def test_a_failing_edit_writes_no_file_of_the_batch(tmp_path: Path) -> None:
    (tmp_path / "a.go").write_text("package a\n")
    with pytest.raises(EditError, match="found 0 times"):
        apply_edits(tmp_path, ["a.go", "b.ts"], [
            {"path": "a.go", "search": "", "replace": "package changed\n"},
            {"path": "b.ts", "search": "", "replace": "export const b = 1;\n"},
            {"path": "a.go", "search": "missing", "replace": "x"},
        ])
    assert (tmp_path / "a.go").read_text() == "package a\n"
    assert not (tmp_path / "b.ts").exists()


def task_entry(plan: Plan, index: int) -> dict[str, object]:
    return cast(list[dict[str, object]], plan["tasks"])[index]


def file_entry(plan: Plan, index: int) -> dict[str, str]:
    return cast(list[dict[str, str]], plan["files"])[index]


@pytest.fixture
def frozen_plan(math_plan: Plan) -> Plan:
    """The boundary plus each task's frozen test file and declared check."""
    return orch_generic.assemble(math_plan, [
        ("T-001", [{"path": "test_add.py", "content": ADD_TESTS}], ADD_TEST),
        ("T-002", [{"path": "test_double.py", "content": DOUBLE_TESTS}], DOUBLE_TEST),
    ], frozenset())


def test_canonical_boundary_and_frozen_plan_pass_structure_checks(
    math_plan: Plan, frozen_plan: Plan,
) -> None:
    assert orch_generic.boundary_errors(math_plan, frozenset()) == []
    assert orch_generic.plan_errors(frozen_plan) == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda p: task_entry(p, 1).update(files=["adder.py"]),
         "adder.py: written by both T-001 and T-002"),
        (lambda p: cast(list[object], p["requirements"]).append({"id": "R3", "text": "x"}),
         "requirements without an accepting task: R3"),
        (lambda p: task_entry(p, 0).update(depends_on=["T-002"]), "depends_on must be acyclic"),
        (lambda p: task_entry(p, 1).update(depends_on=["T-009"]), "depends on unknown task T-009"),
        (lambda p: task_entry(p, 1).update(files=["other.py"]),
         "stub files without a writer task: doubler.py"),
        (lambda p: task_entry(p, 0).update(tests=[ADD_TEST]), "must be [] before test design"),
        (lambda p: task_entry(p, 1).update(reqs=["R1"]), "R1: accepted by both"),
        (lambda p: (file_entry(p, 0).update(path="../adder.py"),
                    task_entry(p, 0).update(files=["../adder.py"])), "not a safe relative path"),
        (lambda p: task_entry(p, 0).pop("contract"), "each task has exactly"),
    ],
)
def test_boundary_checks_reject_unbuildable_plans(
    math_plan: Plan, mutate: Callable[[Plan], None], expected: str
) -> None:
    plan = copy.deepcopy(math_plan)
    mutate(plan)
    errors = orch_generic.boundary_errors(plan, frozenset())
    assert any(expected in error for error in errors), errors


def test_boundary_may_not_overwrite_repository_files_unless_edits_are_allowed(
    math_plan: Plan,
) -> None:
    assert orch_generic.boundary_errors(math_plan, frozenset({"adder.py", "README.md"})) == [
        "adder.py: already exists; describe changes in a task contract"
    ]
    assert orch_generic.boundary_errors(math_plan, frozenset({"adder.py"}),
                                        existing_edits=True) == []


def test_tasks_and_checks_come_from_the_frozen_plan(frozen_plan: Plan) -> None:
    tasks = tasks_from(frozen_plan)
    assert ([t.id for t in tasks], tasks[1].depends_on) == (["T-001", "T-002"], ["T-001"])
    assert [t.tests for t in tasks] == [[ADD_TEST], [DOUBLE_TEST]]
    context = RunContext.of_plan(frozen_plan)
    assert set(context.stubs) == {"adder.py", "doubler.py"}
    assert context.tests_for(tasks[0]) == {"test_add.py": ADD_TESTS}


@pytest.mark.parametrize(("edges", "valid"), [
    ([], True), (["T-001"], True), (["T-001", "T-001"], False),
    (["T-002"], False), (["T-999"], False), ("T-001", False), ([None], False),
])
def test_interface_dependencies_are_explicit_validated_and_round_trip(
    math_plan: Plan, edges: object, valid: bool,
) -> None:
    original = tasks_from(math_plan)[1]
    task_entry(math_plan, 1)["interface_depends_on"] = edges
    graph = {"T-001": [], "T-002": ["T-001"]}
    assert (not dependency_errors(graph, {"T-002": edges})) == valid
    if valid:
        assert orch_generic.boundary_errors(math_plan, frozenset()) == []
        parsed = tasks_from(math_plan)[1]
        assert parsed.interface_depends_on == edges
        assert parsed.entry().get("interface_depends_on", []) == edges
        assert (contract_hash(parsed) == contract_hash(original)) == (not edges)


@pytest.mark.parametrize(("change", "changed"), [
    ("contract", True), ("requirement", True), ("interface", True), ("baseline", True),
    ("tests", False), ("implementation", False),
])
def test_dependency_contract_versions_track_promises_not_build_progress(
    frozen_plan: Plan, change: str, changed: bool,
) -> None:
    task_entry(frozen_plan, 1)["interface_depends_on"] = ["T-001"]
    context = RunContext.of_plan(frozen_plan)
    task = context.tasks["T-002"]
    before = context.dependency_contracts(task, "base")
    if change == "contract":
        context.tasks["T-001"].contract += " Explicit overflow behavior."
    elif change == "requirement":
        context.requirements["R1"] += " Handle overflow."
    elif change == "interface":
        context.stubs["adder.py"] += "# Published invariant changed\n"
    elif change == "tests":
        context.files["test_add.py"] += "# corrected fixture\n"
    elif change == "implementation":
        context.tasks["T-001"].status = "DONE"
        context.tasks["T-001"].commit = "a" * 40
    assert (context.dependency_contracts(task, "next" if change == "baseline" else "base")
            != before) == changed


@pytest.mark.parametrize(
    ("edits", "expected"),
    [
        ([{"path": "a.py", "search": "1", "replace": "2"}], {"a.py": "x = 2\n"}),
        ([{"path": "b.py", "search": "", "replace": "y = 1\n"}], {"b.py": "y = 1\n"}),
        ([{"path": "c.py", "search": "", "replace": "z\n"}], "edit outside owned files: c.py"),
        ([{"path": "a.py", "search": "nope", "replace": "q"}], "found 0 times"),
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


def test_failure_signatures_ignore_line_numbers_but_not_checks() -> None:
    first = (f"FAIL (exit 1): {ADD_TEST}\n\n--- python3 -m unittest -q test_add\n"
             '  File "a.py", line 3\nAssertionError: 1 != 5\n\nrevision=abc\n')
    moved = first.replace("line 3", "line 9").replace("1 != 5", "2 != 5")
    other = first.replace(ADD_TEST, DOUBLE_TEST)
    assert signature("test", first) == signature("test", moved) != signature("test", other)
    assert signature("edit", "x") != signature("test", "x")


@pytest.fixture
def add_task(math_plan: Plan) -> OrchTask:
    return tasks_from(math_plan)[0]


@pytest.fixture
def context_args(add_task: OrchTask) -> ContextArgs:
    return (add_task, {"R1": "add"}, "MAP" * 50, {"adder.py": ADDER_STUB},
            {"adder.py": ADDER_STUB}, {"test_add.py": ADD_TESTS})


def test_context_pack_orders_layers_and_sheds_shared_context(context_args: ContextArgs) -> None:
    full = context_pack(*context_args, 10_000)
    retry = context_pack(*context_args, 10_000, ("DIFF", "ERROR"))
    lean = context_pack(*context_args, len(full.text.encode()) - 100)
    assert full.text.startswith(BUILDER_INSTRUCTIONS) and "MAPMAP" in full.shared
    assert (retry.shared, retry.prefix_hash) == (full.shared, full.prefix_hash)
    assert "PREVIOUS ATTEMPT FAILED" in retry.retry and not full.retry
    assert "(omitted)" in lean.shared


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
    (0, {"adder.py"}),
    (1, {"adder.py", "doubler.py"}),
])
def test_interfaces_follow_task_dependencies_not_the_whole_project(
    frozen_plan: Plan, task_index: int, expected: set[str],
) -> None:
    cast(list[dict[str, str]], frozen_plan["files"]).append(
        {"path": "web/unrelated.ts", "content": "// unrelated interface\n" * 2000})
    context = RunContext.of_plan(frozen_plan)
    selected = context.interfaces_for(tasks_from(frozen_plan)[task_index])
    assert set(selected) == expected  # whole stubs; frozen tests are never interfaces
    assert all(source == context.stubs[path] for path, source in selected.items())


@pytest.mark.parametrize("retry", [("", ""), ("DIFF", "ERROR")])
def test_requested_context_is_rendered_verbatim_in_the_task_layer(
    context_args: ContextArgs, retry: tuple[str, str],
) -> None:
    requested = {'{"read": "z.py"}': "$ read z.py\n--- z.py ---\nLAST",
                 '{"search": "first"}': "$ search first\na.py:1\n    1> FIRST"}
    base = context_pack(*context_args, 20_000, retry)
    full = context_pack(*context_args, 20_000, retry, requested=requested)
    answered = full.task.split("## CONTEXT YOU ASKED FOR (this revision, read-only)\n")[1]
    assert answered.startswith("\n\n".join(requested.values()))
    assert full.shared == base.shared and full.prefix_hash == base.prefix_hash
    assert full.retry == base.retry and full.requested == tuple(requested)
    assert "CONTEXT YOU ASKED FOR" not in base.task
    # only the repository listing is shed before the pack is too large; requests stay whole
    lean = context_pack(*context_args, len(base.text.encode()), retry, requested=requested)
    assert "(omitted)" in lean.shared and lean.task == full.task
    with pytest.raises(ContextTooLarge):
        context_pack(*context_args, len(base.text.encode()) - 200, retry, requested=requested)


@pytest.mark.parametrize("requested", [None, {}])
@pytest.mark.parametrize("retry", [("", ""), ("DIFF", "ERROR")])
def test_context_pack_without_requests_preserves_exact_budget(
    context_args: ContextArgs, requested: dict[str, str] | None, retry: tuple[str, str],
) -> None:
    full = context_pack(*context_args, 20_000, retry, requested=requested)
    exact = context_pack(*context_args, len(full.text.encode()), retry, requested=requested)
    assert exact == full
    assert "CONTEXT YOU ASKED FOR" not in exact.task
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
    }


def test_a_task_keeps_its_component_through_the_plan_round_trip(
        math_plan: dict[str, object]) -> None:
    """Revisions rebuild plan tasks from task records: the component tag must survive, or
    merge gates and designers would group every task under the first component."""
    tagged = {**math_plan, "components": [{"id": "core", "dir": ".", "test_command": ""}],
              "tasks": [{**t, "component": "core"}  # type: ignore[dict-item]
                        for t in math_plan["tasks"]]}  # type: ignore[union-attr]
    tasks = tasks_from(tagged)
    assert all(task.component == "core" for task in tasks)
    assert all(task.entry()["component"] == "core" for task in tasks)
    assert "component" not in tasks_from(math_plan)[0].entry()
