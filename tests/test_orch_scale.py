import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from orchestragent.envelope import digest
from orchestragent.native import NativeResult
from orchestragent.orch_audit import audit
from orchestragent.orch_build import shared_context
from orchestragent.orch_cli import main, status_lines
from orchestragent.orch_pace import TokenBucket
from orchestragent.orch_run import DSF, Deferred, Orchestrator
from orchestragent.orch_state import OrchTask, Run
from orchestragent.orch_synthetic import Shape, SyntheticBuilder, synthetic_plan
from orchestragent.workspace import Workspace
from tests.conftest import GOOD_ADD, edit_result, local_runner


@dataclass
class FakeTime:
    """A clock whose sleep advances it: pacing is tested without waiting."""

    now: float = 1000.0
    slept: list[float] = field(default_factory=list)

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_token_bucket_bursts_then_paces_and_stops_when_cancelled() -> None:
    fake = FakeTime()
    bucket = TokenBucket(60, fake.clock, fake.sleep)
    assert all(bucket.take(lambda: False) for _ in range(10)) and not fake.slept
    assert bucket.take(lambda: False) and fake.slept == [1.0]
    assert not bucket.take(lambda: True)
    with pytest.raises(ValueError):
        TokenBucket(0, fake.clock, fake.sleep)


def test_cancellation_cannot_spend_a_full_bucket_or_start_another_task(
    orchestrator: Orchestrator,
) -> None:
    fake = FakeTime()
    bucket = TokenBucket(60, fake.clock, fake.sleep)
    assert not bucket.take(lambda: True) and bucket.tokens == bucket.capacity
    orchestrator.cancel.set()
    assert orchestrator._room(0) == 0 and orchestrator.stop_reason


def test_verification_queue_cancellation_releases_no_unowned_slot(
    orchestrator: Orchestrator, orch_repo: Path,
) -> None:
    orchestrator.verify_slots = threading.BoundedSemaphore(1)
    orchestrator.verify_slots.acquire()
    orchestrator.cancel.set()
    with pytest.raises(Deferred, match="waiting for verification"):
        orchestrator.verify(orch_repo, orch_repo, "HEAD", [])
    assert not orchestrator.verify_slots.acquire(blocking=False)
    orchestrator.verify_slots.release()


def test_critical_path_order_handles_a_deep_dag_without_recursion(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks = [OrchTask(f"T-{i:04}", "node", [], [], [f"T-{i - 1:04}"] if i else [], [], "",
                      status="READY") for i in range(1500)]
    monkeypatch.setattr(orchestrator.state, "tasks", lambda _: tasks)
    ready = orchestrator._ready("deep", set())
    assert len(ready) == 1500 and ready[0].id == "T-0000"


def synthetic(
    repo: Path, shape: Shape, wrong: float = 0.0, limited: float = 0.0
) -> tuple[Orchestrator, SyntheticBuilder]:
    plan, bodies = synthetic_plan(shape)
    builder = SyntheticBuilder(plan, bodies, wrong=wrong, limited=limited)
    fake = FakeTime()  # rate-limit cooldowns pass instantly
    orchestrator = Orchestrator(repo, builder, local_runner, notify=lambda line: None,
                                clock=fake.clock, sleep=fake.sleep)
    return orchestrator, builder


def test_a_wide_run_merges_everything_and_passes_the_audit(orch_repo: Path) -> None:
    shape = Shape(tasks=40, packages=5, fan_in=2)
    orchestrator, builder = synthetic(orch_repo, shape, wrong=0.2, limited=0.05)
    run = orchestrator.execute(orchestrator.plan("synthetic").id, jobs=6)
    events = orchestrator.state.events(run.id)
    metrics = next(e.data for e in events if e.kind == "run.metrics")
    gates = [e.data for e in events if e.kind == "task.gate"]
    wrong = sum(builder._share(f"T-{n:03d}", "wrong", 0.2) for n in range(1, 41))
    assert run.status == "done" and audit(orchestrator, run.id) == []
    assert sum(e.kind == "task.done" for e in events) == 40
    assert sum(e.kind == "task.failed" for e in events) == wrong > 0
    assert sum(int(str(g["skipped"])) for g in gates) > sum(int(str(g["tests"])) for g in gates)
    assert int(str(metrics["worktrees_reused"])) > int(str(metrics["worktrees_created"]))
    assert 1 < int(str(metrics["peak_generating"])) <= 6


@dataclass
class Overlap:
    """A builder that records how many calls are in flight when each one starts."""

    inner: Callable[..., NativeResult]
    active: int = 0
    seen: list[int] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self, *args: object, **kwargs: object) -> NativeResult:
        with self.lock:
            self.active += 1
            self.seen.append(self.active)
        try:
            time.sleep(0.05)
            return self.inner(*args, **kwargs)
        finally:
            with self.lock:
                self.active -= 1


def test_unknown_cache_history_does_not_serialize_the_first_calls(orch_repo: Path) -> None:
    orchestrator, builder = synthetic(orch_repo, Shape(tasks=8, packages=8, fan_in=0))
    run = orchestrator.plan("synthetic")
    overlap = Overlap(builder)
    orchestrator.call = overlap
    orchestrator.max_jobs = orchestrator.limit = 4
    assert orchestrator._room(0) == 4
    assert orchestrator.execute(run.id, jobs=4).status == "done"
    assert overlap.seen[0] == 1 and max(overlap.seen) > 1


@pytest.mark.parametrize("critical", [False, True])
def test_shared_project_cache_keeps_critical_path_order(
    orch_repo: Path, critical: bool,
) -> None:
    orchestrator, builder = synthetic(orch_repo, Shape(tasks=4, packages=2, fan_in=0))
    if critical:
        cast(list[dict[str, object]], builder.plan["tasks"])[2]["depends_on"] = ["T-002"]
    run = orchestrator.plan("synthetic")
    orchestrator._promote(run.id)
    prefix = digest(shared_context(run.repo_map, "", run.spec))[:12]
    orchestrator.cache_policy.observe((DSF.provider, DSF.model, prefix), 980, 990, {
        "input_tokens": 20, "output_tokens": 10, "total_tokens": 110,
        "cache_read_input_tokens": 80, "cache_creation_input_tokens": 0,
    })
    assert orchestrator._ready(run.id, set())[0].id == ("T-002" if critical else "T-001")
    assert {key[2] for key in orchestrator.ready_cache_keys.values()} == {prefix}
    orchestrator.cache_mode = "off"
    assert orchestrator._ready(run.id, set())[0].id == ("T-002" if critical else "T-001")


def test_cache_wait_deadline_is_reconsidered_while_warming_call_stalls(
    orchestrator: Orchestrator, independent_run: Run, monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_started = threading.Event()
    original_ready = orchestrator._ready

    def ready(run_id: str, busy: set[str]) -> list[OrchTask]:
        tasks = original_ready(run_id, busy)
        orchestrator.ready_cache_keys = {t.id: ("dsh", "deepseek-flash", t.id) for t in tasks}
        return tasks

    def call(*args: Any, **kwargs: Any) -> NativeResult:
        if "T-001" in args[4]:
            assert second_started.wait(2), "cache wait exceeded its deadline"
            return GOOD_ADD
        second_started.set()
        return edit_result("doubler.py", "return a * 2")

    monkeypatch.setattr(orchestrator, "_ready", ready)
    monkeypatch.setattr("orchestragent.orch_cache.CachePolicy.delay",
                        lambda self, key: 0.2 if key[2] == "T-002" else 0)
    orchestrator.call = call
    # The fixture clock is fixed; use a monotonic clock for this scheduling boundary.
    orchestrator.clock = time.monotonic
    result = orchestrator.execute(independent_run.id, jobs=2)
    assert result.status == "done" and second_started.is_set()
    assert any(e.kind == "task.cache_wait" for e in orchestrator.state.events(result.id))


def test_probe_trusts_no_report_whose_tail_was_cut(orch_repo: Path) -> None:
    def tail_only(execution: Path, scratch: Path, revision: str, names: list[str] | None,
                  *, timeout: float = 25.0) -> tuple[bool, str]:
        passed, report = local_runner(execution, scratch, revision, names)
        return passed, report[-1500:]  # the sandbox keeps only the end of long output

    orchestrator, _ = synthetic(orch_repo, Shape(tasks=40, packages=4, fan_in=0))
    orchestrator.runner = tail_only
    run = orchestrator.plan("synthetic")
    assert not [e for e in orchestrator.state.events(run.id) if e.kind == "plan.vacuous_tests"]


def test_probe_runs_each_task_once_however_long_its_failing_report(orch_repo: Path) -> None:
    probes: list[list[str]] = []

    def counting(execution: Path, scratch: Path, revision: str, names: list[str] | None,
                 *, timeout: float = 25.0) -> tuple[bool, str]:
        passed, report = local_runner(execution, scratch, revision, names)
        if "/probe-" in str(execution):
            probes.append(list(names or []))
        return passed, report[-200:]  # nothing in the report could name every failure

    orchestrator, _ = synthetic(orch_repo, Shape(tasks=12, packages=3, fan_in=0))
    orchestrator.runner = counting
    run = orchestrator.plan("synthetic")
    tasks = orchestrator.state.tasks(run.id)
    assert sorted(map(sorted, probes)) == sorted(sorted(t.tests) for t in tasks)
    assert not [e for e in orchestrator.state.events(run.id) if e.kind == "plan.vacuous_tests"]


def test_generation_resumes_while_earlier_tasks_still_verify(orch_repo: Path) -> None:
    orchestrator, builder = synthetic(orch_repo, Shape(tasks=4, packages=4, fan_in=0))
    run = orchestrator.plan("synthetic")
    marks: list[tuple[str, float]] = []

    def slow(execution: Path, scratch: Path, revision: str, names: list[str] | None,
             *, timeout: float = 25.0) -> tuple[bool, str]:
        time.sleep(0.2)
        result = local_runner(execution, scratch, revision, names)
        marks.append(("verified", time.monotonic()))
        return result

    def call(*args: object, **kwargs: object) -> NativeResult:
        marks.append(("call", time.monotonic()))
        return builder(*args, **kwargs)  # type: ignore[arg-type]

    orchestrator.runner, orchestrator.call = slow, call
    assert orchestrator.execute(run.id, jobs=4).status == "done"
    calls = [at for kind, at in marks if kind == "call"]
    first_verified = min(at for kind, at in marks if kind == "verified")
    # Model-call slots remain available while earlier tasks wait for sandbox results.
    assert calls[0] < calls[1] < first_verified


def test_verification_has_its_own_bounded_pool(orch_repo: Path) -> None:
    orchestrator, _ = synthetic(orch_repo, Shape(tasks=6, packages=6, fan_in=0))
    run = orchestrator.execute(orchestrator.plan("synthetic").id, jobs=4, verify_jobs=1)
    metrics = next(e.data for e in orchestrator.state.events(run.id) if e.kind == "run.metrics")
    assert run.status == "done" and metrics["peak_verifying"] == 1


def test_spend_cap_pauses_the_run_and_resume_keeps_counting(orch_repo: Path) -> None:
    orchestrator, _ = synthetic(orch_repo, Shape(tasks=6, packages=2, fan_in=0))
    planned = orchestrator.plan("synthetic")
    paused = orchestrator.execute(planned.id, jobs=1, max_cents=2)
    done = sum(t.status == "DONE" for t in orchestrator.state.tasks(planned.id))
    assert (paused.status, paused.message, done) == ("paused", "run spend cap ¥0.02 reached", 2)
    assert orchestrator.execute(planned.id, jobs=1, max_cents=3).status == "paused"
    assert orchestrator.execute(planned.id, jobs=2).status == "done"
    assert orchestrator.state.spent(planned.id) == 6


def test_request_rate_paces_builder_calls(orch_repo: Path) -> None:
    fake = FakeTime()
    plan, bodies = synthetic_plan(Shape(tasks=12, packages=4, fan_in=0))
    orchestrator = Orchestrator(orch_repo, SyntheticBuilder(plan, bodies), local_runner,
                                notify=lambda line: None, clock=fake.clock, sleep=fake.sleep)
    run = orchestrator.execute(orchestrator.plan("synthetic").id, jobs=4, rpm=60)
    metrics = next(e.data for e in orchestrator.state.events(run.id) if e.kind == "run.metrics")
    started = next(e.data for e in orchestrator.state.events(run.id) if e.kind == "run.started")
    assert run.status == "done" and int(str(metrics["paced_seconds"])) >= 1
    assert started["rpm"] == 60


def test_wide_runs_show_aggregates_and_the_audit_catches_tampering(
    orch_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    orchestrator, _ = synthetic(orch_repo, Shape(tasks=32, packages=4, fan_in=1), wrong=0.1)
    run = orchestrator.execute(orchestrator.plan("synthetic").id, jobs=8)
    summary = status_lines(orchestrator.state, run, full=False)
    assert summary[1:3] == [f"  all 32 task(s) merged on orch/{run.id}", "  32 task(s): DONE 32"]
    assert summary[-1] == f"  (`orch status --all {run.id}` lists every task)"
    assert len(status_lines(orchestrator.state, run)) == 2 + 32  # header, message, tasks
    Workspace.git(orch_repo, "tag", "-f", f"orch/{run.id}/done", f"orch/{run.id}/plan")
    assert main(["--repo", str(orch_repo), "audit", run.id, "--no-tests"]) == 1
    assert "done tag" in capsys.readouterr().out
