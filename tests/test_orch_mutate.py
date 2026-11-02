import time
from pathlib import Path
from typing import cast

import pytest

from orchestragent.live_workspace import LiveWorkspace
from orchestragent.native import NativeResult
from orchestragent.orch_build import SPEC_AUTHORITY
from orchestragent.orch_cli import main
from orchestragent.orch_mutate import PLANNER_LESSONS, Report, mutants, select
from orchestragent.orch_run import Orchestrator
from orchestragent.orch_state import Run
from orchestragent.platform import sandbox_available
from orchestragent.workspace import Workspace
from tests.conftest import Clock, ScriptedCaller, edit_result, local_runner

AGE_STUB = (
    'def is_adult(age: int) -> bool:\n    """True from 18 on."""\n    raise NotImplementedError\n'
)
AGE_TESTS = (
    "import unittest\n\nfrom age import is_adult\n\n\nclass AgeTest(unittest.TestCase):\n"
    "    def test_r1_adult(self) -> None:\n        self.assertTrue(is_adult(30))\n"
    "        self.assertFalse(is_adult(5))\n"
)
GOOD_AGE = edit_result("age.py", "return age >= 18")
LAST = "        self.assertFalse(is_adult(5))\n"


def body(*lines: str) -> str:
    return "def f(x: int, y: int) -> int:\n" + "".join(f"    {line}\n" for line in lines)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (body("return x + y"), [("RET", "return None"), ("AOR", "((x) - (y))")]),
        (body("return x <= y"), [("RET", "return None"), ("ROR", "(x) < (y)")]),
        (body("return x and not y"),
         [("RET", "return None"), ("LCR", "(x) or (not y)"), ("UOI", "(y)")]),
        (body("if x:", "    y += 2", "return -y"),
         [("COND", "not (x)"), ("AUG", "y -= 2"), ("CRP", "3"), ("RET", "return None"),
          ("UOI", "(y)")]),
        (body("while x:", "    break", "return 0"),
         [("COND", "not (x)"), ("JMP", "continue"), ("RET", "return None"), ("CRP", "1")]),
        (body('"""Doc."""', 'return f"{x + 1}" + "é" + str(x * 2)'),
         [("RET", "return None"), ("AOR", '((f"{x + 1}" + "é") - (str(x * 2)))'),
          ("AOR", '((f"{x + 1}") - ("é"))'), ("CRP", "''"), ("AOR", "((x) / (2))"),
          ("CRP", "3")]),
        (body("def g() -> int:", '    """Inner."""', "    return 1", "return y / x ** 2"),
         [("RET", "return None"), ("CRP", "0"), ("RET", "return None"),
          ("AOR", "((y) * (x ** 2))"), ("AOR", "((x) * (2))"), ("CRP", "3")]),
        (body("raise NotImplementedError"), []),
    ],
    ids=["arithmetic", "relational", "logical", "condition-augassign", "loop-jump",
         "skips-docstring-fstring-utf8", "nested-docstring-and-power", "stub"],
)
def test_mutants_change_exactly_one_construct(
    source: str, expected: list[tuple[str, str]]
) -> None:
    found = mutants("m.py", source)
    assert [(m.operator, m.after) for m in found] == expected
    for mutant in found:  # everything outside the spliced span is byte-identical
        assert mutant.source.replace(mutant.after, mutant.before, 1) == source


def test_selection_spreads_over_functions_and_is_stable() -> None:
    source = body("return x + y") + body("return x - y").replace("def f", "def g")
    found = mutants("m.py", source)
    chosen = select(found, 2, "T-001")
    assert {m.symbol for m in chosen} == {"f", "g"}
    assert chosen == select(found, 2, "T-001") and len(select(found, 99, "x")) == len(found)


def test_report_scores_and_names_the_test_gaps() -> None:
    report = Report("T-001", 4, 1, 0, [{"operator": "ROR"}, {"operator": "ROR"},
                                       {"operator": "CRP"}])
    assert report.score == 0.25
    assert report.gaps() == [
        "boundaries are untested: no case sits exactly on a comparison's limit",
        "constants are untested: an off-by-one or changed literal goes unnoticed",
    ]


@pytest.fixture
def age_plan() -> dict[str, object]:
    return {
        "summary": "ages",
        "requirements": [{"id": "R1", "text": "adults are 18 or older"}],
        "files": [{"path": "age.py", "content": AGE_STUB},
                  {"path": "test_age.py", "content": AGE_TESTS}],
        "tasks": [{"id": "T-001", "goal": "is_adult", "reqs": ["R1"], "files": ["age.py"],
                   "depends_on": [], "tests": ["test_age.AgeTest.test_r1_adult"],
                   "contract": "age >= 18"}],
    }


@pytest.fixture
def age_caller(age_plan: dict[str, object]) -> ScriptedCaller:
    return ScriptedCaller({"plan": [NativeResult("ok", age_plan)], "dsh:T-001": [GOOD_AGE]})


@pytest.fixture
def age_orchestrator(orch_repo: Path, age_caller: ScriptedCaller) -> Orchestrator:
    return Orchestrator(orch_repo, age_caller, local_runner, notify=lambda line: None,
                        clock=Clock())


@pytest.fixture
def weak(age_orchestrator: Orchestrator) -> Run:
    """A merged task whose tests miss the boundary: only `return None` is killed."""
    age_orchestrator.state.add_rule("Check the boundary value itself before anything else.", "s")
    return age_orchestrator.execute(age_orchestrator.plan("spec").id, mutants=12)


def test_end_of_run_scoring_flags_weak_tests_and_withdraws_their_ab_evidence(
    age_orchestrator: Orchestrator, weak: Run
) -> None:
    events = age_orchestrator.state.events(weak.id)
    scored = next(e.data for e in events if e.kind == "task.mutation")
    task = age_orchestrator.state.task(weak.id, "T-001")
    [rule] = age_orchestrator.state.rules("builder")
    planner = age_orchestrator.state.rules("planner")
    assert (weak.status, task.mutation, scored["total"], scored["killed"]) == (
        "done", 0.333, 3, 1
    )
    assert [s["after"] for s in cast(list[dict[str, object]], scored["survivors"])] == [
        "(age) > (18)", "19"
    ]
    assert [e.kind for e in events if e.kind in {"rule.trial", "rule.discounted"}] == [
        "rule.trial", "rule.discounted"
    ]
    assert rule.with_total + rule.without_total == 0
    assert next(e.data for e in events if e.kind == "plan.weak_tests")["rules"] == ["r1"]
    # The survivors teach the planner how to write stronger tests next time.
    assert [(r.id, r.text) for r in planner] == [
        ("p1", PLANNER_LESSONS["CRP"]), ("p2", PLANNER_LESSONS["ROR"])
    ]


BOUNDARY = (
    "\n    def test_r1_boundary(self) -> None:\n"
    "        self.assertTrue(is_adult(18))\n        self.assertFalse(is_adult(17))\n"
)
HARDENED = NativeResult("ok", {"summary": "boundary", "edits": [
    {"path": "test_age.py", "search": LAST, "replace": LAST + BOUNDARY}
]})


def test_harden_freezes_new_tests_that_kill_survivors(
    age_orchestrator: Orchestrator, age_caller: ScriptedCaller, weak: Run, orch_repo: Path
) -> None:
    age_caller.responses["claude:T-001"] = [HARDENED]
    lines = age_orchestrator.harden(weak.id)
    task = age_orchestrator.state.task(weak.id, "T-001")
    head = Workspace.git(orch_repo, "rev-parse", weak.branch)
    assert lines == ["T-001: 1 new test(s) kill 2 survivor(s); mutation 0.33 -> 1.00"]
    assert task.tests == ["test_age.AgeTest.test_r1_adult", "test_age.AgeTest.test_r1_boundary"]
    assert Workspace.git(orch_repo, "rev-parse", f"orch/{weak.id}/done") == head
    assert Workspace.git(orch_repo, "log", "-1", "--format=%s", head) == (
        "orch harden T-001: 1 test(s) for surviving mutants"
    )
    files = cast(list[dict[str, str]], age_orchestrator.state.run(weak.id).plan["files"])
    assert "def test_r1_boundary" in files[1]["content"]
    calls = [c for c in age_orchestrator.state.usage_calls(weak.id) if c.scope == "harden"]
    assert [(c.task_id, c.purpose) for c in calls] == [("T-001", "harden")]
    prompt = next(prompt for _, _, prompt in age_caller.calls if "## SURVIVING MUTANTS" in prompt)
    assert weak.spec in prompt and SPEC_AUTHORITY in prompt


def added(test: str) -> NativeResult:
    return NativeResult("ok", {"summary": "", "edits": [
        {"path": "test_age.py", "search": LAST, "replace": LAST + test}
    ]})


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (NativeResult("ok", {"summary": "", "edits": [
            {"path": "test_age.py", "search": "test_r1_adult", "replace": "test_r1_grown"}]}),
         "existing tests were removed or renamed"),
        (added("\n    def test_edge(self) -> None:\n        self.assertTrue(is_adult(18))\n"),
         "is not named for one of ['R1']"),
        (added("\n    def test_r1_wrong(self) -> None:\n        self.assertTrue(is_adult(17))\n"),
         "new tests fail on the merged code"),
        (added("\n    def test_r1_more(self) -> None:\n        self.assertTrue(is_adult(40))\n"),
         "kill none of the surviving mutants"),
        (NativeResult("ok", {"summary": "", "edits": [
            {"path": "age.py", "search": "18", "replace": "17"}]}),
         "edit outside owned files: age.py"),
        (NativeResult("ok", {"summary": "", "edits": [
            {"path": "test_age.py", "search": "is_adult(30)", "replace": "is_adult(18)"}]}),
         "existing tests changed: ['AgeTest.test_r1_adult']"),
    ],
    ids=["renames", "unmarked", "fails-on-code", "kills-nothing", "edits-code",
         "rewrites-existing"],
)
def test_harden_rejects_tests_that_are_not_proven_useful(
    age_orchestrator: Orchestrator, age_caller: ScriptedCaller, weak: Run, orch_repo: Path,
    answer: NativeResult, reason: str,
) -> None:
    before = Workspace.git(orch_repo, "rev-parse", weak.branch)
    age_caller.responses["claude:T-001"] = [answer]
    [line] = age_orchestrator.harden(weak.id)
    assert line.startswith("T-001: harden rejected: ") and reason in line
    assert Workspace.git(orch_repo, "rev-parse", weak.branch) == before
    assert not Workspace.git(age_orchestrator.integration(weak.id), "status", "--porcelain")
    assert age_orchestrator.state.task(weak.id, "T-001").tests == ["test_age.AgeTest.test_r1_adult"]


def test_harden_rejects_tests_that_only_pass_next_to_other_tests(
    age_orchestrator: Orchestrator, age_caller: ScriptedCaller, weak: Run, orch_repo: Path
) -> None:
    def runner(execution: Path, scratch: Path, revision: str, names: list[str] | None,
               *, timeout: float = 25.0) -> tuple[bool, str]:
        alone = names == ["test_age.AgeTest.test_r1_boundary"]
        if alone and "return age >= 18" in (execution / "age.py").read_text():
            return False, "FAIL: needs state from another test"
        return local_runner(execution, scratch, revision, names)

    age_orchestrator.runner = runner
    age_caller.responses["claude:T-001"] = [HARDENED]
    [line] = age_orchestrator.harden(weak.id)
    assert "new tests fail when run alone" in line


def test_an_interrupted_harden_leaves_nothing_on_the_branch(
    age_orchestrator: Orchestrator, age_caller: ScriptedCaller, weak: Run, orch_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted(*args: object) -> int:
        raise KeyboardInterrupt

    before = Workspace.git(orch_repo, "rev-parse", weak.branch)
    monkeypatch.setattr(age_orchestrator, "_validate_hardening", interrupted)
    age_caller.responses["claude:T-001"] = [HARDENED]
    with pytest.raises(KeyboardInterrupt):
        age_orchestrator.harden(weak.id)
    assert Workspace.git(orch_repo, "rev-parse", weak.branch) == before
    assert not Workspace.git(age_orchestrator.integration(weak.id), "status", "--porcelain")


def test_mutation_scores_appear_in_status_report_and_mutate_command(
    age_orchestrator: Orchestrator, weak: Run, orch_repo: Path,
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(LiveWorkspace, "run_unittests", staticmethod(local_runner))
    capsys.readouterr()
    assert main(["--repo", str(orch_repo), "mutate", "--run", weak.id]) == 0
    printed = capsys.readouterr().out.splitlines()
    out = printed[next(i for i, line in enumerate(printed) if line.startswith("mutation")):]
    assert main(["--repo", str(orch_repo), "status", weak.id]) == 0
    status = capsys.readouterr().out
    assert out[0] == "mutation score 0.33 (1/3 mutants killed across 1 task(s))"
    assert out[1].startswith("  T-001 0.33: constants are untested") and "boundaries" in out[1]
    assert out[-1] == f"  add tests that kill the survivors: orch harden --run {weak.id}"
    assert "rung=0 mut=0.33" in status


@pytest.mark.skipif(
    not sandbox_available(),
    reason="needs sandbox-exec or bwrap",
)
def test_sandbox_runner_honours_a_short_timeout(tmp_path: Path) -> None:
    (tmp_path / "test_loop.py").write_text(
        "import unittest\n\n\nclass LoopTest(unittest.TestCase):\n"
        "    def test_r1_loops(self) -> None:\n        while True:\n            pass\n"
    )
    started = time.monotonic()
    passed, report = LiveWorkspace.run_unittests(
        tmp_path, tmp_path, "t", ["test_loop.LoopTest.test_r1_loops"], timeout=2
    )
    assert not passed and time.monotonic() - started < 10
