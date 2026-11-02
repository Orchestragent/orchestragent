import concurrent.futures
import sqlite3
import threading
from dataclasses import asdict
from pathlib import Path

import pytest

from orchestragent.adapters import MockAdapter, StepContext, StepResult
from orchestragent.controller import Controller
from orchestragent.models import STAGES, Task
from orchestragent.serialization import dumps
from orchestragent.store import Conflict, Store
from orchestragent.workspace import Workspace
from tests.conftest import Clock


def finish(controller: Controller, limit: int = 8) -> Task:
    for _ in range(limit):
        if not controller.tick():
            break
    return controller.store.list_tasks()[0]


def test_complete_pipeline_with_pinned_review_and_integration(controller: Controller) -> None:
    task, _ = controller.submit("request-1", "制作加法示例")
    done = finish(controller)
    events = controller.store.events(task.id)
    completed = [e for e in events if e.kind == "stage_completed"]
    observed = {
        "status": done.status,
        "roles": [e.data["stage"] for e in completed],
        "steps": [step.status for step in done.steps],
        "cost": done.cost_usd,
        "review_pinned": done.steps[2].revision == done.revision != "",
        "independent_review": completed[2].data["session_id"] != completed[1].data["session_id"],
        "integrated": Workspace.git(controller.workspace.repo(task.id), "rev-parse", "HEAD"),
        "tests": done.artifacts[-1].content,
    }
    assert observed == {
        "status": "completed",
        "roles": list(STAGES),
        "steps": ["completed"] * 5,
        "cost": 0,
        "review_pinned": True,
        "independent_review": True,
        "integrated": done.revision,
        "tests": "Mock fixed-fixture integration tests: PASS 2/2",
    }


@pytest.mark.parametrize(
    ("scenario", "status", "stage"),
    [
        ("approval", "waiting_approval", "implement"),
        ("rate_limit", "rate_limited", "implement"),
        ("quota", "quota_exhausted", "implement"),
        ("auth", "auth_required", "implement"),
        ("timeout", "timed_out", "implement"),
        ("failure", "failed", "implement"),
        ("review_failure", "failed", "review"),
        ("test_failure", "failed", "test"),
    ],
)
def test_failure_states_are_distinct(
    controller: Controller, scenario: str, status: str, stage: str
) -> None:
    controller.submit("request", "Mock scenario", scenario)
    task = finish(controller)
    assert (task.status, task.stage, task.provider, task.cost_usd, bool(task.error)) == (
        status,
        stage,
        "mock",
        0,
        True,
    )


@pytest.mark.parametrize("scenario", ["happy", "approval"])
def test_duplicate_submission_after_restart(controller: Controller, scenario: str) -> None:
    original, _ = controller.submit("same", "Original prompt", scenario)
    restarted = Controller(Store(controller.store.path), controller.workspace)
    duplicate, created = restarted.submit("same", "Original prompt", scenario)
    assert (duplicate.id, created, len(restarted.store.events(original.id))) == (
        original.id,
        False,
        1,
    )


@pytest.mark.parametrize(("prompt", "scenario"), [("Changed", "happy"), ("Original", "quota")])
def test_request_id_cannot_change_meaning(
    controller: Controller, prompt: str, scenario: str
) -> None:
    controller.submit("same", "Original")
    with pytest.raises(Conflict):
        controller.submit("same", prompt, scenario)


def test_concurrent_duplicates_are_atomic(controller: Controller) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: controller.submit("same", "Same prompt"), range(8)))
    assert (len({task.id for task, _ in results}), sum(created for _, created in results)) == (1, 1)


@pytest.mark.parametrize("completed_stages", [0, 1, 2, 3, 4])
def test_restart_recovers_claim_without_losing_checkpoints(
    controller: Controller, completed_stages: int
) -> None:
    original, _ = controller.submit("recover", "Recover safely")
    for _ in range(completed_stages):
        controller.tick()
    claim = controller.claim()
    assert claim is not None
    restarted = Controller(
        Store(controller.store.path), controller.workspace, clock=controller.clock
    )
    count = restarted.recover()
    done = finish(restarted)
    assert (
        count,
        done.id,
        done.status,
        len([e for e in restarted.store.events(original.id) if e.kind == "recovered"]),
    ) == (1, original.id, "completed", 1)


def test_replay_after_git_commit_before_database_checkpoint(controller: Controller) -> None:
    controller.submit("recover", "Crash after write")
    controller.tick()
    claim = controller.claim()
    assert claim is not None
    source = "def add(a: int, b: int) -> int:\n    return a + b\n"
    first_revision, _ = controller.workspace.implement(claim.id, source)
    controller.recover()
    done = finish(controller)
    assert (done.status, done.revision) == ("completed", first_revision)


@pytest.mark.parametrize("decision,expected", [("approve", "completed"), ("deny", "cancelled")])
def test_approval_survives_restart_and_is_idempotent(
    controller: Controller, decision: str, expected: str
) -> None:
    controller.submit("approval", "Requires operation approval", "approval")
    waiting = finish(controller)
    approval = waiting.approval
    assert approval is not None
    restarted = Controller(
        Store(controller.store.path), controller.workspace, clock=controller.clock
    )
    restarted.recover()
    restarted.approve(waiting.id, approval.id, decision, approval.operation_digest)
    restarted.approve(waiting.id, approval.id, decision, approval.operation_digest)
    done = finish(restarted)
    events = [e for e in restarted.store.events(waiting.id) if e.kind == "approval_decided"]
    assert (done.status, len(events), events[0].data["actor"]) == (expected, 1, "user")


@pytest.mark.parametrize("mutation", ["digest", "approval_id", "task_id", "revision", "cancel"])
def test_approval_is_bound_to_exact_pending_operation(
    controller: Controller, mutation: str
) -> None:
    controller.submit("approval", "Bound approval", "approval")
    waiting = finish(controller)
    approval = waiting.approval
    assert approval is not None
    task_id = waiting.id
    approval_id = approval.id
    digest = approval.operation_digest
    if mutation == "digest":
        digest = "wrong"
    elif mutation == "approval_id":
        approval_id = "wrong"
    elif mutation == "task_id":
        task_id = controller.submit("another", "Another task")[0].id
    elif mutation == "revision":
        with controller.store.transaction() as db:
            waiting.revision = "changed"
            controller.store.save(db, waiting)
    elif mutation == "cancel":
        controller.action(task_id, "cancel")
    with pytest.raises(Conflict):
        controller.approve(task_id, approval_id, "approve", digest)


def test_rate_limit_persists_retry_window(controller: Controller, clock: Clock) -> None:
    controller.submit("rate", "Rate limited", "rate_limit")
    waiting = finish(controller)
    controller.action(waiting.id, "resume")
    assert controller.tick() is False
    clock.now = waiting.next_run_at + 1
    done = finish(controller)
    assert (done.status, done.provider, done.cost_usd) == ("completed", "mock", 0)


@pytest.mark.parametrize("scenario", ["quota", "auth", "timeout"])
def test_blocked_states_never_retry_or_switch_provider(
    controller: Controller, clock: Clock, scenario: str
) -> None:
    controller.submit("blocked", "Blocked", scenario)
    before = finish(controller)
    clock.now += 1_000_000
    progressed = controller.tick()
    after = controller.store.read(before.id)
    assert (progressed, asdict(after)) == (False, asdict(before))


@pytest.mark.parametrize("action,status", [("cancel", "cancelled"), ("pause", "paused")])
def test_stale_worker_cannot_overwrite_user_control(
    controller: Controller, action: str, status: str
) -> None:
    submitted, _ = controller.submit("control", "Control task")
    claimed = controller.claim()
    assert claimed is not None
    controller.action(submitted.id, action)
    controller.finish(claimed, StepResult(summary="Late result"), "", [])
    assert controller.store.read(submitted.id).status == status


def test_paused_task_resumes_from_checkpoint(controller: Controller) -> None:
    task, _ = controller.submit("pause", "Pause between stages")
    controller.tick()
    controller.action(task.id, "pause")
    controller.action(task.id, "resume")
    done = finish(controller)
    assert (
        done.status,
        len([e for e in controller.store.events(task.id) if e.kind == "stage_completed"]),
    ) == ("completed", 5)


class BlockingAdapter(MockAdapter):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, context: StepContext) -> StepResult:
        self.started.set()
        self.release.wait(timeout=3)
        return super().run(context)


def test_watchdog_expires_blocked_step_and_rejects_late_result(
    controller: Controller, clock: Clock
) -> None:
    adapter = BlockingAdapter()
    controller.adapter = adapter
    task, _ = controller.submit("deadline", "Deadline")
    worker = threading.Thread(target=controller.tick)
    worker.start()
    try:
        assert adapter.started.wait(timeout=2)
        clock.now += controller.lease_seconds + 1
        expired = controller.expire_leases()
    finally:
        adapter.release.set()
        worker.join(timeout=5)
    assert (expired, controller.store.read(task.id).status) == (1, "timed_out")


def test_events_reconnect_from_cursor_without_duplicates(controller: Controller) -> None:
    task, _ = controller.submit("events", "Reconnect")
    first = controller.store.events(task.id)
    finish(controller)
    later = controller.store.events(task.id, first[-1].seq)
    seqs = [e.seq for e in first + later]
    assert (seqs, len(seqs)) == (sorted(set(seqs)), len(controller.store.events(task.id)))


def test_state_and_event_rollback_together(controller: Controller) -> None:
    task, _ = controller.submit("atomic", "Atomic persistence")
    with pytest.raises(sqlite3.IntegrityError), controller.store.transaction() as db:
        task.status = "completed"
        controller.store.save(db, task)
        db.execute("INSERT INTO events(task_id, kind, at, data) VALUES('missing','test',0,'{}')")
    assert controller.store.read(task.id).status == "queued"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_numbers_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        dumps({"cost": value})


@pytest.mark.parametrize("task_id", ["../escape", "a" * 31, "/" * 32, "g" * 32])
def test_workspace_path_traversal_rejected(controller: Controller, task_id: str) -> None:
    with pytest.raises(ValueError):
        controller.workspace.repo(task_id)


def test_arbitrary_code_cannot_enter_mock_workspace(controller: Controller, tmp_path: Path) -> None:
    task, _ = controller.submit("code", "User code must never execute")
    with pytest.raises(ValueError):
        controller.workspace.implement(task.id, "import os\nos.system('false')\n")


class LeakingAdapter(MockAdapter):
    def run(self, context: StepContext) -> StepResult:
        raise RuntimeError("FAKE_SECRET_SHOULD_NOT_APPEAR")


def test_internal_exception_text_does_not_enter_events(controller: Controller) -> None:
    controller.adapter = LeakingAdapter()
    task, _ = controller.submit("secret", "Do not log raw exceptions")
    finish(controller)
    data = dumps(asdict(controller.store.read(task.id))) + dumps(
        [asdict(e) for e in controller.store.events(task.id)]
    )
    assert "FAKE_SECRET_SHOULD_NOT_APPEAR" not in data


def test_task_budget_cap_pauses_before_spending_more(controller: Controller, clock: object) -> None:
    task, _ = controller.submit("budget-1", "需求", mode="mock")
    with controller.store.transaction() as db:
        stored = controller.store.get(db, task.id)
        stored.policy = {"max_task_cents": 300}
        stored.usage = {"plan:1": {"reserved_cents": 300}}
        stored.status = "queued"
        stored.stage = "implement"
        stored.steps[0].status = "completed"
        stored.steps[1].status = "pending"
        controller.store.save(db, stored)
    assert controller.claim() is None
    paused = controller.store.read(task.id)
    assert (paused.status, paused.steps[1].status) == ("paused", "paused")
    assert "预算上限" in paused.error


def test_task_budget_cap_leaves_room_below_limit(controller: Controller) -> None:
    task, _ = controller.submit("budget-2", "需求", mode="mock")
    with controller.store.transaction() as db:
        stored = controller.store.get(db, task.id)
        stored.policy = {"max_task_cents": 900}
        stored.usage = {"plan:1": {"reserved_cents": 300}}
        stored.status = "queued"
        stored.stage = "implement"
        stored.steps[0].status = "completed"
        stored.steps[1].status = "pending"
        controller.store.save(db, stored)
    claimed = controller.claim()
    assert claimed is not None and claimed.status == "running"


def test_failed_stage_can_be_retried_without_replaying_completed_steps(
    controller: Controller,
) -> None:
    task, _ = controller.submit("retry-1", "需求")
    for _ in range(6):
        controller.tick()
    with controller.store.transaction() as db:
        stored = controller.store.get(db, task.id)
        stored.status = "failed"
        stored.stage = "review"
        stored.steps[0].status = "completed"
        stored.steps[1].status = "completed"
        stored.steps[2].status = "failed"
        controller.store.save(db, stored)
    with pytest.raises(Conflict):
        controller.action(task.id, "resume")
    retried = controller.action(task.id, "retry")
    assert (retried.status, retried.stage) == ("queued", "review")
    assert retried.steps[2].status == "queued"
    assert retried.steps[0].status == "completed"


def test_retry_rejects_non_failed_tasks(controller: Controller) -> None:
    task, _ = controller.submit("retry-2", "需求")
    with pytest.raises(Conflict):
        controller.action(task.id, "retry")


def test_review_rejection_returns_to_implementation(controller: Controller) -> None:
    task, _ = controller.submit("rework-1", "需要返工的需求")
    with controller.store.transaction() as db:
        stored = controller.store.get(db, task.id)
        stored.stage = "review"
        stored.status = "running"
        stored.steps[0].status = "completed"
        stored.steps[1].status = "completed"
        stored.steps[2].status = "running"
        stored.run_token = "token"
        stored.lease_until = controller.clock() + 60
        controller.store.save(db, stored)
    controller.finish(
        stored,
        StepResult("rework", "缺少边界测试", cost_usd=0.0, provider="claude", model="sonnet"),
        "rev-1",
        [],
    )
    after = controller.store.read(stored.id)
    assert (after.stage, after.status, after.reworks) == ("implement", "queued", 1)
    assert after.steps[2].status == "completed"
    kinds = [event.kind for event in controller.store.events(stored.id)]
    assert "rework_requested" in kinds


def test_rework_stops_at_the_default_cap(controller: Controller) -> None:
    task, _ = controller.submit("rework-2", "需要返工两次的需求")
    with controller.store.transaction() as db:
        stored = controller.store.get(db, task.id)
        stored.stage = "review"
        stored.status = "running"
        stored.steps[0].status = "completed"
        stored.steps[1].status = "completed"
        stored.steps[2].status = "running"
        stored.reworks = 3
        stored.run_token = "token"
        stored.lease_until = controller.clock() + 60
        controller.store.save(db, stored)
    controller.finish(
        stored,
        StepResult("rework", "仍然不合格", cost_usd=0.0, provider="claude", model="sonnet"),
        "rev-2",
        [],
    )
    after = controller.store.read(stored.id)
    assert (after.status, after.stage) == ("failed", "review")


def test_finish_records_memory_inside_the_open_transaction(
    controller: Controller,
) -> None:
    from orchestragent.memory import MemoryStore

    controller.memory = MemoryStore(controller.store)
    task, _ = controller.submit("memory-1", "记忆写入需求")
    with controller.store.transaction() as db:
        stored = controller.store.get(db, task.id)
        stored.stage = "plan"
        stored.mode = "live"
        stored.status = "running"
        stored.steps[0].status = "running"
        stored.run_token = "token"
        stored.lease_until = controller.clock() + 60
        controller.store.save(db, stored)
    controller.finish(
        stored,
        StepResult("ok", "规划完成", cost_usd=None, provider="claude", model="sonnet"),
        "rev-1",
        [],
    )
    from orchestragent.memory import MemoryStore as Store2

    assert Store2(controller.store).stats()["notes"] == 1


def test_parallel_workers_never_claim_the_same_task(controller: Controller) -> None:
    import threading

    for index in range(4):
        controller.submit(f"parallel-{index}", f"需求 {index}")
    claimed: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(4):
            task = controller.claim()
            if task is None:
                return
            with lock:
                claimed.append(task.id)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert len(claimed) == len(set(claimed)) == 4


def test_acceptance_gate_requires_a_human_decision(controller: Controller) -> None:
    task, _ = controller.submit("accept-1", "需要验收的需求")
    with controller.store.transaction() as db:
        stored = controller.store.get(db, task.id)
        stored.mode = "live"
        stored.policy = {"require_acceptance": True}
        stored.stage = "test"
        stored.status = "running"
        for index in range(3):
            stored.steps[index].status = "completed"
        stored.steps[3].status = "running"
        stored.run_token = "token"
        stored.lease_until = controller.clock() + 60
        controller.store.save(db, stored)
    controller.finish(
        stored,
        StepResult("ok", "测试通过", cost_usd=None, provider="local", model=""),
        "rev-1",
        [],
    )
    waiting = controller.store.read(stored.id)
    assert (waiting.status, waiting.stage) == ("waiting_acceptance", "test")
    accepted = controller.accept(stored.id, True, "符合预期")
    assert (accepted.status, accepted.stage) == ("queued", "summarize")


def test_rejection_returns_to_implementation_with_a_reason(controller: Controller) -> None:
    task, _ = controller.submit("accept-2", "需要打回的需求")
    with controller.store.transaction() as db:
        stored = controller.store.get(db, task.id)
        stored.mode = "live"
        stored.stage = "test"
        stored.status = "waiting_acceptance"
        for index in range(3):
            stored.steps[index].status = "completed"
        stored.steps[3].status = "completed"
        controller.store.save(db, stored)
    rejected = controller.accept(stored.id, False, "缺少边界测试")
    assert (rejected.status, rejected.stage, rejected.reworks) == ("queued", "implement", 1)
    assert any(a.name == "review-findings.json" for a in rejected.artifacts)


def test_accepting_publishes_and_records_the_branch(controller: Controller) -> None:
    task, _ = controller.submit("publish-1", "要推送的需求")
    with controller.store.transaction() as db:
        stored = controller.store.get(db, task.id)
        stored.status = "waiting_acceptance"
        stored.stage = "test"
        controller.store.save(db, stored)
    controller.publish = lambda _task: "branch=task/abc\ncommit=deadbeef\n"
    accepted = controller.accept(stored.id, True)
    assert accepted.status == "queued"
    assert any(item.name == "publish.txt" for item in accepted.artifacts)


def test_a_failed_push_keeps_the_task_waiting(controller: Controller) -> None:
    task, _ = controller.submit("publish-2", "推送会失败的需求")
    with controller.store.transaction() as db:
        stored = controller.store.get(db, task.id)
        stored.status = "waiting_acceptance"
        stored.stage = "test"
        controller.store.save(db, stored)

    def boom(_task: object) -> str:
        raise ValueError("remote rejected")

    controller.publish = boom
    still = controller.accept(stored.id, True)
    assert still.status == "waiting_acceptance"
    assert "推送失败" in still.error
