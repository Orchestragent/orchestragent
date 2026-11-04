"""The engine end to end on a project mixing languages: components, their tests, the
planner's integration checks, builds, gates and merges.

The models (scripted here) choose the languages, paths and verification commands; orch
freezes nothing but ownership, checks red on stubs and green on the implementation, merges,
and hands judgment to the supervisor when cheap attempts fail.
"""

from pathlib import Path
from typing import cast

import pytest

from orchestragent.native import NativeResult
from orchestragent.orch_run import Orchestrator
from orchestragent.platform import sandbox_available
from orchestragent.workspace import Workspace
from tests.conftest import (
    SUPERVISE_ESCALATE,
    SUPERVISE_GIVE_UP,
    SUPERVISE_RETRY,
    ScriptedCaller,
    component_design,
    edit_result,
    integration,
    local_runner,
    supervise,
)

pytestmark = pytest.mark.skipif(not sandbox_available(), reason="no sandbox backend")

GREET_STUB = 'greet() { echo "not implemented" >&2; return 1; }\n'
SLUG_STUB = "def slug(text):\n    raise NotImplementedError\n"
# Two components: a shell library under lib/ and a Python tool under tools/.
BOUNDARY: dict[str, object] = {
    "summary": "a shell greeter and a Python slug helper",
    "requirements": [{"id": "R1", "text": "greet NAME prints 'hello NAME'"},
                     {"id": "R2", "text": "slug(text) lower-cases and joins words with -"}],
    "components": [{"id": "sh", "dir": "lib", "test_command": "sh lib/tests/greet_test.sh"},
                   {"id": "py", "dir": "tools", "test_command": ""}],
    "files": [{"path": "lib/greet.sh", "content": GREET_STUB},
              {"path": "tools/slug.py", "content": SLUG_STUB}],
    "tasks": [
        {"id": "T-001", "goal": "greeter", "component": "sh", "reqs": ["R1"],
         "files": ["lib/greet.sh"], "depends_on": [], "interface_depends_on": [],
         "contract": "greet NAME -> hello NAME", "tests": []},
        {"id": "T-002", "goal": "slug", "component": "py", "reqs": ["R2"],
         "files": ["tools/slug.py"], "depends_on": [], "interface_depends_on": [],
         "contract": "slug(str) -> str", "tests": []},
    ],
}
GREET_TEST = ('. lib/greet.sh\n[ "$(greet Ada)" = "hello Ada" ] '
              '|| { echo "R1 greet Ada"; exit 1; }\n')
GREET_CMD = "sh lib/tests/greet_test.sh"
SLUG_TEST = ("import unittest\nfrom slug import slug\n\n"
             "class SlugTest(unittest.TestCase):\n"
             "    def test_r2_slug(self):\n"
             "        self.assertEqual(slug('Hello Big World'), 'hello-big-world')\n")
SLUG_CMD = "cd tools && python3 -m unittest -q test_slug"
GREET_SHARD = ("T-001", "lib/tests/greet_test.sh", GREET_TEST, GREET_CMD)
SLUG_SHARD = ("T-002", "tools/test_slug.py", SLUG_TEST, SLUG_CMD)
GREET_IMPL = edit_result("lib/greet.sh", 'greet() { echo "hello $1"; }\n', search="")
SLUG_IMPL = edit_result("tools/slug.py", "return '-'.join(text.lower().split())")


def designs() -> dict[str, list[NativeResult]]:
    """Fresh queues each time: the scripted caller consumes them."""
    return {"dsh:design:sh": [component_design(GREET_SHARD)],
            "dsh:design:py": [component_design(SLUG_SHARD)]}


@pytest.fixture
def polyglot(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    Workspace.git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("polyglot demo\n")
    Workspace.git(repo, "add", "README.md")
    Workspace.git(repo, "commit", "-m", "seed")
    return repo


def test_components_are_designed_apart_built_and_merged(polyglot: Path) -> None:
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", BOUNDARY)],
        # a vacuous shard (passes on the stub) is sent back to its component's designer
        "dsh:design:sh": [component_design(("T-001", "lib/tests/greet_test.sh", "true\n",
                                            GREET_CMD)),
                          component_design(GREET_SHARD)],
        "dsh:design:py": [component_design(SLUG_SHARD)],
        "dsh:T-001": [GREET_IMPL], "dsh:T-002": [SLUG_IMPL],
    })
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    planned = orch.plan("A shell greeter and a Python slug helper", test_jobs=2)
    tasks = {t["id"]: t for t in cast(list[dict[str, list[str]]], planned.plan["tasks"])}
    assert all(test.startswith("cmd:") for t in tasks.values() for test in t["tests"])
    prompts = [text for _, key, text in caller.calls if key == "dsh:design:sh"]
    assert "prove nothing" in prompts[1]  # the red gate's feedback, in the designer's words
    assert "Component sh (directory lib" in prompts[0] and "T-002" not in prompts[0].split(
        "## ASSIGNED COMPONENT")[1].split("PINNED")[0]
    run = orch.execute(planned.id)
    assert run.status == "done", orch.gate_failure
    branch = f"orch/{run.id}"
    assert "hello" in Workspace.git(polyglot, "show", f"{branch}:lib/greet.sh")
    assert "join" in Workspace.git(polyglot, "show", f"{branch}:tools/slug.py")
    listed = Workspace.git(polyglot, "ls-tree", "-r", "--name-only", branch).split()
    assert "lib/tests/greet_test.sh" in listed and not any(".orch-tmp" in p for p in listed)
    suites = [e for e in orch.state.events(run.id) if e.kind == "run.component_suites"]
    assert suites and suites[0].data == {"passed": True, "suites": 1, "unrunnable": False}


def test_a_test_outside_its_component_is_refused_then_redesigned(polyglot: Path) -> None:
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", BOUNDARY)],
        "dsh:design:sh": [component_design(GREET_SHARD)],
        "dsh:design:py": [component_design(("T-002", "tests/test_slug.py", SLUG_TEST,
                                            "python3 -m unittest -q tests.test_slug")),
                          component_design(SLUG_SHARD)],
        "dsh:T-001": [GREET_IMPL], "dsh:T-002": [SLUG_IMPL],
    })
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    planned = orch.plan("A shell greeter and a Python slug helper", test_jobs=2)
    prompts = [text for _, key, text in caller.calls if key == "dsh:design:py"]
    assert "live under tools/" in prompts[1]
    paths = [f["path"] for f in cast(list[dict[str, str]], planned.plan["files"])]
    assert "tools/test_slug.py" in paths and "tests/test_slug.py" not in paths
    assert orch.execute(planned.id).status == "done", orch.gate_failure


def test_the_planner_writes_the_check_where_components_meet(polyglot: Path) -> None:
    """R3 needs both components: the planner's integration check covers T-001 and T-002, so
    the task that takes it gains the other as a dependency and runs it at its merge."""
    boundary = {
        **BOUNDARY,
        "requirements": [*cast(list[object], BOUNDARY["requirements"]),
                         {"id": "R3", "text": "greet and slug agree: greet's output slugs"}],
        "tasks": [cast(list[dict[str, object]], BOUNDARY["tasks"])[0],
                  {**cast(list[dict[str, object]], BOUNDARY["tasks"])[1], "reqs": ["R2", "R3"],
                   "depends_on": ["T-001"]}],
    }
    agree = ('. lib/greet.sh\nout=$(greet Ada) || exit 1\n'
             '[ "$(cd tools && python3 -c "from slug import slug; print(slug(\'$out\'))")" '
             '= "hello-ada" ] || { echo "R3 disagree"; exit 1; }\n')
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", boundary)],
        "claude:integration": [integration((["T-001", "T-002"], "tools/tests_agree.sh", agree,
                                            "sh tools/tests_agree.sh"))],
        **designs(), "dsh:T-001": [GREET_IMPL], "dsh:T-002": [SLUG_IMPL],
    })
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    planned = orch.plan("greeter, slug and their agreement", test_jobs=2)
    prompt = next(text for _, key, text in caller.calls if key == "claude:integration")
    assert "WHERE TASKS MEET" in prompt and '"joins":["T-002"]' in prompt
    tasks = {t["id"]: t for t in cast(list[dict[str, list[str]]], planned.plan["tasks"])}
    assert len(tasks["T-002"]["tests"]) == 2 and len(tasks["T-001"]["tests"]) == 1
    run = orch.execute(planned.id)
    assert run.status == "done", orch.gate_failure
    gates = [e.data["tests"] for e in orch.state.events(run.id, "T-002")
             if e.kind == "task.gate"]
    # Its own two checks: T-001 is a dependency in another component, not a dependent, so
    # T-002's merge cannot break it; the final gate runs everything once more.
    assert gates == [2]


def test_a_person_finishes_a_task_the_supervisor_gave_up_on(polyglot: Path) -> None:
    wrong = edit_result("lib/greet.sh", 'greet() { echo "hi $1"; }\n', search="")
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", BOUNDARY)], **designs(),
        "dsh:T-001": [wrong, wrong, wrong, wrong, wrong, wrong],
        "claude:T-001": [SUPERVISE_RETRY, SUPERVISE_ESCALATE, wrong, SUPERVISE_GIVE_UP,
                         SUPERVISE_RETRY, SUPERVISE_ESCALATE, wrong, SUPERVISE_GIVE_UP],
        "dsh:T-002": [SLUG_IMPL],
    })
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    run = orch.execute(orch.plan("greeter and slug", test_jobs=2).id)
    assert orch.state.task(run.id, "T-001").status == "PARKED"
    assert orch.state.task(run.id, "T-002").status == "DONE"

    hinted = orch.state.hint(run.id, "T-001", "print exactly 'hello NAME'")
    assert hinted.status == "READY" and "hello NAME" in hinted.human_hint
    orch.execute(run.id)  # the scripted builder still answers wrongly: parked again
    prompts = [text for _, key, text in caller.calls if key == "dsh:T-001"]
    assert "HUMAN GUIDANCE" in prompts[-1] and "hello NAME" in prompts[-1]
    assert orch.state.task(run.id, "T-001").status == "PARKED"

    path = orch.take(run.id, "T-001")
    assert 'hi $1' in (path / "lib" / "greet.sh").read_text()  # the latest candidate
    (path / "lib" / "tests" / "greet_test.sh").write_text("true\n")  # not its file: refused
    merged, report = orch.submit(run.id, "T-001")
    assert not merged and "outside" in report
    Workspace.git(path, "checkout", "--", "lib/tests/greet_test.sh")
    merged, report = orch.submit(run.id, "T-001")  # still the wrong greeting
    assert not merged and "FAIL" in report
    (path / "lib" / "greet.sh").write_text('greet() { echo "hello $1"; }\n')
    merged, report = orch.submit(run.id, "T-001")
    assert merged, report
    finished = orch.execute(run.id)
    assert finished.status == "done", orch.gate_failure
    subjects = Workspace.git(polyglot, "log", "--format=%s", f"orch/{run.id}").splitlines()
    assert "orch T-001: finished by a person" in subjects


def test_a_task_that_owns_no_files_is_decided_by_its_checks_alone(polyglot: Path) -> None:
    """A verification-only task (no components declared: one implicit root component)."""
    plain = {k: v for k, v in BOUNDARY.items() if k != "components"}
    tasks = [{k: v for k, v in t.items() if k != "component"}
             for t in cast(list[dict[str, object]], BOUNDARY["tasks"])]
    boundary = {
        **plain,
        "requirements": [*cast(list[object], BOUNDARY["requirements"]),
                         {"id": "R3", "text": "greet and slug agree: greet's output slugs"}],
        "tasks": [*tasks, {"id": "T-003", "goal": "agreement", "reqs": ["R3"], "files": [],
                           "depends_on": ["T-001", "T-002"], "interface_depends_on": [],
                           "contract": "verification only", "tests": []}],
    }
    agree = ('. lib/greet.sh\nout=$(greet Ada) || exit 1\n'
             '[ "$(cd tools && python3 -c "from slug import slug; print(slug(\'$out\'))")" '
             '= "hello-ada" ] || { echo "R3 disagree"; exit 1; }\n')
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", boundary)], "claude:integration": [integration()],
        "dsh:design:root": [component_design(
            GREET_SHARD, SLUG_SHARD, ("T-003", "tests/agree_test.sh", agree,
                                      "sh tests/agree_test.sh"))],
        "dsh:T-001": [GREET_IMPL], "dsh:T-002": [SLUG_IMPL],
    })
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    run = orch.execute(orch.plan("greeter, slug and their agreement", test_jobs=1).id)
    assert run.status == "done", orch.gate_failure
    assert orch.state.task(run.id, "T-003").status == "DONE"
    assert "dsh:T-003" not in caller.keys()  # no builder call: its checks decided


RIPPLE_BOUNDARY: dict[str, object] = {
    "summary": "a greeter, a shouter built on it, and an unrelated wave",
    "requirements": [{"id": "R1", "text": "greet NAME prints a greeting naming NAME"},
                     {"id": "R2", "text": "shout NAME prints HELLO NAME in capitals"},
                     {"id": "R3", "text": "wave prints ~"}],
    "files": [{"path": "lib/greet.sh", "content": GREET_STUB},
              {"path": "lib/shout.sh", "content": 'shout() { echo "not implemented" >&2; '
                                                  'return 1; }\n'},
              {"path": "lib/wave.sh", "content": 'wave() { echo "not implemented" >&2; '
                                                 'return 1; }\n'}],
    "tasks": [
        {"id": "T-001", "goal": "greeter", "reqs": ["R1"], "files": ["lib/greet.sh"],
         "depends_on": [], "contract": "greet NAME", "tests": []},
        {"id": "T-002", "goal": "shouter", "reqs": ["R2"], "files": ["lib/shout.sh"],
         "depends_on": ["T-001"], "contract": "shout NAME", "tests": []},
        {"id": "T-003", "goal": "wave", "reqs": ["R3"], "files": ["lib/wave.sh"],
         "depends_on": ["T-001"], "contract": "wave", "tests": []},
    ],
}
RIPPLE_DESIGN = component_design(
    ("T-001", "tests/greet_test.sh", '. lib/greet.sh\ngreet Ada | grep -q Ada\n',
     "sh tests/greet_test.sh"),
    ("T-002", "tests/shout_test.sh", '. lib/greet.sh\n. lib/shout.sh\n[ "$(shout Ada)" = '
     '"HELLO ADA" ] || { echo "R2 shout"; exit 1; }\n', "sh tests/shout_test.sh"),
    ("T-003", "tests/wave_test.sh", '. lib/wave.sh\n[ "$(wave)" = "~" ]\n',
     "sh tests/wave_test.sh"))


def test_a_person_reworks_a_done_node_and_only_broken_dependents_rebuild(
        polyglot: Path) -> None:
    """Guidance with rework rebuilds a DONE node; a dependent whose checks now fail is
    reopened and rebuilt, one whose checks still pass is only marked unaffected. A node's
    chosen model builds it."""
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", RIPPLE_BOUNDARY)],
        "dsh:design:root": [RIPPLE_DESIGN],
        "dsh:T-001": [edit_result("lib/greet.sh", 'greet() { echo "hello $1"; }\n', search=""),
                      edit_result("lib/greet.sh", 'greet() { echo "hi $1"; }\n', search="")],
        "dsh:T-002": [edit_result("lib/shout.sh", 'shout() { greet "$1" | tr a-z A-Z; }\n',
                                  search=""),
                      edit_result("lib/shout.sh",
                                  'shout() { echo "HELLO $(echo "$1" | tr a-z A-Z)"; }\n',
                                  search="")],
        "claude:T-003": [edit_result("lib/wave.sh", 'wave() { echo "~"; }\n', search="")],
    })
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    planned = orch.plan("greeter, shouter and wave", test_jobs=1)
    orch.state.set_route(planned.id, "T-003", "claude/claude-opus-5-5:medium")
    run = orch.execute(planned.id)
    assert run.status == "done", orch.gate_failure
    assert caller.keys().count("claude:T-003") == 1

    reopened = orch.state.message(run.id, "T-001", "Say hi instead of hello", rework=True)
    assert reopened.status == "READY" and reopened.reopened_by == "person"
    finished = orch.execute(run.id)
    assert finished.status == "done", orch.gate_failure
    prompts = [text for _, key, text in caller.calls if key == "dsh:T-001"]
    assert "Say hi instead of hello" in prompts[-1]
    ripples = {event.task_id: event.data["affected"] for event in orch.state.events(run.id)
               if event.kind == "task.ripple"}
    assert ripples == {"T-002": True, "T-003": False}
    branch = f"orch/{run.id}"
    assert "hi" in Workspace.git(polyglot, "show", f"{branch}:lib/greet.sh")
    assert "HELLO $(echo" in Workspace.git(polyglot, "show", f"{branch}:lib/shout.sh")
    assert all(task.status == "DONE" and not task.reopened_by
               for task in orch.state.tasks(run.id))


def test_merge_gates_run_what_a_change_can_break_in_parallel(polyglot: Path) -> None:
    """A merge runs its own checks and those of done tasks that depend on it or share its
    component, each in its own worktree; the final gate still runs everything."""
    from orchestragent.orch_run import combined

    caller = ScriptedCaller({
        "plan": [NativeResult("ok", RIPPLE_BOUNDARY)],
        "dsh:design:root": [RIPPLE_DESIGN],
        "dsh:T-001": [edit_result("lib/greet.sh", 'greet() { echo "hello $1"; }\n', search="")],
        "dsh:T-002": [edit_result("lib/shout.sh", 'shout() { greet "$1" | tr a-z A-Z; }\n',
                                  search="")],
        "dsh:T-003": [edit_result("lib/wave.sh", 'wave() { echo "~"; }\n', search="")],
    })
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    run = orch.execute(orch.plan("greeter, shouter and wave", test_jobs=1).id)
    assert run.status == "done", orch.gate_failure
    tasks = {task.id: task for task in orch.state.tasks(run.id)}
    # one implicit component: every task shares it, so every merge gate includes all three
    assert orch._gate_scope(run.id, tasks["T-003"], ["lib/wave.sh"]) == {"T-001", "T-002",
                                                                         "T-003"}
    head = Workspace.git(orch.integration(run.id), "rev-parse", "HEAD")
    passed, report = orch._checks_at(run.id, "gate", head,
                                     [t for task in tasks.values() for t in task.tests])
    assert passed and report.count("PASS: ") == 3
    merged = combined(["PASS: a\n\nissuer=x", "FAIL (exit 1): b\n\n--- b\nboom"])
    assert merged.splitlines()[:2] == ["PASS: a", "FAIL (exit 1): b"] and "boom" in merged
    assert "issuer=x" not in merged


def test_a_suite_command_that_cannot_run_is_sent_back_to_the_planner(polyglot: Path) -> None:
    """`python` for `python3`, a runner that is not installed: the declared component test
    command must at least start here, and the planner hears about it like any lint error."""
    broken = {**BOUNDARY, "components": [
        {"id": "sh", "dir": "lib", "test_command": "no-such-runner lib/tests"},
        {"id": "py", "dir": "tools", "test_command": ""}]}
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", broken), NativeResult("ok", BOUNDARY)], **designs(),
        "dsh:T-001": [GREET_IMPL], "dsh:T-002": [SLUG_IMPL],
    })
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    planned = orch.plan("A shell greeter and a Python slug helper", test_jobs=2)
    second = [text for _, key, text in caller.calls if key == "plan"][1]
    assert "test_command could not run (exit 127)" in second
    assert orch.execute(planned.id).status == "done", orch.gate_failure


def test_a_suite_failure_at_the_final_gate_goes_to_the_supervisor(polyglot: Path) -> None:
    """An integration test that passes under its own check command (which sets FIX) but
    fails under the component's normal test command. The final gate catches it; instead of
    parking the run, the task whose test it is goes to the supervisor with the evidence."""
    fixture = ('[ -n "$FIX" ] || { echo "lib/tests/fixture_test.sh: FIX is not set"; exit 1; }\n'
               '. lib/greet.sh\n[ "$(greet Bo)" = "hello Bo" ] || { echo "R1 Bo"; exit 1; }\n')
    fixed = '. lib/greet.sh\n[ "$(greet Bo)" = "hello Bo" ] || { echo "R1 Bo"; exit 1; }\n'
    tasks = [dict(t) for t in cast(list[dict[str, object]], BOUNDARY["tasks"])]
    tasks[1]["depends_on"] = ["T-001"]  # a join: T-002 builds on T-001
    boundary = {**BOUNDARY, "tasks": tasks, "components": [
        {"id": "sh", "dir": "lib",
         "test_command": "sh lib/tests/greet_test.sh && sh lib/tests/fixture_test.sh"},
        {"id": "py", "dir": "tools", "test_command": ""}]}
    slug_again = edit_result(
        "tools/slug.py", "def slug(text):\n    return '-'.join(text.lower().split())\n",
        search="")
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", boundary)], **designs(),
        "claude:integration": [integration((["T-001", "T-002"], "lib/tests/fixture_test.sh",
                                            fixture, "FIX=1 sh lib/tests/fixture_test.sh"))],
        "dsh:T-001": [GREET_IMPL], "dsh:T-002": [SLUG_IMPL, slug_again],
        "claude:T-002": [supervise("revise_tests", explanation="hold without FIX too",
                                   test_edits=[{"path": "lib/tests/fixture_test.sh",
                                                "search": "", "replace": fixed}])],
    })
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    run = orch.execute(orch.plan("greeter and slug, agreeing", test_jobs=2).id)
    assert run.status == "done", orch.gate_failure
    task = orch.state.task(run.id, "T-002")
    assert task.status == "DONE" and task.supervisions == 1
    prompt = next(text for _, key, text in caller.calls if key == "claude:T-002")
    assert "component's own test command" in prompt and "FIX is not set" in prompt
    reopened = [e for e in orch.state.events(run.id) if e.kind == "run.regression"]
    assert len(reopened) == 1 and reopened[0].data["culprit"] == "T-002"
    assert (polyglot / "lib" / "tests" / "fixture_test.sh").exists() is False  # main untouched


def test_a_command_edit_also_repairs_the_component_test_command(polyglot: Path) -> None:
    """The designer ran the shell tests with a variable that makes the runner refuse; the
    component's own test command carried the same mistake. One command edit fixes both."""
    guarded = '[ -z "$FAIL" ] || { echo "runner misconfigured"; exit 1; }\n' + GREET_TEST
    boundary = {**BOUNDARY, "components": [
        {"id": "sh", "dir": "lib", "test_command": "FAIL=1 sh lib/tests/greet_test.sh"},
        {"id": "py", "dir": "tools", "test_command": ""}]}
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", boundary)],
        "dsh:design:sh": [component_design(("T-001", "lib/tests/greet_test.sh", guarded,
                                            "FAIL=1 sh lib/tests/greet_test.sh"))],
        "dsh:design:py": [component_design(SLUG_SHARD)],
        "dsh:T-001": [GREET_IMPL, GREET_IMPL], "dsh:T-002": [SLUG_IMPL],
        "claude:T-001": [supervise("revise_tests", explanation="FAIL=1 breaks the runner",
                                   command_edits=[{"search": "FAIL=1 ", "replace": ""}])],
    })
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    run = orch.execute(orch.plan("greeter and slug", test_jobs=2).id)
    assert run.status == "done", orch.gate_failure
    task = orch.state.task(run.id, "T-001")
    assert task.status == "DONE" and task.component == "sh"
    assert all("FAIL=1" not in test for test in task.tests)
    plan = orch.state.run(run.id).plan
    components = {c["id"]: c["test_command"] for c in plan["components"]}  # type: ignore[index, union-attr]
    assert components["sh"] == "sh lib/tests/greet_test.sh"
    tags = {t["id"]: t.get("component") for t in plan["tasks"]}  # type: ignore[index, union-attr]
    assert tags == {"T-001": "sh", "T-002": "py"}  # revision kept every task's component


JOINED_BOUNDARY: dict[str, object] = {
    **BOUNDARY,
    "requirements": [*cast(list[object], BOUNDARY["requirements"]),
                     {"id": "R3", "text": "greet and slug agree: greet's output slugs"}],
    "tasks": [cast(list[dict[str, object]], BOUNDARY["tasks"])[0],
              {**cast(list[dict[str, object]], BOUNDARY["tasks"])[1], "reqs": ["R2", "R3"],
               "depends_on": ["T-001"]}],
}
AGREE = ('. lib/greet.sh\nout=$(greet Ada) || exit 1\n'
         '[ "$(cd tools && python3 -c "from slug import slug; print(slug(\'$out\'))")" '
         '= "hello-ada" ] || { echo "R3 disagree"; exit 1; }\n')


def test_the_integration_design_runs_alongside_the_component_designs(polyglot: Path) -> None:
    """The integration design needs only the boundary. A component designer here waits
    until the integration design has been asked for: run one after the other, planning
    would stall until the wait times out."""
    import threading
    asked = threading.Event()

    class Overlapping(ScriptedCaller):
        def __call__(self, provider, model, prompt, schema, run_id, cancelled, *,  # type: ignore[no-untyped-def, override]
                     effort=""):
            if "-integration-design-planner-" in run_id:
                asked.set()
            elif "-test-design-component-" in run_id:
                assert asked.wait(20), "component design finished before integration began"
            return super().__call__(provider, model, prompt, schema, run_id, cancelled,
                                    effort=effort)

    caller = Overlapping({
        "plan": [NativeResult("ok", JOINED_BOUNDARY)],
        "claude:integration": [integration((["T-001", "T-002"], "tools/tests_agree.sh",
                                            AGREE, "sh tools/tests_agree.sh"))],
        **designs(), "dsh:T-001": [GREET_IMPL], "dsh:T-002": [SLUG_IMPL]})
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    run = orch.execute(orch.plan("greeter, slug and their agreement", test_jobs=2).id)
    assert run.status == "done", orch.gate_failure


def test_an_integration_check_on_a_component_test_path_is_sent_back(polyglot: Path) -> None:
    """Designed in parallel, the integration check may pick a path a component designer
    also chose; that clash is sent back as the next attempt, with the path named."""
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", JOINED_BOUNDARY)],
        "claude:integration": [
            integration((["T-001", "T-002"], "tools/test_slug.py", AGREE,
                         "sh tools/test_slug.py")),
            integration((["T-001", "T-002"], "tools/tests_agree.sh", AGREE,
                         "sh tools/tests_agree.sh"))],
        **designs(), "dsh:T-001": [GREET_IMPL], "dsh:T-002": [SLUG_IMPL]})
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    planned = orch.plan("greeter, slug and their agreement", test_jobs=2)
    asks = [text for _, key, text in caller.calls if key == "claude:integration"]
    assert len(asks) == 2 and "tools/test_slug.py: already a component's test" in asks[1]
    assert orch.execute(planned.id).status == "done", orch.gate_failure


def test_an_integration_check_red_for_the_wrong_reason_is_sent_back(
    polyglot: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The planner's integration check goes through the same judgment as component checks."""
    from orchestragent import orch_run
    monkeypatch.setattr(orch_run, "RED_JUDGMENT", True)
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", JOINED_BOUNDARY)],
        "claude:integration": [
            integration((["T-001", "T-002"], "tools/tests_agree.sh", AGREE,
                         "FIX=1 sh tools/tests_agree.sh")),
            integration((["T-001", "T-002"], "tools/tests_agree.sh", AGREE,
                         "sh tools/tests_agree.sh"))],
        "dsh:red:integration": [NativeResult("ok", {"checks": [
            {"check": "I1", "verdict": "runner_failed",
             "reason": "FIX=1 is set only by this command"}]}),
            NativeResult("ok", {"checks": []})],
        **designs(), "dsh:T-001": [GREET_IMPL], "dsh:T-002": [SLUG_IMPL]})
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    planned = orch.plan("greeter, slug and their agreement", test_jobs=2)
    asks = [text for _, key, text in caller.calls if key == "claude:integration"]
    assert len(asks) == 2 and "FIX=1 is set only by this command" in asks[1]
    assert orch.execute(planned.id).status == "done", orch.gate_failure
