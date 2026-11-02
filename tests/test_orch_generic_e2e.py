"""The generic engine end to end: a project mixing languages, with model-declared checks.

The models (scripted here) choose the languages, paths and verification commands; orch only
freezes them, checks red on stubs and green on the implementation, and merges.
"""

from pathlib import Path
from typing import cast

import pytest

from orchestragent.native import NativeResult
from orchestragent.orch_run import Orchestrator
from orchestragent.platform import sandbox_available
from orchestragent.workspace import Workspace
from tests.conftest import ScriptedCaller, design, edit_result, local_runner

pytestmark = pytest.mark.skipif(not sandbox_available(), reason="no sandbox backend")

GREET_STUB = 'greet() { echo "not implemented" >&2; return 1; }\n'
SLUG_STUB = "def slug(text):\n    raise NotImplementedError\n"
BOUNDARY: dict[str, object] = {
    "summary": "a shell greeter and a Python slug helper",
    "requirements": [{"id": "R1", "text": "greet NAME prints 'hello NAME'"},
                     {"id": "R2", "text": "slug(text) lower-cases and joins words with -"}],
    "files": [{"path": "lib/greet.sh", "content": GREET_STUB},
              {"path": "tools/slug.py", "content": SLUG_STUB}],
    "tasks": [
        {"id": "T-001", "goal": "greeter", "reqs": ["R1"], "files": ["lib/greet.sh"],
         "depends_on": [], "interface_depends_on": [], "contract": "greet NAME -> hello NAME",
         "tests": []},
        {"id": "T-002", "goal": "slug", "reqs": ["R2"], "files": ["tools/slug.py"],
         "depends_on": [], "interface_depends_on": [], "contract": "slug(str) -> str",
         "tests": []},
    ],
}
GREET_TEST = ('. lib/greet.sh\n[ "$(greet Ada)" = "hello Ada" ] '
              '|| { echo "R1 greet Ada"; exit 1; }\n')
SLUG_TEST = ("import unittest\nfrom slug import slug\n\n"
             "class SlugTest(unittest.TestCase):\n"
             "    def test_r2_slug(self):\n"
             "        self.assertEqual(slug('Hello Big World'), 'hello-big-world')\n")


@pytest.fixture
def polyglot(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    Workspace.git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("polyglot demo\n")
    Workspace.git(repo, "add", "README.md")
    Workspace.git(repo, "commit", "-m", "seed")
    return repo


def test_mixed_language_plan_freezes_declared_checks_builds_and_merges(polyglot: Path) -> None:
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", BOUNDARY)],
        "dsh:T-001": [
            # a vacuous test (passes on the stub) is sent back; then a real one
            design("tests/greet_test.sh", "true\n", "sh tests/greet_test.sh"),
            design("tests/greet_test.sh", GREET_TEST, "sh tests/greet_test.sh"),
            edit_result("lib/greet.sh", 'greet() { echo "hello $1"; }\n', search=""),
        ],
        "dsh:T-002": [
            design("tools/test_slug.py", SLUG_TEST,
                   "cd tools && python3 -m unittest -q test_slug"),
            edit_result("tools/slug.py", "return '-'.join(text.lower().split())"),
        ],
    })
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    planned = orch.plan("A shell greeter and a Python slug helper", test_jobs=1)
    tasks = {t["id"]: t for t in cast(list[dict[str, list[str]]], planned.plan["tasks"])}
    assert all(test.startswith("cmd:") for t in tasks.values() for test in t["tests"])
    prompts = [text for _, key, text in caller.calls if key == "dsh:T-001"]
    assert "prove nothing" in prompts[1]  # the vacuous shard was rejected by the red gate
    run = orch.execute(planned.id)
    assert run.status == "done", orch.gate_failure
    branch = f"orch/{run.id}"
    assert "hello" in Workspace.git(polyglot, "show", f"{branch}:lib/greet.sh")
    assert "join" in Workspace.git(polyglot, "show", f"{branch}:tools/slug.py")
    assert Workspace.git(polyglot, "show", f"{branch}:tests/greet_test.sh") == GREET_TEST.strip()
    listed = Workspace.git(polyglot, "ls-tree", "-r", "--name-only", branch).split()
    assert not any(".orch-tmp" in path for path in listed)


SUITE_CHECK = 'for f in tests/*_test.sh; do sh "$f" || { echo "failed: $f"; exit 1; }; done'


def test_shards_that_break_each_other_once_joined_are_redesigned(polyglot: Path) -> None:
    """Each shard passes the red gate alone; joined, T-001's check also runs T-002's file
    (they share tests/) and fails because of it, so T-002 is redesigned elsewhere."""
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", BOUNDARY)],
        "dsh:T-001": [design("tests/zgreet_test.sh", GREET_TEST, SUITE_CHECK),
                      edit_result("lib/greet.sh", 'greet() { echo "hello $1"; }\n', search="")],
        "dsh:T-002": [
            design("tests/slug_test.sh", "cd tools && python3 -m unittest -q test_slug\n",
                   "sh tests/slug_test.sh"),
            design("tools/test_slug.py", SLUG_TEST, "cd tools && python3 -m unittest -q test_slug"),
            edit_result("tools/slug.py", "return '-'.join(text.lower().split())"),
        ],
    })
    # T-002's first file needs tools/test_slug.py, so it fails on the stubs; joined, it
    # makes T-001's suite-wide check fail too, naming tests/slug_test.sh.
    caller.responses["dsh:T-002"][0] = design(
        "tests/slug_test.sh", 'echo "R2 slug stub"; exit 1\n', "sh tests/slug_test.sh")
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    planned = orch.plan("A shell greeter and a Python slug helper", test_jobs=1)
    prompts = [text for _, key, text in caller.calls if key == "dsh:T-002"]
    assert "breaks another task's frozen check" in prompts[1]
    paths = [f["path"] for f in cast(list[dict[str, str]], planned.plan["files"])]
    assert "tools/test_slug.py" in paths and "tests/slug_test.sh" not in paths
    assert orch.execute(planned.id).status == "done", orch.gate_failure


def test_a_person_finishes_a_parked_task_and_the_run_completes(polyglot: Path) -> None:
    """The builder keeps failing T-001 until it parks; a hint retries it, then a person
    takes it over, a wrong edit is refused by the frozen check, a right one merges, and the
    run finishes the remaining task."""
    from tests.conftest import EMPTY_REPLAN, THICK

    wrong = edit_result("lib/greet.sh", 'greet() { echo "hi $1"; }\n', search="")
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", BOUNDARY)],
        "dsh:T-001": [design("tests/greet_test.sh", GREET_TEST, "sh tests/greet_test.sh"),
                      wrong, wrong, wrong, wrong, wrong, wrong],
        "claude:T-001": [wrong, THICK, EMPTY_REPLAN, wrong, THICK, EMPTY_REPLAN],
        "dsh:T-002": [design("tools/test_slug.py", SLUG_TEST,
                             "cd tools && python3 -m unittest -q test_slug"),
                      edit_result("tools/slug.py", "return '-'.join(text.lower().split())")],
    })
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    run = orch.execute(orch.plan("greeter and slug", test_jobs=1).id)
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
    (path / "tests" / "greet_test.sh").write_text("true\n")  # frozen: refused
    merged, report = orch.submit(run.id, "T-001")
    assert not merged and "outside" in report
    Workspace.git(path, "checkout", "--", "tests/greet_test.sh")
    merged, report = orch.submit(run.id, "T-001")  # still the wrong greeting
    assert not merged and "FAIL" in report
    (path / "lib" / "greet.sh").write_text('greet() { echo "hello $1"; }\n')
    merged, report = orch.submit(run.id, "T-001")
    assert merged, report
    finished = orch.execute(run.id)
    assert finished.status == "done", orch.gate_failure
    branch = f"orch/{run.id}"
    assert "hello" in Workspace.git(polyglot, "show", f"{branch}:lib/greet.sh")
    subjects = Workspace.git(polyglot, "log", "--format=%s", branch).splitlines()
    assert "orch T-001: finished by a person" in subjects


def test_a_task_that_owns_no_files_is_decided_by_its_checks_alone(polyglot: Path) -> None:
    """A cross-component agreement check owns no files; run r7313527a parked one after
    five rungs of builders correctly returning no edits."""
    tasks = cast(list[dict[str, object]], BOUNDARY["tasks"])
    boundary = {
        **BOUNDARY,
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
        "plan": [NativeResult("ok", boundary)],
        "dsh:T-001": [
            design("tests/greet_test.sh", GREET_TEST, "sh tests/greet_test.sh"),
            edit_result("lib/greet.sh", 'greet() { echo "hello $1"; }\n', search=""),
        ],
        "dsh:T-002": [
            design("tools/test_slug.py", SLUG_TEST,
                   "cd tools && python3 -m unittest -q test_slug"),
            edit_result("tools/slug.py", "return '-'.join(text.lower().split())"),
        ],
        "dsh:T-003": [design("tests/agree_test.sh", agree, "sh tests/agree_test.sh"),
                      # the join's independent integration shard
                      design("tests/agree_i_test.sh", agree, "sh tests/agree_i_test.sh")],
    })
    orch = Orchestrator(polyglot, caller, local_runner, notify=lambda _: None)
    planned = orch.plan("greeter, slug and their agreement", test_jobs=1)
    run = orch.execute(planned.id)
    assert run.status == "done", [(t.id, t.status, t.reason)
                                  for t in orch.state.tasks(run.id)]
    assert orch.state.task(run.id, "T-003").status == "DONE"
    assert [key for _, key, _ in caller.calls].count("dsh:T-003") == 2  # test design only
