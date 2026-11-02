import copy
import json
import os
from pathlib import Path

import pytest

from orchestragent.live_workspace import LiveWorkspace
from orchestragent.orch_cli import ROOT, doctor_lines, main, providers, report_text
from orchestragent.orch_planning import PlanningSession, PlanningStore
from orchestragent.orch_progress import snapshot
from orchestragent.orch_run import Orchestrator
from orchestragent.orch_state import Run
from orchestragent.orch_usage import Call, summary
from tests.conftest import (
    BAD_ADD,
    GOOD_ADD,
    GOOD_DOUBLE,
    PARKING,
    Clock,
    ScriptedCaller,
    ScriptedJev,
    local_runner,
    rung,
)

READY: dict[str, object] = {
    "claude": {"ready": True, "auth": "claude_subscription"},
    "dsh": {"credential_ready": True, "verified": True},
}
BUDGET: dict[str, object] = {"cap_cents": 10000, "spent_cents": 12, "reserved_cents": 300}


def test_cli_stages_scheduling_candidate_without_provider_or_activation(
    orch_repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orchestragent.orch_cli.providers",
                        lambda *_: pytest.fail("policy staging must not construct clients"))
    code, output = cli(orch_repo, capsys, "policy", "enable", "--system")
    baseline = json.loads(output)
    assert code == 0 and baseline["tuning"]["jobs"] == 4
    code, output = cli(orch_repo, capsys, "policy", "snapshot", "--jobs", "8",
                       "--test-jobs", "2", "--cache-mode", "off")
    candidate = json.loads(output)
    assert code == 0 and candidate["parent"] == baseline["id"]
    assert candidate["tuning"] == {
        "jobs": 8, "test_jobs": 2, "verify_jobs": 0, "cache_mode": "off"}
    code, output = cli(orch_repo, capsys, "policy", "status")
    assert code == 0 and json.loads(output)["active"] == baseline


def test_progress_traces_waits_and_unknown_receipts_without_dispatch(
    orch_repo: Path, planned: Run, unfinished_call: Call, orchestrator: Orchestrator,
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orchestragent.orch_cli.providers",
                        lambda *_: pytest.fail("progress must not construct clients"))
    before = orchestrator.state.events(planned.id)
    code, output = cli(orch_repo, capsys, "progress", "--json")
    observed = json.loads(output)
    assert code == 0 and observed["run_id"] == planned.id
    assert observed["counts"] == {"generating": 1, "dependency_wait": 1}
    assert observed["remaining_chain"] == 2
    assert observed["tasks"][1]["generation_waits"] == ["T-001"]
    assert observed["tasks"][0]["call_ids"] == [unfinished_call.id]
    assert observed["unresolved_calls"] == [unfinished_call.id]
    assert observed["usage"]["totals"]["cache_hit_rate"] is None
    assert orchestrator.state.events(planned.id) == before
    code, output = cli(orch_repo, capsys, "progress", "--run", planned.id)
    assert code == 0 and "merged 0/2" in output and "not an ETA" in output


@pytest.mark.parametrize("candidate,stage,blocked", [
    ("", "ready", []), ("a" * 40, "candidate_wait", ["T-001"]),
])
def test_progress_distinguishes_interface_ready_from_integration_wait(
    planned: Run, orchestrator: Orchestrator, candidate: str, stage: str, blocked: list[str],
) -> None:
    orchestrator.state.transition(planned.id, "T-002", "PLANNED", "task.candidate", {},
                                  interface_depends_on=["T-001"], pending_candidate=candidate)
    observed = snapshot(orchestrator.state, planned.id)
    assert observed.tasks[1].stage == stage
    assert observed.tasks[1].generation_waits == blocked
    assert observed.tasks[1].integration_waits == ["T-001"]
    assert observed.tasks[1].candidate == candidate


def test_progress_sees_planning_session_before_run_exists(
    orch_repo: Path, orchestrator: Orchestrator, capsys: pytest.CaptureFixture[str],
) -> None:
    journal = PlanningStore(orchestrator.state.store)
    journal.create(PlanningSession("planning-only", "spec", "base", "opus-dsf", "model", "high"))
    call = Call("planning-call", "planning-only", "claude", "model", "boundary", 1)
    with orchestrator.state.usage.capture(call):
        code, output = cli(orch_repo, capsys, "progress", "--json")
        data = json.loads(output)
        assert code == 0 and data["run_id"] == "planning-only"
        assert "acceptance not recorded" in data["status"]
        assert data["unresolved_calls"] == [call.id] and data["tasks"] == []
    assert cli(orch_repo, capsys, "progress", "--run", "missing")[0] == 1


def test_progress_reports_merged_delivery_without_counting_recovery_as_model_work(
    finished: Run, orchestrator: Orchestrator,
) -> None:
    observed = snapshot(orchestrator.state, finished.id)
    assert observed.counts == {"merged": 2}
    assert observed.remaining_chain == 0
    assert observed.usage["totals"] == summary(observed.calls)
    assert all(task.commit and not task.candidate for task in observed.tasks)


def test_reconcile_is_read_only_and_unknown_retry_is_explicit(
    orch_repo: Path, planned: Run, unfinished_call: Call, orchestrator: Orchestrator,
    capsys: pytest.CaptureFixture[str],
) -> None:
    orchestrator.state.recover(planned.id)
    before = orchestrator.state.events(planned.id)
    code, output = cli(orch_repo, capsys, "reconcile", "--run", planned.id, "--json")
    evidence = json.loads(output)
    assert code == 0 and evidence["head_matches"] is True
    assert evidence["unresolved_calls"][0]["id"] == unfinished_call.id
    assert orchestrator.state.events(planned.id) == before
    code, _ = cli(orch_repo, capsys, "retry", "T-001", "--run", planned.id)
    assert code == 1
    code, _ = cli(orch_repo, capsys, "retry", "T-001", "--run", planned.id, "--ack-unknown")
    assert code == 0 and orchestrator.state.task(planned.id, "T-001").status == "READY"


@pytest.mark.parametrize("strategy", ["opus-dsf", "sonnet-dsf"])
def test_bench_mock_and_report_never_construct_provider_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    strategy: str,
) -> None:
    def forbidden(_: Path) -> None:
        pytest.fail("Mock/report must not inspect credentials or create provider clients")

    monkeypatch.setattr("orchestragent.orch_cli.providers", forbidden)
    monkeypatch.setattr(LiveWorkspace, "run_unittests", staticmethod(local_runner))
    suite = ROOT / "examples" / "orch-bench" / "suite.json"
    output = tmp_path / "bench"
    assert main(["bench", str(suite), "--output", str(output), "--json",
                 "--strategy", strategy, "--max-calls", "30"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["passed_trials"] == 2 and result["synthetic"] is True
    assert result["config"]["strategy"] == strategy
    if strategy == "sonnet-dsf":
        calls = [call for trial in result["trials"] for call in trial["usage"]["calls"]]
        assert all(call["model"] in {"claude-sonnet-5-5", "deepseek-flash"} for call in calls)
        assert sum(call["purpose"] == "test-design" for call in calls) == 2
    assert main(["bench", "--output", str(output), "--report", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == result
    assert main(["bench", str(suite), "--output", str(output)]) == 1
    assert "File exists" in capsys.readouterr().err


@pytest.mark.parametrize("budget", ["nan", "inf", "-1"])
def test_bench_rejects_an_invalid_optional_spend_stop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], budget: str,
) -> None:
    output = tmp_path / "bench"
    assert main(["bench", "missing.json", "--output", str(output),
                 "--max-cny", budget]) == 1
    assert "finite and not negative" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("options", [["--context-mode", "scoped"],
                                   ["--context-bytes", "100000"],
                                   ["--baseline-attempts", "0"]])
def test_bench_rejects_invalid_context_modes_before_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, options: list[str],
) -> None:
    monkeypatch.setattr("orchestragent.orch_cli.providers",
                        lambda *_: pytest.fail("provider construction"))
    assert main(["bench", "missing.json", "--output", str(tmp_path / "bad"),
                 "--live", *options]) == 1


def cli(repo: Path, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    code = main(["--repo", str(repo), *args])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


@pytest.mark.parametrize(("status", "stage"), [
    ("READY", "review_ready"), ("RUNNING", "reviewing"),
    ("VERIFYING", "reviewing"), ("PARKED", "parked"),
])
def test_cli_progress_traces_pending_review_and_online_cost_includes_its_receipt(
    orchestrator: Orchestrator, planned: Run, orch_repo: Path,
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, status: str, stage: str,
) -> None:
    monkeypatch.setattr("orchestragent.orch_cli.providers",
                        lambda *_: pytest.fail("observations must not construct clients"))
    state = orchestrator.state
    state.transition(planned.id, "T-001", "READY", "task.ready", {})
    state.transition(planned.id, "T-001", "RUNNING", "task.started", {}, attempts=1,
                     pending_review="frozen-review-id")
    if status != "RUNNING":
        state.transition(planned.id, "T-001", status, "task.test_stage", {})
    receipt = Call("review-call", planned.id, "claude", "model", "review", 1,
                   task_id="T-001", attempt=1, sample="frozen-review-id")
    with state.usage.capture(receipt):
        receipt.outcome, receipt.usage = "ok", {"settled_cents": 7}
    code, output = cli(orch_repo, capsys, "progress", "--run", planned.id, "--json")
    task = json.loads(output)["tasks"][0]
    assert code == 0 and task["review"] == "frozen-review-id" and task["stage"] == stage
    assert task["call_ids"] == [receipt.id]
    code, output = cli(orch_repo, capsys, "cost", "--run", planned.id,
                       "--scope", "online", "--json")
    observed = json.loads(output)["totals"]
    assert code == 0 and observed["costs"]["settled_cents"]["known_sum"] == 7
    code, output = cli(orch_repo, capsys, "progress", "--run", planned.id)
    assert code == 0 and "review=frozen-review-id" in output


def test_status_lists_every_task(
    finished: Run, orch_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = cli(orch_repo, capsys, "status")
    assert code == 0
    assert out.splitlines()[0] == (
        f"run {finished.id}  done  branch orch/{finished.id}  plan {finished.plan_hash}"
    )
    assert [line.split()[:2] for line in out.splitlines()[2:]] == [
        ["T-001", "DONE"],
        ["T-002", "DONE"],
    ]


@pytest.mark.parametrize(("task_id", "expected"), [
    ("T-001", ["adder.py"]),
    ("T-002", ["adder.py", "doubler.py"]),
])
def test_context_inspects_same_interface_selection_without_providers(
    planned: Run, orch_repo: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, task_id: str, expected: list[str],
) -> None:
    def forbidden(_: Path) -> None:
        pytest.fail("context inspection must not construct provider clients")

    monkeypatch.setattr("orchestragent.orch_cli.providers", forbidden)
    code, out = cli(orch_repo, capsys, "context", task_id, "--json", "--run", planned.id)
    result = json.loads(out)
    assert code == 0 and result["interface_files"] == expected
    assert result["revision"] == planned.verified_head
    assert result["test_files"] == ["test_mathops.py"]


def test_context_excerpts_and_cache_decisions_are_available_in_cli(
    finished: Run, orch_repo: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    code, out = cli(orch_repo, capsys, "context", "T-001", "--show", "--json")
    context = json.loads(out)
    assert code == 0 and "class DoubleTest" not in context["test_excerpts"]["test_mathops.py"]
    assert context["context_requests"] == 0 and context["requested_selectors"] == []
    code, out = cli(orch_repo, capsys, "cache", "--json")
    result = json.loads(out)
    assert code == 0 and result["run_id"] == finished.id
    assert result["recorded_policy"]["wait_checks"] == 0


def test_status_explains_parked_and_blocked_tasks(
    orchestrator: Orchestrator,
    caller: ScriptedCaller,
    planned: Run,
    orch_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caller.responses.update(copy.deepcopy(PARKING))
    orchestrator.execute(planned.id)
    _, out = cli(orch_repo, capsys, "status", planned.id)
    assert "blocked by T-001" in out
    assert "PARKED T-001: replan returned no tasks" in out
    assert "  | AssertionError: -1 != 5" in out


def test_retry_returns_a_parked_task_to_a_fresh_ladder(
    orchestrator: Orchestrator,
    caller: ScriptedCaller,
    planned: Run,
    orch_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caller.responses.update(copy.deepcopy(PARKING))
    orchestrator.execute(planned.id)
    code, out = cli(orch_repo, capsys, "retry", "T-001")
    caller.responses.update({"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    assert (code, out.strip()) == (0, f"T-001 READY; continue with `orch run {planned.id}`")
    assert orchestrator.execute(planned.id).status == "done"
    assert cli(orch_repo, capsys, "retry", "T-001") == (
        1, "orch: T-001 is not PARKED; only parked tasks can be retried\n"
    )


def test_why_links_requirement_to_tests_tasks_and_commits(
    orchestrator: Orchestrator, finished: Run, orch_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commit = orchestrator.state.task(finished.id, "T-001").commit
    _, out = cli(orch_repo, capsys, "why", "R1")
    assert out.splitlines() == [
        "R1: add two integers",
        "  test test_mathops.AddTest.test_r1_adds",
        f"  task T-001 DONE {commit[:10]}  implement add",
    ]


@pytest.mark.parametrize(
    ("target", "task", "location"),
    [("adder.py:6", "T-001", "adder.py:6"), ("double", "T-002", "doubler.py:8")],
)
def test_trace_follows_blame_to_task_trailers(
    finished: Run,
    orch_repo: Path,
    capsys: pytest.CaptureFixture[str],
    target: str,
    task: str,
    location: str,
) -> None:
    code, out = cli(orch_repo, capsys, "trace", target)
    assert code == 0 and out.startswith(f"{location} <- commit ")
    assert f"  Orch-Task: {task}" in out and "  Orch-Model: dsh/deepseek-flash" in out


def test_cost_groups_usage_by_model(
    finished: Run, orch_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out = cli(orch_repo, capsys, "cost", "--by", "model", "--json")
    result = json.loads(out)
    assert result["totals"]["calls"] == 3  # includes the planner
    dsf = result["groups"]["dsh/deepseek-flash"]
    assert dsf["known_tokens"]["output_tokens"] == 40
    assert dsf["reported_calls"]["cache_read_tokens"] == 0
    assert dsf["cache_hit_rate"] is None  # an omitted counter is not a zero hit
    assert dsf["costs"]["settled_cents"]["known_sum"] == 2
    # Task dependencies are dynamic; both tasks reuse the project prefix.
    assert len({c["prefix_hash"] for c in result["calls"] if c["provider"] == "dsh"}) == 1


@pytest.mark.parametrize(
    ("args", "message"),
    [(["why", "R9"], "R9 is not a requirement"), (["trace", "nothing_here"], "neither file:line")],
)
def test_lookup_errors_exit_nonzero(
    finished: Run,
    orch_repo: Path,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    message: str,
) -> None:
    code, out = cli(orch_repo, capsys, *args)
    assert code == 1 and message in out


def test_status_without_runs_points_to_plan(
    orch_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = cli(orch_repo, capsys, "status")
    assert code == 1 and "start with `orch plan`" in out


def test_doctor_passes_when_every_prerequisite_holds(orch_repo: Path, tmp_path: Path) -> None:
    ok, lines = doctor_lines(orch_repo, READY, BUDGET, local_runner, tmp_path / "doctor")
    assert ok and all(line.startswith("  ok ") for line in lines[1:-1])
    assert lines[-2] == ("  ok dsh spent ¥0.12 on this machine; no local cap "
                         "(an empty DeepSeek balance stops dispatch)")


@pytest.mark.parametrize(
    ("providers", "budget", "fix"),
    [
        ({**READY, "claude": {"ready": False, "auth": "logged_out"}}, BUDGET, "claude auth login"),
        ({**READY, "dsh": {"credential_ready": False}}, BUDGET, "orch keys dsh"),
    ],
)
def test_doctor_names_the_fix_for_each_missing_prerequisite(
    orch_repo: Path,
    tmp_path: Path,
    providers: dict[str, object],
    budget: dict[str, object],
    fix: str,
) -> None:
    ok, lines = doctor_lines(orch_repo, providers, budget, local_runner, tmp_path / "doctor")
    assert not ok
    assert [line for line in lines if line.startswith("  FIX")] == [
        line for line in lines if fix in line
    ]


def test_report_includes_status_cost_and_merge_hint(
    orchestrator: Orchestrator, finished: Run
) -> None:
    report = report_text(orchestrator.state, finished)
    assert report.endswith(f"merge when satisfied: git merge orch/{finished.id}\n")
    assert "dsh/deepseek-flash" in report


def test_rule_command_lists_shows_and_overrides(
    orchestrator: Orchestrator, orch_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(orch_repo, capsys, "rule")[1].startswith("no rules yet")
    orchestrator.state.add_rule("Check empty input before indexing into it.", "seed")
    listed = cli(orch_repo, capsys, "rule")[1].splitlines()
    promoted = cli(orch_repo, capsys, "rule", "r1", "--promote")[1].splitlines()
    assert listed[1].split()[:3] == ["r1", "builder", "candidate"]
    assert promoted[0] == "r1 active: Check empty input before indexing into it."
    assert promoted[-1] == "  manual active"
    assert cli(orch_repo, capsys, "rule", "r9") == (1, "orch: 'unknown rule r9'\n")


def test_cost_by_rule_attributes_calls_to_the_rules_in_their_prompt(
    finished: Run, orch_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out = cli(orch_repo, capsys, "cost", "--by", "rule", "--json")
    assert json.loads(out)["groups"]["none"]["calls"] == 3


@pytest.mark.parametrize(("preset", "expected"), [(None, 900.0), ("30", 30.0), ("0", 900.0)])
def test_cli_clients_get_a_default_deadline_without_leaking_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preset: str | None, expected: float
) -> None:
    if preset is None:
        monkeypatch.delenv("ORCHESTRA_NATIVE_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("ORCHESTRA_NATIVE_TIMEOUT", preset)
    clients = providers(tmp_path)
    clients.close()
    assert clients.timeout == expected
    assert os.environ.get("ORCHESTRA_NATIVE_TIMEOUT") == preset


def test_doctor_reports_jev_as_optional(orch_repo: Path, tmp_path: Path) -> None:
    ok, lines = doctor_lines(orch_repo, READY, BUDGET, local_runner, tmp_path / "doctor")
    ready = {**READY, "jev": {"ready": True}}
    assert ok and lines[-1].startswith("  opt jev (typesafe-ai/jev) not configured")
    assert doctor_lines(orch_repo, ready, BUDGET, local_runner, tmp_path / "d")[1][-1] == (
        "  ok  jev (typesafe-ai/jev) ready for triage and pre-screen"
    )


def test_jev_report_compares_decisions_with_the_rules(
    orch_repo: Path, caller: ScriptedCaller, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    caller.responses.update(
        {"dsh:T-001": [BAD_ADD], "claude:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]}
    )
    orchestrator = Orchestrator(
        orch_repo, caller, local_runner, notify=lambda line: None, clock=clock,
        decide=ScriptedJev(triage=[rung("claude_patch", 0.9)]), jev_mode="on",
    )
    run = orchestrator.execute(orchestrator.plan("spec").id)
    _, report = cli(orch_repo, capsys, "jev")
    _, cost = cli(orch_repo, capsys, "cost", "--by", "model")
    assert report.splitlines() == [
        "jev calls 2 (failed 0), spent ¥0.02",
        "triage 1: agrees with the rules 0, confident 1, applied 1",
        "screens 1: flagged 0, applied 0",
        "  T-001 a1: rules 1, jev claude_patch 0.90 (applied)",
    ]
    assert run.status == "done" and "jev/typesafe-ai/jev" in cost
    # Jev's API-billed calls count toward --max-cny while the run is going.
    assert orchestrator.spent == orchestrator.state.spent(run.id) == 5


def test_cli_explains_a_directory_that_is_not_a_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = cli(tmp_path, capsys, "status")
    assert code == 1 and "a git repository with a commit?" in out


def test_cli_refuses_to_plan_into_its_own_checkout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(ROOT)
    assert main(["run"]) == 1
    assert "pass --repo <project>" in capsys.readouterr().err


@pytest.mark.parametrize(("verified", "state", "expected", "label"), [
    (False, "unverified", False, "unverified: local setup only"),
    (False, "auth_required", False, "rejected -> update the key locally"),
    (True, "verified", True, "verified by actual inference"),
])
def test_doctor_distinguishes_local_configuration_from_inference_evidence(
    orch_repo: Path, tmp_path: Path, verified: bool, state: str, expected: bool, label: str,
) -> None:
    status = {**READY, "dsh": {"credential_ready": True, "verified": verified,
                               "authentication": {"state": state}}}
    ok, lines = doctor_lines(orch_repo, status, BUDGET, local_runner, tmp_path / "doctor")
    assert ok is expected
    assert any(label in line for line in lines)
    if state == "unverified":
        assert any("explicitly authorized first inference is allowed" in line for line in lines)


@pytest.mark.parametrize(("state", "configured", "cap", "expected"), [
    ("unverified", True, 10000, True),
    ("auth_required", True, 10000, False),
    ("unverified", False, 10000, False),
    ("unverified", True, 100, True),  # a small recorded cap is no longer a gate
])
def test_doctor_first_inference_option_preserves_real_failures(
    orch_repo: Path, tmp_path: Path, state: str, configured: bool, cap: int, expected: bool,
) -> None:
    status = {**READY, "dsh": {"credential_ready": True, "configured": configured,
                               "verified": False, "authentication": {"state": state}}}
    ok, lines = doctor_lines(orch_repo, status, {**BUDGET, "cap_cents": cap},
                            local_runner, tmp_path / "doctor", allow_unverified_auth=True)
    assert ok is expected
    assert not any("verified by actual inference" in line for line in lines)
