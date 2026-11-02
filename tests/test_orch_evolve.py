import copy
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

import pytest

from orchestragent.native import NativeResult
from orchestragent.orch_cli import main
from orchestragent.orch_index import Coupling
from orchestragent.orch_plan import coupled_splits, lint_lessons, plan_score
from orchestragent.orch_rules import Rule, planner_verdict, spearman
from orchestragent.orch_run import Orchestrator
from orchestragent.orch_state import OrchEvent
from orchestragent.workspace import Workspace
from tests.conftest import GOOD_ADD, GOOD_DOUBLE, MATH_TESTS, Clock, ScriptedCaller, local_runner

Plan = dict[str, object]
TESTS_LESSON = "Write at least one frozen test for every requirement, including its failure cases."


@pytest.mark.parametrize(
    ("error", "lesson"),
    [
        ("R3: no test proves it", TESTS_LESSON),
        ("T-002: doubler.py imports adder.py, owned by T-001; add T-001 to depends_on",
         "When a stub imports another task's module, list that task in depends_on."),
        ("files: string.py would shadow the standard library module 'string'",
         "Never name a module or package after a standard-library module."),
        ("tasks: bad or duplicate id 'X'", None),
    ],
)
def test_lint_errors_teach_fixed_planner_lessons(error: str, lesson: str | None) -> None:
    assert lint_lessons([error]) == ([lesson] if lesson else [])


def tasked(plan: Plan, **changes: object) -> Plan:
    copied = copy.deepcopy(plan)
    for task in cast(list[dict[str, object]], copied["tasks"]):
        task.update(changes)
    return copied


def test_plan_score_rewards_parallel_tested_plans_and_penalizes_waste(math_plan: Plan) -> None:
    chain, _ = plan_score(math_plan, 0, 0, 0)
    wide, parts = plan_score(tasked(math_plan, depends_on=[]), 0, 0, 0)
    assert (chain, wide, parts["parallelism"]) == (0.504, 0.592, 0.5)
    assert plan_score(math_plan, 1, 0, 0)[0] == 0.354
    assert plan_score(math_plan, 0, 1, 1)[0] == 0.304


def test_coupled_files_split_across_independent_tasks_are_reported(math_plan: Plan) -> None:
    coupling = [Coupling("adder.py", "doubler.py", 4, 0.8), Coupling("a.py", "b.py", 9, 1.0)]
    assert coupled_splits(math_plan, coupling) == []  # T-002 depends on T-001
    assert coupled_splits(tasked(math_plan, depends_on=[]), coupling) == [
        "adder.py (T-001) and doubler.py (T-002)"
    ]
    weak = [Coupling("adder.py", "doubler.py", 2, 0.4)]
    assert coupled_splits(tasked(math_plan, depends_on=[]), weak) == []


@pytest.mark.parametrize(
    ("xs", "ys", "rho"),
    [([1, 2, 3], [10, 20, 30], 1.0), ([1, 2, 3], [3, 2, 1], -1.0),
     ([1, 1, 2], [1, 2, 3], 0.866), ([1, 1, 1], [1, 2, 3], 0.0)],
)
def test_spearman_handles_ties_and_constants(xs: list[float], ys: list[float], rho: float) -> None:
    assert round(spearman(xs, ys), 3) == rho


def test_planner_rules_use_the_proxy_only_while_it_is_trusted() -> None:
    rule = Rule("p1", "x", "s", 0.0, kind="planner", with_passed=1, with_total=6,
                without_passed=4, without_total=6, proxy_with_passed=6, proxy_with_total=6,
                proxy_without_passed=2, proxy_without_total=6)
    assert planner_verdict(rule, True) == ("active", "promoted: with 6/6, without 2/6 (proxy)")
    assert planner_verdict(rule, False) == ("retired", "harmful: with 1/6, without 4/6")


@dataclass
class Planner:
    """Answers plan calls in order, or per sample when the run id names one."""

    plans: list[Plan]
    calls: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, provider: str, model: str, prompt: str, schema: dict[str, object],
                 run_id: str, cancelled: Callable[[], bool], *, effort: str = "") -> NativeResult:
        self.calls.append((run_id, prompt))
        sample = re.search(r"-plan-s([0-9])-", run_id)
        if sample:
            return NativeResult("ok", self.plans[int(sample.group(1)) - 1])
        return NativeResult("ok", self.plans.pop(0))


def planner(*plans: Plan) -> Planner:
    return Planner(list(plans))


def kinds(orchestrator: Orchestrator, run_id: str, kind: str) -> list[OrchEvent]:
    return [event for event in orchestrator.state.events(run_id) if event.kind == kind]


def test_lint_mistakes_become_planner_candidates_that_are_trialled_then_promoted(
    orch_repo: Path, math_plan: Plan
) -> None:
    broken = copy.deepcopy(math_plan)
    cast(list[object], broken["requirements"]).append({"id": "R3", "text": "x"})
    call = planner(broken, math_plan, math_plan, math_plan)
    orchestrator = Orchestrator(orch_repo, call, local_runner, notify=lambda line: None)
    first = orchestrator.plan("spec")
    learned = orchestrator.state.rules("planner")
    proxy = kinds(orchestrator, first.id, "plan.proxy")[0].data
    assert [(r.id, r.text, r.status) for r in learned] == [
        ("p1", TESTS_LESSON, "candidate"),
        ("p2", "Assign every requirement to at least one task.", "candidate"),
    ]
    assert (proxy["passed"], proxy["repairs"], proxy["rule"]) == (False, 1, "")

    second = orchestrator.plan("spec")
    trial = kinds(orchestrator, second.id, "plan.proxy")[0].data
    prompt = call.calls[-1][1]
    assert trial["rule"] == "p1" and trial["passed"] is True
    assert ("EXTRA PLANNING RULE (on trial):\n- " + TESTS_LESSON in prompt) is trial["shown"]

    rule = orchestrator.state.rules("planner")[0]
    orchestrator.state.save_rule(replace(rule, proxy_with_passed=6, proxy_with_total=6,
                                         proxy_without_passed=2, proxy_without_total=6))
    third = orchestrator.plan("spec")
    message = Workspace.git(orch_repo, "log", "-1", "--format=%B", third.checkpoint)
    assert orchestrator.state.rules("planner")[0].status == "active"
    assert "LEARNED PLANNING RULES:\n- " + TESTS_LESSON in call.calls[-1][1]
    assert "Orch-Rules: p1" in message


def test_parallel_samples_are_scored_and_the_best_plan_wins(
    orch_repo: Path, math_plan: Plan
) -> None:
    vacuous = copy.deepcopy(math_plan)
    files = cast(list[dict[str, str]], vacuous["files"])
    files[0]["content"] = "def add(a: int, b: int) -> int:\n    return a + b\n"
    call = planner(vacuous, math_plan)
    orchestrator = Orchestrator(orch_repo, call, local_runner, notify=lambda line: None)
    run = orchestrator.plan("spec", samples=2)
    picked = kinds(orchestrator, run.id, "plan.samples")[0].data
    prompts = [prompt for _, prompt in call.calls]
    assert picked == {"scores": [0.354, 0.504], "chosen": 2}
    assert "raise NotImplementedError" in (
        Workspace.git(orch_repo, "show", f"{run.checkpoint}:adder.py")
    )
    assert sum("APPROACH:\nPrefer many small tasks" in prompt for prompt in prompts) == 1
    calls = orchestrator.state.usage_calls(run.id)
    assert {(c.scope, c.sample) for c in calls} == {("planning", "s1"), ("planning", "s2")}


SPLIT = NativeResult("ok", {
    "summary": "split", "lesson": "Give each module that another task imports its own task.",
    "tasks": [
        {"id": "T-003", "goal": "add", "reqs": ["R1"], "files": ["adder.py"], "depends_on": [],
         "tests": ["test_mathops.AddTest.test_r1_adds"], "contract": "a + b"},
        {"id": "T-004", "goal": "double", "reqs": ["R2"], "files": ["doubler.py"],
         "depends_on": ["T-003"], "tests": ["test_mathops.DoubleTest.test_r2_doubles"],
         "contract": "add(a, a)"},
    ],
})
BLOCKED = NativeResult("ok", {"status": "BLOCKED", "summary": "", "reason": "needs doubler.py",
                              "edits": []})


def test_replans_count_as_real_planning_failures_and_teach_a_lesson(
    orch_repo: Path, math_plan: Plan
) -> None:
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", math_plan)], "dsh:T-001": [BLOCKED], "claude:T-001": [SPLIT],
        "dsh:T-003": [GOOD_ADD], "dsh:T-004": [GOOD_DOUBLE],
    })
    orchestrator = Orchestrator(orch_repo, caller, local_runner, notify=lambda line: None,
                                clock=Clock())
    run = orchestrator.execute(orchestrator.plan("spec").id)
    outcome = kinds(orchestrator, run.id, "plan.outcome")[0].data
    assert run.status == "done"
    assert (outcome["blocked_rate"], outcome["failures"], outcome["passed"]) == (
        1.0, ["T-001", "T-002"], False
    )
    assert [r.text for r in orchestrator.state.rules("planner")] == [
        "Give each module that another task imports its own task."
    ]
    orchestrator.execute(run.id)  # a re-run records no second outcome
    assert len(kinds(orchestrator, run.id, "plan.outcome")) == 1


def test_proxy_that_disagrees_with_real_outcomes_loses_trust(
    orch_repo: Path, math_plan: Plan
) -> None:
    orchestrator = Orchestrator(orch_repo, planner(math_plan), local_runner,
                                notify=lambda line: None)
    template = orchestrator.plan("spec")
    for number in range(6):
        run = replace(template, id=f"rx{number}")
        orchestrator.state.create_run(run, [])
        # Higher proxy score, worse build: the proxy is anti-correlated with reality.
        orchestrator.state.plan_outcome(run.id, "", False, True, "proxy", {"score": number})
        orchestrator.state.plan_outcome(run.id, "", False, False, "outcome",
                                        {"blocked_rate": number / 5})
    history = orchestrator.state.planning_history()
    assert len(history) == 6 and orchestrator.proxy_trust() == (False, -1.0, 6)
    assert [run.id for run in orchestrator.state.runs()][:2] == ["rx5", "rx4"]


def test_evolve_replays_past_specs_with_and_without_the_candidate(
    orch_repo: Path, math_plan: Plan, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = copy.deepcopy(math_plan)
    cast(list[dict[str, str]], broken["files"])[2]["content"] = MATH_TESTS.replace(
        "test_r1_adds", "test_adds"
    )
    call = planner(math_plan, broken, math_plan, math_plan)
    orchestrator = Orchestrator(orch_repo, call, local_runner, notify=lambda line: None)
    past = orchestrator.plan("Build add and double.")
    orchestrator.state.add_rule(TESTS_LESSON, "seed", "planner")
    lines = orchestrator.evolve()
    rule = orchestrator.state.rules("planner")[0]
    replay = [event.data for event in kinds(orchestrator, past.id, "plan.replay")]
    assert lines[1:] == [
        f"  {past.id}: with fail 0.40, without ok 0.50",
        "p1 proxy with 0/1, without 1/1: not decided yet",
    ]
    assert [(r["shown"], r["passed"], r["repairs"]) for r in replay] == [
        (True, False, 1), (False, True, 0)
    ]
    assert (rule.proxy_with_total, rule.proxy_without_passed, rule.status) == (1, 1, "candidate")
    calls = [c for c in orchestrator.state.usage_calls(past.id) if c.scope == "evolve"]
    assert sorted((c.sample, c.attempt, c.rules) for c in calls) == [
        ("eo", 1, []), ("ew", 1, [rule.id]), ("ew", 2, [rule.id]),
    ]
    assert main(["--repo", str(orch_repo), "evolve", "--check"]) == 0
    assert capsys.readouterr().out.startswith(
        "planner proxy vs real outcome over 0 run(s): needs 5 runs, trusted"
    )


def test_plan_leaves_no_worktrees_and_evolve_skips_a_running_run(
    orch_repo: Path, math_plan: Plan
) -> None:
    orchestrator = Orchestrator(orch_repo, planner(math_plan), local_runner,
                                notify=lambda line: None)
    past = orchestrator.plan("spec")
    listed = Workspace.git(orch_repo, "worktree", "list").splitlines()
    orchestrator.state.add_rule(TESTS_LESSON, "seed", "planner")
    with orchestrator.exclusive(past.id):
        lines = orchestrator.evolve()
    assert len(listed) == 2  # the checkout and the run's integration tree
    assert lines[1] == f"  {past.id}: skipped (run {past.id} is already executing)"


@pytest.mark.parametrize(("seed", "expected"), [(False, "no planner candidates"),
                                                (True, "no past runs")])
def test_evolve_explains_when_there_is_nothing_to_replay(
    orch_repo: Path, seed: bool, expected: str
) -> None:
    orchestrator = Orchestrator(orch_repo, planner(), local_runner, notify=lambda line: None)
    if seed:
        orchestrator.state.add_rule(TESTS_LESSON, "seed", "planner")
    assert orchestrator.evolve() == [f"nothing to replay: {expected}"]
