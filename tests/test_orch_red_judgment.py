"""A designed check must fail on the stubs, and for the right reason: the tests ran and
found the behaviour missing, not a runner that broke before any test ran. A model reads
the red output once per design round; code only applies its verdict."""

from pathlib import Path

import pytest

from orchestragent import orch_run
from orchestragent.native import NativeResult
from orchestragent.orch_run import Orchestrator
from orchestragent.orch_thin import red_verdicts
from tests.conftest import (
    ADD_CHECK,
    ADD_SHARD,
    DOUBLE_SHARD,
    GOOD_ADD,
    GOOD_DOUBLE,
    Clock,
    component_design,
    local_runner,
    scripted,
)

BROKEN_RUNNER = ("T-001", "test_add.py", "import unittest\n", "python3 -m unittest -q tests_absent")


@pytest.fixture(autouse=True)
def _judgment_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch_run, "RED_JUDGMENT", True)


def judged(*verdicts: tuple[str, str]) -> NativeResult:
    return NativeResult("ok", {"checks": [
        {"check": check, "verdict": "runner_failed", "reason": reason}
        for check, reason in verdicts]})


def test_a_check_red_for_the_wrong_reason_is_sent_back_to_its_designer(
    orch_repo: Path, math_plan: dict[str, object], clock: Clock, unsandboxed: None,
) -> None:
    """The first design's T-001 command names a module that does not exist: red on the
    stubs, and red on any implementation. The judgment sends it back with the reason; the
    resent shard is judged again and accepted."""
    caller = scripted(math_plan, designs={"dsh:design:root": [
        component_design(BROKEN_RUNNER, DOUBLE_SHARD), component_design(ADD_SHARD)]})
    caller.responses["dsh:red"] = [
        judged(("T-001", "ModuleNotFoundError: No module named 'tests_absent'")),
        NativeResult("ok", {"checks": []})]
    caller.responses.update({"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    orch = Orchestrator(orch_repo, caller, local_runner, notify=lambda _: None, clock=clock)
    run = orch.plan("Build add and double.")
    judgments = [text for _, key, text in caller.calls if key == "dsh:red"]
    assert len(judgments) == 2
    assert "## CHECK T-001" in judgments[0] and "## CHECK T-002" in judgments[0]
    assert "tests_absent" in judgments[0]  # the red output itself is shown
    assert "## CHECK T-001" in judgments[1] and "## CHECK T-002" not in judgments[1]
    designs = [text for _, key, text in caller.calls if key == "dsh:design:root"]
    assert "red for the wrong reason" in designs[1] and "tests_absent" in designs[1]
    assert orch.state.task(run.id, "T-001").tests == [ADD_CHECK.id]
    assert orch.execute(run.id).status == "done"


def test_a_failed_judgment_rejects_nothing(
    orch_repo: Path, math_plan: dict[str, object], clock: Clock, unsandboxed: None,
) -> None:
    caller = scripted(math_plan)
    caller.responses["dsh:red"] = [NativeResult("error", {}, message="provider error")]
    orch = Orchestrator(orch_repo, caller, local_runner, notify=lambda _: None, clock=clock)
    run = orch.plan("Build add and double.")
    assert [key for _, key, _ in caller.calls].count("dsh:design:root") == 1
    assert len(orch.state.task(run.id, "T-001").tests) == 1


def test_verdicts_only_ever_reject_checks_they_name() -> None:
    labels = ["T-001", "T-002"]
    assert red_verdicts({"checks": [{"check": "T-001", "verdict": "runner_failed",
                                     "reason": "no classpath"},
                                    {"check": "T-002", "verdict": "tests_ran", "reason": ""},
                                    {"check": "T-999", "verdict": "runner_failed",
                                     "reason": "x"}]}, labels) == {"T-001": "no classpath"}
    for malformed in (None, [], {"checks": "x"}, {"checks": [{"verdict": "runner_failed"}]}):
        assert red_verdicts(malformed, labels) == {}


def test_the_judgment_sees_the_stub_files_and_a_repeat_rejection_goes_to_the_planner(
    orch_repo: Path, math_plan: dict[str, object], clock: Clock, unsandboxed: None,
) -> None:
    """Tessera: a stub Makefile did not build the library its tests link, and the cheap
    judgment called that a broken runner twice, ending planning. The judgment is told which
    files are stubs, and a second rejection of the same task's check is the planner's call;
    here the planner rules the tests ran, and the design is accepted."""
    caller = scripted(math_plan, designs={"dsh:design:root": [
        component_design(ADD_SHARD, DOUBLE_SHARD), component_design(ADD_SHARD)]})
    caller.responses["dsh:red"] = [judged(("T-001", "cannot find build/libadder.a")),
                                   judged(("T-001", "cannot find build/libadder.a"))]
    caller.responses["claude:red"] = [NativeResult("ok", {"checks": [
        {"check": "T-001", "verdict": "tests_ran", "reason": "adder.py is a stub"}]})]
    caller.responses.update({"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    orch = Orchestrator(orch_repo, caller, local_runner, notify=lambda _: None, clock=clock)
    run = orch.plan("Build add and double.")
    cheap = [text for _, key, text in caller.calls if key == "dsh:red"]
    assert "IMPLEMENTATION FILES" in cheap[0] and "adder.py" in cheap[0]
    final = [text for _, key, text in caller.calls if key == "claude:red"]
    assert len(cheap) == 2 and len(final) == 1 and "## CHECK T-001" in final[0]
    assert orch.state.task(run.id, "T-001").tests == [ADD_CHECK.id]
