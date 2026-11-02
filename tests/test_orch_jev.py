from pathlib import Path
from typing import cast

import pytest

from orchestragent.jev import Decision
from orchestragent.orch_run import Orchestrator
from orchestragent.orch_state import OrchEvent
from tests.conftest import (
    BAD_ADD,
    GOOD_ADD,
    GOOD_DOUBLE,
    Clock,
    ScriptedCaller,
    ScriptedJev,
    cheat,
    local_runner,
    rung,
    scripted,
)


@pytest.fixture(autouse=True)
def local_red(unsandboxed: None) -> None:
    """Every test here plans: the red gate runs without the OS sandbox."""


def run_with(
    orch_repo: Path, caller: ScriptedCaller, jev: ScriptedJev, mode: str, clock: Clock
) -> Orchestrator:
    orchestrator = Orchestrator(
        orch_repo, caller, local_runner, notify=lambda line: None, clock=clock,
        decide=jev, jev_mode=mode,
    )
    orchestrator.execute(orchestrator.plan("spec").id)
    return orchestrator


def events(orchestrator: Orchestrator, kind: str) -> list[OrchEvent]:
    run = orchestrator.state.latest()
    return [event for event in orchestrator.state.events(run.id) if event.kind == kind]


@pytest.mark.parametrize(
    ("mode", "answer", "routes", "applied"),
    [
        ("on", rung("claude_patch", 0.9), ["dsh", "claude", "dsh"], True),
        ("shadow", rung("claude_patch", 0.9), ["dsh", "dsh", "dsh"], False),
        ("on", rung("claude_patch", 0.4), ["dsh", "dsh", "dsh"], False),
    ],
    ids=["applied", "shadow-records-only", "low-confidence-falls-back"],
)
def test_jev_triage_picks_the_next_rung_only_when_confident_and_on(
    orch_repo: Path, math_plan: dict[str, object], clock: Clock,
    mode: str, answer: Decision, routes: list[str], applied: bool,
) -> None:
    caller = scripted(math_plan, {"dsh:T-001": [BAD_ADD, GOOD_ADD],
                                  "claude:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    jev = ScriptedJev(triage=[answer])
    orchestrator = run_with(orch_repo, caller, jev, mode, clock)
    [triage] = events(orchestrator, "jev.triage")
    assert orchestrator.state.latest().status == "done"
    # after the planner and the two test designers
    assert [key.split(":")[0] for key in caller.keys()[3:]] == routes
    assert (triage.data["deterministic"], triage.data["applied"]) == (1, applied)
    assert set(cast(dict[str, object], jev.states[0])) == {
        "goal", "contract", "failure", "error", "diff"
    }


def test_pre_screen_rejects_a_diff_flagged_as_special_casing(
    orch_repo: Path, math_plan: dict[str, object], clock: Clock
) -> None:
    caller = scripted(math_plan, {"dsh:T-001": [GOOD_ADD, GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    jev = ScriptedJev(triage=[rung("retry", 0.3)], screen=[cheat(0.95)])
    orchestrator = run_with(orch_repo, caller, jev, "on", clock)
    failed = [e.data["category"] for e in events(orchestrator, "task.failed")]
    screens = [(e.data["flagged"], e.data["applied"]) for e in events(orchestrator, "jev.screen")]
    assert (orchestrator.state.latest().status, failed) == ("done", ["suspect"])
    assert screens == [(True, True), (False, False), (False, False)]


def test_unavailable_jev_switches_itself_off_and_the_rules_continue(
    orch_repo: Path, math_plan: dict[str, object], clock: Clock
) -> None:
    caller = scripted(math_plan, {"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    jev = ScriptedJev(screen=[Decision("quota_exhausted", message="budget")])
    orchestrator = run_with(orch_repo, caller, jev, "on", clock)
    calls = [e.data["outcome"] for e in events(orchestrator, "jev.call")]
    assert (orchestrator.state.latest().status, orchestrator.jev_mode) == ("done", "off")
    assert calls == ["quota_exhausted"]
