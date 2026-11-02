from __future__ import annotations

import json
import runpy
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import pytest

from orchestragent.envelope import digest
from orchestragent.live_workspace import LiveWorkspace
from orchestragent.native import NativeResult
from orchestragent.orch_bench import (
    Case,
    _Admission,
    load_suite,
    mock_caller,
    read_report,
    report_lines,
    run_suite,
)
from orchestragent.orch_environment import PytestEnvironment
from orchestragent.orch_policy import Tuning
from orchestragent.orch_run import DSF, DSF_RESERVATION, Caller, Orchestrator
from orchestragent.orch_synthetic import SyntheticBuilder
from orchestragent.platform import sandbox_available
from orchestragent.serialization import dumps
from orchestragent.store import Store
from orchestragent.workspace import Workspace
from tests.conftest import ScriptedCaller, local_runner

SUITE = Path(__file__).resolve().parents[1] / "examples" / "orch-bench" / "suite.json"


@pytest.fixture
def artifact_reassessment_case(orch_repo: Path, tmp_path: Path) -> tuple[Path, Path, str]:
    case = tmp_path / "offline-case"
    case.mkdir()
    (case / "spec.md").write_text("R1: add positive integers")
    (case / "test_acceptance.py").write_text(
        "import unittest\nclass Acceptance(unittest.TestCase):\n"
        "    def test_r1_add(self):\n        self.assertEqual(2 + 2, 4)\n")
    output = orch_repo.parent
    environment = PytestEnvironment.create(output / "environment")
    revision = Workspace.git(orch_repo, "rev-parse", "HEAD")
    (output / "report.json").write_text(dumps({
        "case": case.name, "spec_digest": digest((case / "spec.md").read_text()),
        "status": "failed", "engine_revision": "original-engine",
        "evaluation_base": revision, "environment_fingerprint": environment.fingerprint,
        "acceptance_digests": {"test_acceptance.py": "original-oracle"},
    }))
    return output, case, revision


def test_offline_oracle_reassessment_preserves_original_report_and_artifact(
    artifact_reassessment_case: tuple[Path, Path, str], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output, case, revision = artifact_reassessment_case
    destination = tmp_path.parent / (tmp_path.name + "-assessment")
    original = (output / "report.json").read_bytes()
    monkeypatch.setattr(LiveWorkspace, "run_unittests", local_runner)
    script = runpy.run_path(str(SUITE.parents[2] / "scripts" / "orch_reassess_artifact.py"))
    result = script["reassess"](output, revision, case, destination)
    assert result["acceptance_passed"] and result["original_status"] == "failed"
    assert result["model_calls"] == 0 and result["implementation_interventions"] == 0
    assert result["artifact_revision"] == revision
    assert (output / "report.json").read_bytes() == original
    assert Workspace.git(output / "project", "rev-parse", "HEAD") == revision


@pytest.mark.parametrize("correct", [False, True])
def test_repository_oracle_positive_control_is_separate_and_cannot_leak_reference(
    artifact_reassessment_case: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch,
    correct: bool,
) -> None:
    output, case_path, base = artifact_reassessment_case
    reference = case_path / "reference"
    reference.mkdir()
    (reference / "addition.py").write_text(
        f"def add(a,b):\n    return {'a+b' if correct else '0'}\n")
    acceptance = {"test_acceptance.py": (
        "import unittest\nfrom addition import add\nclass TestAddition(unittest.TestCase):\n"
        "    def test_r1_add(self):\n        self.assertEqual(add(2,3),5)\n")}
    monkeypatch.setattr(LiveWorkspace, "run_unittests", local_runner)
    script = runpy.run_path(str(SUITE.parents[2] / "scripts" / "orch_repository_probe.py"))
    passed, log, revision = script["positive_control"](
        case_path, output, output / "project", base,
        PytestEnvironment.load(output / "environment"),
        Case(case_path.name, "R1 add", {}, acceptance))
    assert passed is correct and ("OK" if correct else "FAILED") in log
    assert not (output / "project" / "addition.py").exists()
    assert Workspace.git(output / "project", "rev-parse", "HEAD") == base
    assert revision not in Workspace.git(output / "project", "rev-list", "--all").splitlines()
    assert not Workspace.git(output / "project", "status", "--porcelain")


@pytest.mark.parametrize("protected", ["test_bypass.py", "pytest.ini"])
def test_repository_reference_cannot_replace_tests_or_collection_configuration(
    artifact_reassessment_case: tuple[Path, Path, str], protected: str,
) -> None:
    output, case_path, base = artifact_reassessment_case
    reference = case_path / "reference"
    reference.mkdir()
    (reference / "addition.py").write_text("def add(a,b):\n    return a+b\n")
    (case_path / "reference-edits.json").write_text(dumps([
        {"path": protected, "search": "", "replace": "# cannot change collection\n"}]))
    script = runpy.run_path(str(SUITE.parents[2] / "scripts" / "orch_repository_probe.py"))
    with pytest.raises(ValueError):
        script["positive_control"](
            case_path, output, output / "project", base,
            PytestEnvironment.load(output / "environment"), Case(case_path.name, "R1", {}, {}))
    assert Workspace.git(output / "project", "rev-parse", "HEAD") == base
    assert not Workspace.git(output / "project", "status", "--porcelain")


@pytest.mark.parametrize("invalid", ["revision", "dirty", "spec", "nested-output"])
def test_offline_oracle_reassessment_rejects_changed_inputs_before_evaluation(
    artifact_reassessment_case: tuple[Path, Path, str], tmp_path: Path, invalid: str,
) -> None:
    output, case, revision = artifact_reassessment_case
    destination = tmp_path.parent / (tmp_path.name + "-assessment")
    if invalid == "revision":
        revision = "0" * 40
    elif invalid == "dirty":
        (output / "project" / "README.md").write_text("changed artifact")
    elif invalid == "spec":
        (case / "spec.md").write_text("changed requirement")
    else:
        destination = output / "reassessment"
    script = runpy.run_path(str(SUITE.parents[2] / "scripts" / "orch_reassess_artifact.py"))
    with pytest.raises(ValueError):
        script["reassess"](output, revision, case, destination)
    assert not destination.exists()


@pytest.mark.parametrize("path,existing,blocked", [
    ("README.md", True, False), ("test_new.py", False, False), ("test_old.py", True, True),
    ("pytest.ini", False, True), ("nested/conftest.py", False, True),
    ("requirements-extra.txt", False, True),
])
@pytest.mark.parametrize("staged", [False, True])
def test_repository_baseline_freezes_new_collection_controls(
    orch_repo: Path, path: str, existing: bool, blocked: bool, staged: bool,
) -> None:
    target = orch_repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if existing:
        target.write_text("original\n")
        Workspace.git(orch_repo, "add", path)
        Workspace.git(orch_repo, "commit", "-m", "tracked input")
    base = Workspace.git(orch_repo, "rev-parse", "HEAD")
    target.write_text("changed\n")
    if staged:
        Workspace.git(orch_repo, "add", path)
    script = runpy.run_path(str(SUITE.parents[2] / "scripts" / "orch_repository_probe.py"))
    assert script["protected_changes"](orch_repo, base) == ([path] if blocked else [])


@pytest.mark.skipif(not sandbox_available(), reason="real pinned sandbox runner")
@pytest.mark.parametrize(("nodeids", "expected"), [
    (["test_pass.py::TestPass::test_value"], 0), (None, 1),
    (["../outside.py"], None), (["--collect-only"], None),
])
def test_native_single_agent_can_select_tests_without_bypassing_the_final_project_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, nodeids: list[str] | None, expected: int | None,
) -> None:
    output = tmp_path / "trial"
    execution = output / "project"
    execution.mkdir(parents=True)
    (output / "manifest.json").write_text(dumps({"evaluation_base": "a" * 40}))
    PytestEnvironment.create(output / "environment")
    (execution / "test_pass.py").write_text(
        "import unittest\nclass TestPass(unittest.TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(2+2,4)\n")
    (execution / "test_fail.py").write_text("def test_unfinished():\n    assert False\n")
    run_sandbox = LiveWorkspace.run_pytest
    concurrent = execution / ".orch-pytest-another-active-call"

    def interleaved(*args: Any, **kwargs: Any) -> tuple[bool, str]:
        result = run_sandbox(*args, **kwargs)
        concurrent.mkdir()
        (concurrent / "owned.txt").write_text("another call still owns these bytes")
        return result

    monkeypatch.setattr(LiveWorkspace, "run_pytest", interleaved)
    helper = runpy.run_path(str(SUITE.parents[2] / "scripts" / "orch_repository_probe.py"))
    if expected is None:
        with pytest.raises(ValueError, match="Unsafe pytest nodeid"):
            helper["agent_test"](output, nodeids)
        assert not list(output.glob("agent-test-*.json"))
    else:
        with pytest.raises(SystemExit) as caught:
            helper["agent_test"](output, nodeids)
        assert caught.value.code == expected
        result = json.loads(next(output.glob("agent-test-*.json")).read_text())
        assert result["nodeids"] == nodeids and result["passed"] is (expected == 0)
        assert "issuer=controller" in capsys.readouterr().out
        assert list(execution.glob(".orch-pytest-*")) == [concurrent]
        assert (concurrent / "owned.txt").read_text() == "another call still owns these bytes"


def test_absolute_trial_deadline_survives_scheduler_cancellation_reset(
    orchestrator: Orchestrator, admission_store: tuple[Store, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orchestragent.orch_bench.time.monotonic", lambda: 20.0)
    store, trial = admission_store
    admission = _Admission(4, 1200, deadline=19.0)
    orchestrator.call = admission.wrap(
        lambda *a, **kw: pytest.fail("expired trial dispatched"), orchestrator, store, trial)
    orchestrator.cancel.set()
    orchestrator.cancel.clear()
    answer = orchestrator._call_model("expired", DSF, "request", {}, "expired-call",
                                       purpose="build")
    assert answer.outcome == "cancelled" and answer.usage["forwarded"] == 0
    assert admission.calls == admission.cents == admission.inflight == 0
    assert orchestrator.state.usage.calls()[0].outcome == "cancelled"


@pytest.fixture
def suite_payload() -> dict[str, object]:
    return cast(dict[str, object], json.loads(SUITE.read_text()))


def write_suite(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "suite.json"
    path.write_text(dumps(payload))
    return path


def one_case(payload: dict[str, object]) -> dict[str, object]:
    cases = cast(list[dict[str, object]], payload["cases"])
    payload["cases"] = cases[:1]
    return payload


@pytest.fixture
def admission_store(tmp_path: Path) -> tuple[Store, dict[str, object]]:
    store = Store(tmp_path / "admission.sqlite")
    trial: dict[str, object] = {"id": "admission-test", "run_id": ""}
    with store.transaction() as db:
        db.execute("CREATE TABLE bench_trials (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        db.execute("INSERT INTO bench_trials VALUES (?,?)", (trial["id"], dumps(trial)))
    return store, trial


@pytest.fixture
def admission_pair(
    orchestrator: Orchestrator,
    admission_store: tuple[Store, dict[str, object]],
) -> Generator[Callable[..., AdmissionPair], None, None]:
    store, trial = admission_store
    created: list[AdmissionPair] = []

    def configure(*, max_cents: int, first_mode: str = "settled",
                  settlement: int = 4) -> AdmissionPair:
        pair = AdmissionPair(orchestrator)
        for call_id in ("first-call", "second-call", "permanently-blocked"):
            pair.wrapper_entered[call_id] = threading.Event()
            pair.provider_entered[call_id] = threading.Event()
            pair.completed[call_id] = threading.Event()

        def caller(provider: str, model: str, prompt: str, schema: dict[str, object],
                   run_id: str, cancelled: Callable[[], bool], *, effort: str = ""
                   ) -> NativeResult:
            assert provider == "dsh"
            with pair.lock:
                pair.calls += 1
                number = pair.calls
                pair.active += 1
                pair.reserved += DSF_RESERVATION
                pair.peak = max(pair.peak, pair.active)
            pair.provider_entered[run_id].set()
            if number == 1:
                pair.release_first.wait(10)
                with pair.lock:
                    pair.active -= 1
                    if first_mode == "settled":
                        pair.reserved -= DSF_RESERVATION
                        pair.settled += settlement
                if first_mode == "exception":
                    raise RuntimeError("unknown settlement")
                if first_mode == "unknown":
                    return NativeResult("failed", {}, usage={})
                return NativeResult("ok", {}, usage={"settled_cents": settlement})
            with pair.lock:
                pair.active -= 1
                pair.reserved -= DSF_RESERVATION
                pair.settled += settlement
            return NativeResult("ok", {}, usage={"settled_cents": settlement})

        pair.admission = _Admission(4, max_cents)
        wrapped = pair.admission.wrap(caller, orchestrator, store, trial)

        def observed(provider: str, model: str, prompt: str, schema: dict[str, object],
                     run_id: str, cancelled: Callable[[], bool], *, effort: str = ""
                     ) -> NativeResult:
            pair.wrapper_entered[run_id].set()
            return wrapped(provider, model, prompt, schema, run_id, cancelled, effort=effort)

        orchestrator.call = observed
        created.append(pair)
        return pair

    try:
        yield configure
    finally:
        for pair in created:
            pair.close()


def dispatch_dsf(orchestrator: Orchestrator, call_id: str) -> NativeResult:
    return orchestrator._call_model(
        "admission-test-run", DSF, "test request", {}, call_id,
        purpose="build", task_id="T-001", attempt=1,
    )


@dataclass
class AdmissionPair:
    orchestrator: Orchestrator
    admission: _Admission | None = None
    release_first: threading.Event = field(default_factory=threading.Event)
    wrapper_entered: dict[str, threading.Event] = field(default_factory=dict)
    provider_entered: dict[str, threading.Event] = field(default_factory=dict)
    completed: dict[str, threading.Event] = field(default_factory=dict)
    results: dict[str, NativeResult] = field(default_factory=dict)
    errors: list[Exception] = field(default_factory=list)
    threads: list[threading.Thread] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    calls: int = 0
    active: int = 0
    reserved: int = 0
    settled: int = 0
    peak: int = 0

    def start(self, call_id: str) -> None:
        def invoke() -> None:
            try:
                self.results[call_id] = dispatch_dsf(self.orchestrator, call_id)
            except Exception as error:
                self.errors.append(error)
            finally:
                self.completed[call_id].set()

        thread = threading.Thread(target=invoke, daemon=True)
        self.threads.append(thread)
        thread.start()

    def close(self) -> None:
        self.release_first.set()
        for thread in self.threads:
            thread.join(timeout=10)

    @contextmanager
    def queued_pair(self) -> Generator[bool, None, None]:
        try:
            self.start("first-call")
            if not self.provider_entered["first-call"].wait(10):
                yield False
                return
            self.start("second-call")
            if not self.wrapper_entered["second-call"].wait(10):
                yield False
                return
            entered_provider = self.provider_entered["second-call"].wait(0.1)
            yield not entered_provider and not self.completed["second-call"].is_set()
        finally:
            self.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", 2), ("schema_version", True), ("cases", {}), ("cases", [])],
)
def test_load_suite_rejects_malformed_schema(
    tmp_path: Path, suite_payload: dict[str, object], field: str, value: object,
) -> None:
    suite_payload[field] = value
    with pytest.raises(ValueError):
        load_suite(write_suite(tmp_path, suite_payload))


@pytest.mark.parametrize(
    ("file_kind", "name"),
    [
        ("seed", "../escape.py"),
        ("seed", "/tmp/escape.py"),
        ("acceptance", "nested/test_hidden.py"),
        ("seed", "_orch_bench_acceptance.py"),
    ],
)
def test_load_suite_rejects_unsafe_paths(
    tmp_path: Path, suite_payload: dict[str, object], file_kind: str, name: str,
) -> None:
    cases = cast(list[dict[str, object]], suite_payload["cases"])
    cases[0][file_kind] = {name: "content"}
    with pytest.raises(ValueError):
        load_suite(write_suite(tmp_path, suite_payload))


def test_load_suite_rejects_duplicate_case_ids(
    tmp_path: Path, suite_payload: dict[str, object],
) -> None:
    cases = cast(list[dict[str, object]], suite_payload["cases"])
    cases.append({**cases[0], "id": str(cases[1]["id"]).upper()})
    with pytest.raises(ValueError, match="duplicate"):
        load_suite(write_suite(tmp_path, suite_payload))


@pytest.mark.parametrize(
    "shape",
    [
        {"tasks": 0, "packages": 1, "fan_in": 0, "seed": 1},
        {"tasks": 2, "packages": 3, "fan_in": 0, "seed": 1},
        {"tasks": 2, "packages": 1, "fan_in": -1, "seed": 1},
        {"tasks": 2, "packages": 1, "fan_in": 0, "seed": -1},
        {"tasks": True, "packages": 1, "fan_in": 0, "seed": 1},
        {"tasks": 2, "packages": 1, "fan_in": 0, "seed": 1, "extra": 0},
    ],
)
def test_load_suite_rejects_invalid_mock_configuration(
    tmp_path: Path, suite_payload: dict[str, object], shape: dict[str, int],
) -> None:
    cases = cast(list[dict[str, object]], suite_payload["cases"])
    cases[0]["mock_shape"] = shape
    with pytest.raises(ValueError):
        load_suite(write_suite(tmp_path, suite_payload))


def test_suite_has_two_independent_held_out_synthetic_cases() -> None:
    cases = load_suite(SUITE)
    assert len(cases) == 2
    assert cases[0].mock_shape != cases[1].mock_shape
    assert "f1(7)" in cases[0].acceptance["test_heldout_offsets.py"]
    assert "f1(-11)" in cases[0].acceptance["test_heldout_offsets.py"]
    assert "f3(7)" in cases[1].acceptance["test_heldout_dependencies.py"]
    assert all("NOT intelligence evidence" in source
               for case in cases for source in case.acceptance.values())


@pytest.mark.parametrize(
    ("mode", "max_calls", "jobs", "repeats", "max_cents"),
    [
        ("unknown", 10, 2, 1, 0),
        ("mock", 0, 2, 1, 0),
        ("mock", 10, 0, 1, 0),
        ("mock", 10, 2, 0, 0),
        ("mock", 10, 2, 1, -1),
    ],
)
def test_run_suite_rejects_invalid_configuration(
    tmp_path: Path, mode: str, max_calls: int, jobs: int, repeats: int, max_cents: int,
) -> None:
    output = tmp_path / "invalid-config"
    with pytest.raises(ValueError, match="configuration"):
        run_suite(SUITE, output, mock_caller, local_runner, mode=mode,
                  max_calls=max_calls, jobs=jobs, repeats=repeats, max_cents=max_cents)
    assert not output.exists()


@pytest.mark.parametrize("distributed", [False, True])
def test_benchmark_executes_and_records_frozen_scheduling_settings(
    tmp_path: Path, suite_payload: dict[str, object], orchestrator: Orchestrator,
    distributed: bool,
) -> None:
    policy = orchestrator.policies.enable(tuning=Tuning(jobs=1, verify_jobs=1, cache_mode="off"))
    result = run_suite(write_suite(tmp_path, one_case(suite_payload)), tmp_path / "tuned-report",
                       lambda case: mock_caller(case, distributed=distributed), local_runner,
                       mode="mock", max_calls=100, jobs=2, policy=policy, distributed=distributed)
    trials = cast(list[dict[str, object]], result["trials"])
    assert result["passed_trials"] == 1
    settings = cast(dict[str, object], trials[0]["execution_config"])
    assert (settings["jobs"], settings["verify_jobs"], settings["cache_mode"],
            settings["policy_id"]) == (1, 1, "off", policy.id)
    assert orchestrator.policies.current() == policy


def test_mock_suite_runs_locally_and_reopens_successful_report(
    tmp_path: Path, suite_payload: dict[str, object],
) -> None:
    suite_path = write_suite(tmp_path, suite_payload)
    output = tmp_path / "mock-report"
    mock_callers: list[Caller] = []

    def factory(case: Case) -> Caller:
        caller = mock_caller(case)
        assert isinstance(caller, SyntheticBuilder)
        mock_callers.append(caller)
        return caller

    result = run_suite(suite_path, output, factory, local_runner, mode="mock",
                       max_calls=100, jobs=2)
    reopened = read_report(output)
    assert result["expected_trials"] == reopened["expected_trials"] == 2
    assert reopened["completed_trials"] == reopened["passed_trials"] == 2
    assert reopened["mode"] == "mock" and reopened["success_rate"] == 1.0
    assert reopened["synthetic"] is True and len(mock_callers) == 2
    trials = cast(list[dict[str, object]], reopened["trials"])
    assert all(trial["status"] == "passed" for trial in trials)
    assert all(cast(dict[str, object], trial["usage"])["synthetic"] is True for trial in trials)
    assert "NOT intelligence evidence" in report_lines(reopened)[0]
    assert all(trial["acceptance_report"] for trial in trials)
    original_snapshot = reopened["suite_snapshot"]
    original_config = reopened["config"]
    original_hash = reopened["suite_hash"]
    source_cases = cast(list[dict[str, object]], suite_payload["cases"])
    source_cases[0]["spec"] = "changed after the benchmark run"
    write_suite(tmp_path, suite_payload)
    reopened = read_report(output)
    assert reopened["suite_snapshot"] == original_snapshot
    assert reopened["config"] == original_config and reopened["suite_hash"] == original_hash
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(dumps(reopened["suite_snapshot"]))
    snapshot_cases = load_suite(snapshot)
    assert snapshot_cases[0].spec != load_suite(suite_path)[0].spec
    config = cast(dict[str, object], reopened["config"])
    assert config["max_calls"] == 100 and config["jobs"] == 2
    assert len(str(config["engine_source_hash"])) == 64
    assert reopened["case_kind"] == "synthetic"
    assert all(cast(dict[str, int], trial["dispatches"])["builder"] > 0 for trial in trials)


@pytest.mark.parametrize(("max_calls", "rejected_purpose"), [(1, "test-design"), (2, "build")])
def test_thin_call_budget_includes_test_design_before_any_builder_dispatch(
    tmp_path: Path, suite_payload: dict[str, object], max_calls: int, rejected_purpose: str,
) -> None:
    suite = write_suite(tmp_path, one_case(suite_payload))
    result = run_suite(suite, tmp_path / "thin-budget",
                       lambda case: mock_caller(case, "sonnet-dsf"), local_runner,
                       mode="mock", strategy="sonnet-dsf", max_calls=max_calls, jobs=1)
    trials = cast(list[dict[str, object]], result["trials"])
    usage = cast(dict[str, object], trials[0]["usage"])
    calls = cast(list[dict[str, object]], usage["calls"])
    rejected = [call for call in calls
                if cast(dict[str, object], call["usage"]).get("forwarded") == 0]
    assert (trials[0]["status"], result["success_rate"],
            {call["purpose"] for call in rejected}, len(calls) - len(rejected)) == (
        "paused", None, {rejected_purpose}, max_calls,
    )
    assert {call["model"] for call in calls} <= {"claude-sonnet-5-5", "deepseek-flash"}


def test_independent_acceptance_rejects_a_solution_passing_planner_tests(
    tmp_path: Path, suite_payload: dict[str, object],
) -> None:
    case = load_suite(SUITE)[0]
    caller = mock_caller(case)
    assert isinstance(caller, SyntheticBuilder)
    # Passes generated f1(2) and f1(-3) checks, but fails the hidden f1(7) check.
    caller.bodies["T-001"] = (
        "pkg0/f1.py", "return x + 1 if x in (2, -3) else 0",
    )
    output = tmp_path / "heldout-failure"
    result = run_suite(write_suite(tmp_path, one_case(suite_payload)), output,
                       lambda _: caller, local_runner, mode="mock", max_calls=100)
    reopened = read_report(output)
    trial = cast(list[dict[str, object]], reopened["trials"])[0]
    assert result["passed_trials"] == 0
    assert trial["status"] == "failed" and trial["acceptance_passed"] is False
    assert "FAIL" in str(trial["acceptance_report"])
    assert any("failed" in line for line in report_lines(reopened))


def test_plan_failure_keeps_usage_in_reopened_report(
    tmp_path: Path, suite_payload: dict[str, object],
) -> None:
    bad_plan = NativeResult(
        "ok", {"summary": "incomplete"},
        usage={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25,
               "settled_cents": 2},
    )
    scripted = ScriptedCaller({"plan": [bad_plan, bad_plan, bad_plan]})
    output = tmp_path / "plan-failure"
    run_suite(write_suite(tmp_path, one_case(suite_payload)), output, lambda _: scripted,
              local_runner, mode="mock", max_calls=10)
    reopened = read_report(output)
    trial = cast(list[dict[str, object]], reopened["trials"])[0]
    usage = cast(dict[str, object], trial["usage"])
    totals = cast(dict[str, object], usage["totals"])
    assert trial["status"] == "plan_rejected"
    assert totals["calls"] == 3
    known_tokens = cast(dict[str, object], totals["known_tokens"])
    assert known_tokens["input_tokens"] == 60
    assert known_tokens["output_tokens"] == 15
    costs = cast(dict[str, object], totals["costs"])
    assert cast(dict[str, object], costs["settled_cents"])["known_sum"] == 6.0


def test_shared_call_cap_counts_planner_calls_across_trials(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []

    def factory(case: Case) -> Caller:
        inner = mock_caller(case)

        def logged(provider: str, model: str, prompt: str, schema: dict[str, object],
                   run_id: str, cancelled: Callable[[], bool], *, effort: str = ""
                   ) -> NativeResult:
            calls.append((case.id, provider))
            return inner(provider, model, prompt, schema, run_id, cancelled, effort=effort)

        return logged

    report = run_suite(SUITE, tmp_path / "call-cap", factory, local_runner, mode="mock",
                       max_calls=1, jobs=1)
    assert calls == [("offsets-single-package", "claude")]
    assert report["expected_trials"] == 2 and report["completed_trials"] == 1
    assert report["passed_trials"] == 0
    assert report["success_rate"] is None
    trials = cast(list[dict[str, object]], report["trials"])
    assert [trial["status"] for trial in trials] == ["paused", "queued"]
    usage = cast(dict[str, object], trials[0]["usage"])
    receipts = cast(list[dict[str, object]], usage["calls"])
    blocked = [receipt for receipt in receipts if receipt["outcome"] == "policy_blocked"]
    assert len(blocked) == 1
    blocked_usage = cast(dict[str, object], blocked[0]["usage"])
    assert blocked_usage["rejected"] == 1 and blocked_usage["settled_cents"] == 0
    blocked_tokens = cast(dict[str, object], blocked[0]["tokens"])
    assert blocked_tokens["input_tokens"] == blocked_tokens["output_tokens"] == 0


def test_a_single_paused_trial_has_no_success_rate(
    tmp_path: Path, suite_payload: dict[str, object],
) -> None:
    suite_path = write_suite(tmp_path, one_case(suite_payload))
    report = run_suite(suite_path, tmp_path / "single-paused", mock_caller, local_runner,
                       mode="mock", max_calls=1)
    trials = cast(list[dict[str, object]], report["trials"])
    assert report["expected_trials"] == report["completed_trials"] == 1
    assert report["passed_trials"] == 0 and report["success_rate"] is None
    assert trials[0]["status"] == "paused"


def test_subreservation_budget_blocks_dsf_and_keeps_planner_usage(
    tmp_path: Path, suite_payload: dict[str, object],
) -> None:
    providers: list[str] = []

    def factory(case: Case) -> Caller:
        inner = mock_caller(case)

        def tracked(provider: str, model: str, prompt: str, schema: dict[str, object],
                    run_id: str, cancelled: Callable[[], bool], *, effort: str = ""
                    ) -> NativeResult:
            providers.append(provider)
            result = inner(provider, model, prompt, schema, run_id, cancelled, effort=effort)
            if provider == "claude":
                return NativeResult(
                    result.outcome, result.data, result.session_id,
                    {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
                     "settled_cents": 7}, result.message,
                )
            return result

        return tracked

    suite_path = write_suite(tmp_path, one_case(suite_payload))
    report = run_suite(suite_path, tmp_path / "reservation-blocked", factory, local_runner,
                       mode="mock", max_calls=10, max_cents=DSF_RESERVATION - 1)
    trial = cast(list[dict[str, object]], report["trials"])[0]
    usage = cast(dict[str, object], trial["usage"])
    receipts = cast(list[dict[str, object]], usage["calls"])
    planner = next(receipt for receipt in receipts if receipt["provider"] == "claude")
    blocked = [receipt for receipt in receipts if receipt["provider"] == "dsh"]
    assert providers == ["claude"]
    assert trial["status"] == "paused" and trial["acceptance_passed"] is None
    assert planner["outcome"] == "ok"
    assert cast(dict[str, object], planner["usage"])["settled_cents"] == 7
    # Several ready tasks may reach the local guard before the first rejection stops dispatch.
    assert blocked and all(receipt["outcome"] == "policy_blocked" for receipt in blocked)
    assert all(cast(dict[str, object], receipt["usage"])["settled_cents"] == 0
               for receipt in blocked)
    totals = cast(dict[str, object], usage["totals"])
    assert totals["calls"] == len(receipts) == 1 + len(blocked)
    assert cast(dict[str, object], totals["known_tokens"])["input_tokens"] == 10
    assert cast(dict[str, object], totals["costs"])["settled_cents"] == {
        "known_sum": 7.0, "reported_calls": len(receipts),
    }


def test_concurrent_dsf_calls_stay_within_reservation_cap(
    tmp_path: Path, suite_payload: dict[str, object],
) -> None:
    cases = cast(list[dict[str, object]], suite_payload["cases"])
    cases[0]["mock_shape"] = {"tasks": 6, "packages": 6, "fan_in": 0, "seed": 1}
    active = peak = 0
    lock = threading.Lock()

    def factory(case: Case) -> Caller:
        inner = mock_caller(case)
        assert isinstance(inner, SyntheticBuilder)
        inner.latency = 0.08

        def observed(provider: str, model: str, prompt: str, schema: dict[str, object],
                     run_id: str, cancelled: Callable[[], bool], *, effort: str = ""
                     ) -> NativeResult:
            nonlocal active, peak
            tracked = provider == "dsh"
            if tracked:
                with lock:
                    active += 1
                    peak = max(peak, active)
            try:
                return inner(provider, model, prompt, schema, run_id, cancelled, effort=effort)
            finally:
                if tracked:
                    with lock:
                        active -= 1

        return observed

    suite_path = write_suite(tmp_path, one_case(suite_payload))
    max_cents = 2 * DSF_RESERVATION + 3
    report = run_suite(suite_path, tmp_path / "concurrent-cap", factory, local_runner,
                       mode="mock", max_calls=100, jobs=4, max_cents=max_cents)
    assert peak == 2
    assert peak * DSF_RESERVATION <= max_cents
    assert report["expected_trials"] == 1


@pytest.mark.parametrize("rejection", [{"rejected": 400, "forwarded": 1}, {"forwarded": 0}])
def test_proven_unbilled_rejection_releases_suite_exposure(
    tmp_path: Path, suite_payload: dict[str, object], rejection: dict[str, object],
) -> None:
    calls = 0

    def factory(case: Case) -> Caller:
        inner = mock_caller(case)

        def rejected_once(provider: str, model: str, prompt: str, schema: dict[str, object],
                          run_id: str, cancelled: Callable[[], bool], *, effort: str = ""
                          ) -> NativeResult:
            nonlocal calls
            if provider == "dsh":
                calls += 1
                if calls == 1:
                    return NativeResult("failed", {}, usage=rejection)
            result = inner(provider, model, prompt, schema, run_id, cancelled, effort=effort)
            return replace(result, usage={"settled_cents": 1}) if provider == "dsh" else result

        return rejected_once

    suite_path = write_suite(tmp_path, one_case(suite_payload))
    result = run_suite(suite_path, tmp_path / "rejected-once", factory, local_runner,
                       mode="mock", max_calls=10, jobs=1, max_cents=DSF_RESERVATION + 1)
    assert result["passed_trials"] == 1 and calls == 3


def test_interrupted_suite_keeps_an_incomplete_trial_denominator(
    tmp_path: Path,
) -> None:
    calls = 0

    def factory(_: Case) -> Caller:
        def interrupt(provider: str, model: str, prompt: str, schema: dict[str, object],
                      run_id: str, cancelled: Callable[[], bool], *, effort: str = ""
                      ) -> NativeResult:
            nonlocal calls
            calls += 1
            raise KeyboardInterrupt("synthetic interruption")

        return interrupt

    output = tmp_path / "interrupted"
    with pytest.raises(KeyboardInterrupt, match="synthetic interruption"):
        run_suite(SUITE, output, factory, local_runner, mode="mock", max_calls=1)
    reopened = read_report(output)
    trials = cast(list[dict[str, object]], reopened["trials"])
    assert calls == 1
    assert reopened["expected_trials"] == 2 and reopened["completed_trials"] == 1
    assert reopened["success_rate"] is None
    assert [trial["status"] for trial in trials] == ["interrupted", "queued"]


def test_reopened_error_report_does_not_expose_exception_text(tmp_path: Path) -> None:
    def fail_factory(_: Case) -> Caller:
        raise RuntimeError("secret-sentinel-from-fake-provider")

    output = tmp_path / "safe-error-report"
    run_suite(SUITE, output, fail_factory, local_runner, mode="mock", max_calls=1)
    reopened = read_report(output)
    trials = cast(list[dict[str, object]], reopened["trials"])
    assert reopened["completed_trials"] == 2 and reopened["success_rate"] == 0.0
    assert all(trial["status"] == "error" for trial in trials)
    assert all("RuntimeError during caller setup" == trial["acceptance_report"]
               for trial in trials)
    assert "secret-sentinel" not in "\n".join(report_lines(reopened))


@pytest.mark.parametrize("settlement", [4, 0], ids=["small-settlement", "unbilled-release"])
def test_admission_waits_for_settlement_or_proven_release_then_continues(
    admission_pair: Callable[..., AdmissionPair], settlement: int,
) -> None:
    pair = admission_pair(max_cents=2 * DSF_RESERVATION - 1, settlement=settlement)
    with pair.queued_pair() as waiting_was_observed:
        assert waiting_was_observed
    assert pair.errors == []
    assert {result.outcome for result in pair.results.values()} == {"ok"}
    assert pair.calls == 2 and pair.peak == 1
    assert pair.settled == 2 * settlement
    assert pair.admission is not None
    assert pair.admission.cents == 2 * settlement and pair.admission.inflight == 0


@pytest.mark.parametrize("first_mode", ["unknown", "exception"])
def test_unknown_or_exception_keeps_reservation_and_blocks_after_release(
    admission_pair: Callable[..., AdmissionPair], first_mode: str,
) -> None:
    pair = admission_pair(max_cents=DSF_RESERVATION + 4, first_mode=first_mode)
    with pair.queued_pair() as waiting_was_observed:
        assert waiting_was_observed
    assert pair.calls == 1 and pair.results["second-call"].outcome == "policy_blocked"
    assert pair.admission is not None
    assert pair.admission.cents == DSF_RESERVATION and pair.admission.inflight == 0
    if first_mode == "exception":
        assert len(pair.errors) == 1 and isinstance(pair.errors[0], RuntimeError)
    else:
        assert pair.errors == [] and pair.results["first-call"].outcome == "failed"


def test_waiting_admission_returns_cancelled_promptly(
    admission_pair: Callable[..., AdmissionPair],
) -> None:
    pair = admission_pair(max_cents=DSF_RESERVATION + 4)
    with pair.queued_pair() as waiting_was_observed:
        assert waiting_was_observed
        pair.orchestrator.cancel.set()
        assert pair.completed["second-call"].wait(10)
    assert pair.results["second-call"].outcome == "cancelled"
    assert pair.calls == 1 and pair.admission is not None
    assert pair.admission.calls == 1 and pair.admission.inflight == 0


def test_an_opted_in_suite_stop_below_one_call_blocks_without_waiting(
    admission_pair: Callable[..., AdmissionPair],
) -> None:
    pair = admission_pair(max_cents=DSF_RESERVATION - 1)
    pair.start("permanently-blocked")
    assert pair.completed["permanently-blocked"].wait(10)

    assert pair.results["permanently-blocked"].outcome == "policy_blocked"
    assert pair.calls == 0 and pair.admission is not None
    assert pair.admission.calls == pair.admission.inflight == 0
