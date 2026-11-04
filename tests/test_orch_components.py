"""Components: disjoint directories with their own runner; one test designer each; the
planner's integration checks where tasks meet."""

from typing import Any, cast

import pytest

from orchestragent import orch_generic
from orchestragent.orch_thin import component_test_prompt, integration_prompt
from orchestragent.orch_verify import Check
from tests.test_orch_generic_e2e import BOUNDARY, GREET_TEST, SLUG_TEST

TASKS = cast(list[dict[str, Any]], BOUNDARY["tasks"])


def with_components(*components: dict[str, str], tasks: list[dict[str, Any]] | None = None
                    ) -> dict[str, object]:
    return {**BOUNDARY, "components": list(components), "tasks": tasks or TASKS}


def test_components_default_to_the_root_and_must_not_overlap() -> None:
    plain = {k: v for k, v in BOUNDARY.items() if k != "components"}
    assert orch_generic.components(plain) == [{"id": "", "dir": ".", "test_command": ""}]
    assert orch_generic.boundary_errors(BOUNDARY, frozenset()) == []
    nested = with_components({"id": "a", "dir": "lib", "test_command": ""},
                             {"id": "b", "dir": "lib/inner", "test_command": ""})
    errors = orch_generic.boundary_errors(nested, frozenset())
    assert any("overlap" in e for e in errors)
    unknown = with_components({"id": "sh", "dir": "lib", "test_command": ""},
                              tasks=[{**TASKS[0], "component": "py"}, TASKS[1]])
    assert any("not declared" in e for e in orch_generic.boundary_errors(unknown, frozenset()))
    astray = with_components({"id": "sh", "dir": "lib", "test_command": ""},
                             {"id": "py", "dir": "tools", "test_command": ""},
                             tasks=[{**TASKS[0], "files": ["tools/greet.sh"]}, TASKS[1]])
    assert any("outside its component" in e
               for e in orch_generic.boundary_errors(astray, frozenset()))


def test_component_shards_cover_each_task_once_inside_the_directory() -> None:
    sh = orch_generic.components(BOUNDARY)[0]
    good = {"shards": [{"task": "T-001", "files": [{"path": "lib/tests/greet_test.sh",
                                                     "content": GREET_TEST}],
                        "command": "sh lib/tests/greet_test.sh"}]}
    [(tid, files, check)] = orch_generic.component_shards(BOUNDARY, sh, good)
    assert tid == "T-001" and files[0]["path"] == "lib/tests/greet_test.sh"
    assert check == Check("sh lib/tests/greet_test.sh", ("lib/tests/greet_test.sh",))
    with pytest.raises(ValueError, match="live under lib/"):
        orch_generic.component_shards(BOUNDARY, sh, {"shards": [{
            "task": "T-001", "files": [{"path": "tests/greet_test.sh", "content": GREET_TEST}],
            "command": "sh tests/greet_test.sh"}]})
    with pytest.raises(ValueError, match="not a task of this component"):
        orch_generic.component_shards(BOUNDARY, sh, {"shards": [{
            "task": "T-002", "files": [{"path": "lib/x.sh", "content": "x"}], "command": "sh x"}]})
    with pytest.raises(ValueError, match="no shard for"):
        orch_generic.component_shards(BOUNDARY, sh, {"shards": []})
    with pytest.raises(ValueError, match="exactly one per task"):
        orch_generic.component_shards(BOUNDARY, sh, {"shards": [good["shards"][0],
                                                                good["shards"][0]]})


def test_integration_checks_name_two_tasks_and_joins_find_meeting_points() -> None:
    assert orch_generic.joins(BOUNDARY) == []  # independent tasks in two components
    joined = {**BOUNDARY, "tasks": [TASKS[0], {**TASKS[1], "depends_on": ["T-001"]}]}
    assert orch_generic.joins(joined) == ["T-002"]  # depends across components
    checks = orch_generic.integration_checks(joined, {"checks": [{
        "tasks": ["T-001", "T-002"], "files": [{"path": "tools/agree.sh", "content": "x"}],
        "command": "sh tools/agree.sh"}]})
    assert checks[0][0] == ["T-001", "T-002"] and checks[0][2].command == "sh tools/agree.sh"
    with pytest.raises(ValueError, match="two or more existing tasks"):
        orch_generic.integration_checks(joined, {"checks": [{
            "tasks": ["T-001"], "files": [{"path": "a.sh", "content": "x"}], "command": "sh a"}]})
    assert orch_generic.integration_checks(joined, {"checks": []}) == []


def test_prompts_scope_a_component_and_name_the_joins() -> None:
    py = orch_generic.components(BOUNDARY)[1]
    prompt = component_test_prompt("SPEC", BOUNDARY, "MAP", ["fix this"], component=py)
    assigned = prompt.split("## ASSIGNED COMPONENT")[1]
    assert "Component py (directory tools)" in assigned and '["T-002"]' in assigned
    assert SLUG_TEST not in prompt and "fix this" in prompt and "SPEC" in prompt
    assert "Every test must fail on the stubs" in prompt
    joined = {**BOUNDARY, "tasks": [TASKS[0], {**TASKS[1], "depends_on": ["T-001"]}]}
    text = integration_prompt("SPEC", joined, "MAP", [])
    assert '"joins":["T-002"]' in text and "never mocks of one side" in text


def test_the_sandbox_note_states_which_tools_exist_here() -> None:
    from orchestragent import orch_verify
    present, absent = orch_verify.tool_inventory()
    note = orch_verify.sandbox_note()
    assert "sh" in present and "Commands available here:" in note
    for name in present:
        assert name in note
    if "python" in absent:
        assert "Not installed: python" in note
    assert note in component_test_prompt("SPEC", BOUNDARY, "MAP", [],
                                         component=orch_generic.components(BOUNDARY)[0])


def test_a_test_file_on_an_implementation_path_is_refused_with_its_owner_named() -> None:
    """A designer told only "must be a new file" resent the same path; the refusal names
    whose file it is and what to do instead."""
    boundary: dict[str, object] = {
        "files": [{"path": "integration/harness.py", "content": "def run(): ...\n"}],
        "tasks": [{"id": "T-007", "files": ["integration/harness.py"]}]}
    design = {"files": [{"path": "integration/harness.py", "content": "x = 1\n"}],
              "command": "python3 -m unittest"}
    with pytest.raises(ValueError, match="written by T-007.*new file at another path"):
        orch_generic.test_shard(boundary, "T-007", design)
    design["files"] = [{"path": "README.md", "content": "x\n"}]
    with pytest.raises(ValueError, match="already exists in the repository"):
        orch_generic.test_shard(boundary, "T-007", design, existing=frozenset({"README.md"}))


def test_a_malformed_item_is_refused_with_what_it_lacked() -> None:
    """Meridian (after-step7c): a designer twice resent a shard without its command after
    being told only 'each shard is {task, files, command}'."""
    message = orch_generic.shape_error("shard", {"task": "T-016", "files": []},
                                       {"task", "files", "command"})
    assert message == "each shard is {command, files, task} (missing command)"
    assert "unexpected note" in orch_generic.shape_error(
        "check", {"files": [], "command": "x", "tasks": [], "note": ""},
        {"files", "command", "tasks"})
    assert "not list" in orch_generic.shape_error("file", [], {"path", "content"})
