"""Deterministic, durable control plane; no model may authorize actions."""

import hashlib
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from orchestragent.adapters import Adapter, StepContext, StepResult
from orchestragent.dispatch import Dispatcher
from orchestragent.live import LiveWorkflow
from orchestragent.models import (
    RESUMABLE,
    SCENARIOS,
    STAGES,
    TERMINAL,
    Approval,
    Artifact,
    Step,
    Task,
)
from orchestragent.serialization import dumps
from orchestragent.settings import SettingsStore
from orchestragent.store import Conflict, Store

if TYPE_CHECKING:
    from orchestragent.chat import Chat
    from orchestragent.codeindex import CodeIndex
    from orchestragent.health import HealthMonitor
    from orchestragent.kimi import KimiManual
    from orchestragent.memory import MemoryStore
from orchestragent.workspace import Workspace


class Controller:
    def __init__(
        self,
        store: Store,
        workspace: Workspace,
        adapter: Adapter | None = None,
        clock: Callable[[], float] = time.time,
        lease_seconds: float = 30,
        live: LiveWorkflow | None = None,
    ):
        self.store = store
        self.workspace = workspace
        self.adapter = adapter if adapter is not None else Dispatcher()
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.settings = SettingsStore(store)
        self.live = live
        self.chat: Chat | None = None
        self.memory: MemoryStore | None = None
        self.health: HealthMonitor | None = None
        self.kimi: KimiManual | None = None
        self.index: CodeIndex | None = None
        self.publish: Callable[[Task], str] | None = None

    def submit(
        self, request_id: str, prompt: str, scenario: str = "happy", mode: str = "mock"
    ) -> tuple[Task, bool]:
        if mode not in {"mock", "live"} or (mode == "live" and self.live is None):
            raise ValueError("Execution mode unavailable")
        if scenario != "happy":
            mode = "mock"
        if not request_id or len(request_id) > 128:
            raise ValueError("request_id must contain 1–128 characters")
        if not prompt.strip() or len(prompt) > 8000:
            raise ValueError("prompt must contain 1–8000 characters")
        if scenario not in SCENARIOS:
            raise ValueError("Unknown Mock scenario")
        with self.store.transaction() as db:
            row = db.execute("SELECT body FROM tasks WHERE request_id=?", (request_id,)).fetchone()
            if row:
                task = self.store.decode(row["body"])
                if (task.prompt, task.scenario, task.mode) != (prompt, scenario, mode):
                    raise Conflict("request_id was already used for a different request")
                return task, False
            now = self.clock()
            task = Task(uuid.uuid4().hex, request_id, prompt, scenario, now, now)
            settings = self.settings.snapshot(db)
            policy_revision = settings["revision"]
            policy = settings["policy"]
            assert isinstance(policy_revision, int) and isinstance(policy, dict)
            task.policy_revision = policy_revision
            task.policy = policy
            task.mode = mode
            task.provider = "native" if mode == "live" else "mock"
            task.billing = "subscription" if mode == "live" else "mock"
            task.cost_usd = None if mode == "live" else 0.0
            task.session_id = "" if mode == "live" else f"mock-implement-{task.id}"
            task.steps = [
                Step(
                    stage,
                    stage,
                    [STAGES[i - 1]] if i else [],
                    provider="pending" if mode == "live" else "mock",
                )
                for i, stage in enumerate(STAGES)
            ]
            self.store.save(db, task)
            self.store.event(
                db,
                task,
                "submitted",
                {
                    "provider": task.provider,
                    "cost_usd": task.cost_usd,
                    "mode": mode,
                    "policy_revision": policy_revision,
                },
            )
            return task, True

    def _record(
        self, db: sqlite3.Connection, task: Task, kind: str, data: dict[str, object] | None = None
    ) -> None:
        task.updated_at = self.clock()
        self.store.save(db, task)
        self.store.event(db, task, kind, data or {"status": task.status, "stage": task.stage})

    def recover(self) -> int:
        """Only called by the exclusive server owner after restart. Mock is replay-safe."""
        recovered = 0
        with self.store.transaction() as db:
            for task in self.store.all(db):
                if task.status == "running":
                    task.status = "paused" if task.mode == "live" else "queued"
                    task.error = (
                        "Interrupted native call may have consumed subscription usage; "
                        "resume explicitly"
                        if task.mode == "live"
                        else ""
                    )
                    task.run_token = ""
                    task.lease_until = 0
                    task.steps[STAGES.index(task.stage)].status = "pending"
                    self._record(
                        db, task, "recovered", {"reason": task.error or "Mock replay after restart"}
                    )
                    recovered += 1
        return recovered

    def expire_leases(self) -> int:
        expired = 0
        with self.store.transaction() as db:
            for task in self.store.all(db):
                if task.status == "running" and task.lease_until <= self.clock():
                    task.status = "timed_out"
                    task.error = "Step deadline exceeded; explicit resume required"
                    task.run_token = ""
                    task.steps[STAGES.index(task.stage)].status = "timed_out"
                    self._record(db, task, "timed_out")
                    expired += 1
        return expired

    def claim(self) -> Task | None:
        self.expire_leases()
        with self.store.transaction() as db:
            for task in reversed(self.store.all(db)):
                if task.mode == "live" and self.live is None:
                    continue
                if task.status not in {"queued", "rate_limited"}:
                    continue
                if task.mode == "live" and task.status == "rate_limited":
                    continue
                if task.next_run_at > self.clock():
                    continue
                if any(
                    step.status != "completed" for step in task.steps[: STAGES.index(task.stage)]
                ):
                    raise Conflict("Task dependencies are incomplete")
                limit = task.policy.get("max_task_cents")
                if isinstance(limit, int) and limit > 0:
                    spent = sum(
                        int(entry.get("reserved_cents", 0))
                        for entry in task.usage.values()
                        if isinstance(entry, dict)
                    )
                    if spent >= limit:
                        task.status = "paused"
                        task.error = (
                            f"任务预算上限 {limit} 分已用完（已预留 {spent} 分）；"
                            "提高上限后可以继续，已完成的步骤不会重跑"
                        )
                        task.steps[STAGES.index(task.stage)].status = "paused"
                        self._record(
                            db, task, "budget_stopped", {"limit": limit, "spent": spent}
                        )
                        continue
                task.status = "running"
                task.attempt += 1
                task.error = ""
                task.run_token = uuid.uuid4().hex
                duration = self.lease_seconds
                if task.mode == "live" and self.live is not None:
                    duration = max(duration, self.live.clients.call_timeout("max") + 30)
                task.lease_until = self.clock() + duration
                step = task.steps[STAGES.index(task.stage)]
                step.status = "running"
                if task.mode == "live":
                    if task.stage in {"test", "summarize"}:
                        step.provider, step.model = "local", ""
                    else:
                        try:
                            step.provider, step.model, _ = LiveWorkflow.route(task)
                        except (ValueError, KeyError):
                            step.provider = "unavailable"
                self._record(
                    db,
                    task,
                    "stage_started",
                    {
                        "stage": task.stage,
                        "attempt": task.attempt,
                        "session_id": self._session(task),
                        "revision": task.revision,
                    },
                )
                return task
        return None

    @staticmethod
    def _session(task: Task) -> str:
        if task.mode == "live":
            return task.steps[STAGES.index(task.stage)].session_id
        return f"mock-review-{task.id}" if task.stage == "review" else task.session_id

    def tick(self) -> bool:
        with self.store.activity() as admitted:
            return self._tick() if admitted else False

    def _tick(self) -> bool:
        task = self.claim()
        if task is None:
            return False
        if task.mode == "live":
            assert self.live is not None
            lease_window = max(1.0, task.lease_until - self.clock())

            def cancelled() -> bool:
                with self.store.transaction() as db:
                    current = self.store.get(db, task.id)
                    if current.run_token != task.run_token or current.status != "running":
                        return True
                    now = self.clock()
                    if current.lease_until - now < lease_window / 2:
                        current.lease_until = now + lease_window
                        self.store.save(db, current)
                    return False

            try:
                result, revision, artifacts = self.live.run(task, cancelled)
            except Exception as error:
                from pathlib import Path as _Path

                (_Path(".data") / "logs").mkdir(parents=True, exist_ok=True)
                with (_Path(".data") / "logs" / "controller.err.log").open("a") as log:
                    log.write(f"{task.stage}: {type(error).__name__}: {error}\n")
                step = task.steps[STAGES.index(task.stage)]
                result = StepResult(
                    "failed",
                    "真实执行失败；未尝试计费回退",
                    cost_usd=None,
                    provider=step.provider,
                    model=step.model,
                )
                revision, artifacts = task.revision, []
            self.finish(task, result, revision, artifacts)
            return True
        revision = task.revision
        artifacts = []
        try:
            source = (
                self.workspace.review_source(task.id, revision)
                if task.stage in {"review", "test", "summarize"}
                else ""
            )
            context = StepContext(
                task_id=task.id,
                prompt=task.prompt,
                stage=task.stage,
                scenario=task.scenario,
                attempt=task.attempt,
                session_id=self._session(task),
                revision=revision,
                artifact=source,
                approved=task.approved,
            )
            result = self.adapter.run(context)
            # A stale/cancelled call cannot begin additional filesystem work.
            current = self.store.read(task.id)
            if current.run_token != task.run_token or current.status != "running":
                return True
            if self.clock() >= task.lease_until:
                self.expire_leases()
                return True
            if result.outcome == "ok" and task.stage == "implement":
                revision, diff = self.workspace.implement(task.id, result.artifact)
                artifacts = [
                    Artifact("calculator.py (Mock)", result.artifact),
                    Artifact("implementation.diff", diff),
                ]
            elif result.outcome == "ok" and task.stage == "test":
                passed, report = self.workspace.integrate_and_test(task.id, revision)
                result = replace(result, outcome="ok" if passed else "failed", summary=report)
                artifacts = [Artifact("integration-tests.txt", report)]
        except Exception:
            # Never persist arbitrary exception messages: provider/process errors may carry secrets.
            result = StepResult(
                outcome="failed", summary="Mock step failed; no provider was invoked"
            )
        self.finish(task, result, revision, artifacts)
        return True

    def remember(self, task: Task, step: object, db: sqlite3.Connection | None = None) -> None:
        """Persist a compact stage note for later cross-vendor recall."""
        store = getattr(self, "memory", None)
        remember = getattr(store, "remember", None)
        if not callable(remember) or task.mode != "live":
            return
        from orchestragent.memory import Note

        remember(
            Note(
                project=task.project,
                kind="stage",
                title=f"{task.stage}: {task.prompt[:60]}",
                body=str(getattr(step, "summary", "") or ""),
                task_id=task.id,
                provider=str(getattr(step, "provider", "") or ""),
                model=str(getattr(step, "model", "") or ""),
            ),
            db,
        )

    def finish(
        self, claimed: Task, result: StepResult, revision: str, artifacts: list[Artifact]
    ) -> None:
        with self.store.transaction() as db:
            task = self.store.get(db, claimed.id)
            if task.status != "running" or task.run_token != claimed.run_token:
                return
            if self.clock() >= task.lease_until:
                result = replace(result, outcome="timed_out", summary="Step deadline exceeded")
            task.run_token = ""
            task.lease_until = 0
            step = task.steps[STAGES.index(task.stage)]
            task.revision = revision
            task.artifacts.extend(artifacts)
            # The Mock contract cannot authorize any billed usage.
            if task.mode == "mock" and result.cost_usd != 0:
                result = StepResult(outcome="failed", summary="Unexpected cost; execution blocked")
            step.provider = result.provider
            step.model = result.model
            step.session_id = result.session_id
            if task.mode == "live":
                task.usage[f"{task.stage}:{task.attempt}"] = result.usage
                task.provider = result.provider
                task.session_id = result.session_id or task.session_id
                task.cost_usd = None
                if result.usage.get("billing") == "api":
                    task.billing = "includes_api"
            step.summary = result.summary
            step.revision = revision
            limit = task.policy.get("max_reworks", 3)
            if type(limit) is not int or not 1 <= limit <= 5:
                limit = 3
            if result.outcome == "rework" and task.stage == "review" and task.reworks < limit:
                step.status = "completed"
                task.reworks += 1
                task.stage = "implement"
                task.status = "queued"
                task.steps[STAGES.index("implement")].status = "pending"
                self._record(
                    db,
                    task,
                    "rework_requested",
                    {"attempt": task.reworks, "findings": step.summary[:200]},
                )
                return
            if result.outcome == "ok":
                step.status = "completed"
                self._record(
                    db,
                    task,
                    "stage_completed",
                    {
                        "stage": task.stage,
                        "summary": step.summary,
                        "revision": revision,
                        "session_id": self._session(task),
                        "cost_usd": task.cost_usd,
                        "provider": result.provider,
                        "model": result.model,
                        "usage": result.usage,
                    },
                )
                self.remember(task, step, db)
                index = STAGES.index(task.stage)
                if task.stage == "test" and task.policy.get("require_acceptance") is True:
                    task.status = "waiting_acceptance"
                    task.error = "等待你验收：查看差异与测试结果后接受或打回"
                    self._record(db, task, "acceptance_gate", {"revision": revision})
                    return
                manual = str(task.policy.get("manual_review_provider", "") or "")
                if manual == "kimi" and task.stage == "test":
                    # Subscription terms allow interactive use only, so the pipeline
                    # stops here until a human taps the review action.
                    task.status = "waiting_manual"
                    task.error = "等待手动复核：在 App 里点“用 Kimi 复核”"
                    self._record(db, task, "manual_gate", {"provider": manual})
                    return
                if index == len(STAGES) - 1:
                    task.status = "completed"
                    task.result = (
                        "实现、独立审查和隔离测试已完成。"
                        if task.mode == "live"
                        else "Mock 示例已完成，审查和测试通过。"
                    )
                else:
                    task.stage = STAGES[index + 1]
                    task.status = "queued"
                    task.attempt = 0
                    task.next_run_at = 0
                self._record(db, task, "state_changed")
                return
            allowed = {
                "waiting_approval",
                "policy_blocked",
                "cancelled",
                "rate_limited",
                "quota_exhausted",
                "auth_required",
                "timed_out",
                "failed",
            }
            task.status = result.outcome if result.outcome in allowed else "failed"
            task.error = result.summary
            step.status = task.status
            if task.status == "rate_limited":
                task.next_run_at = self.clock() + max(1, min(result.retry_after, 300))
            if task.status == "waiting_approval" and task.mode == "mock":
                operation = (
                    "Mock：在此任务独立 worktree 写入固定 calculator.py 示例并创建本地提交；"
                    "不启动厂商、不推送，费用 USD 0。"
                )
                digest = hashlib.sha256(
                    dumps(
                        {
                            "task_id": task.id,
                            "stage": task.stage,
                            "operation": operation,
                            "revision": task.revision,
                            "scenario": task.scenario,
                        }
                    ).encode()
                ).hexdigest()
                task.approval = Approval(uuid.uuid4().hex, operation, digest, task.revision)
            if task.status == "waiting_approval" and task.mode == "live":
                operation = (
                    f"确认已在 Mac 完成 {result.provider} 的登录/额度配置后，重试 "
                    f"{task.stage}（模型 {result.model}，版本 {task.revision or '尚无代码'}）。"
                    "本次确认不会修改计费授权或绕过厂商限制。"
                )
                digest = hashlib.sha256(
                    dumps(
                        {"task_id": task.id, "stage": task.stage, "operation": operation}
                    ).encode()
                ).hexdigest()
                task.approval = Approval(uuid.uuid4().hex, operation, digest, task.revision)
            self._record(db, task, "state_changed")

    def action(self, task_id: str, action: str) -> Task:
        with self.store.transaction() as db:
            task = self.store.get(db, task_id)
            if action == "cancel":
                if task.status == "cancelled":
                    return task
                if task.status in TERMINAL:
                    raise Conflict("Terminal tasks cannot be cancelled")
                task.status = "cancelled"
                if task.approval and task.approval.status == "pending":
                    task.approval.status = "cancelled"
            elif action == "pause":
                if task.status == "paused":
                    return task
                if task.status not in {"running", "queued", "rate_limited"}:
                    raise Conflict("Only runnable tasks can be paused")
                task.status = "paused"
            elif action == "retry":
                if task.status != "failed":
                    raise Conflict("Only failed tasks can be retried")
                if task.stage in {"test", "summarize"} and not any(
                    step.status == "completed" for step in task.steps[: STAGES.index(task.stage)]
                ):
                    raise Conflict("Nothing to retry")
                task.status = "queued"
                task.error = ""
                # An explicit human retry grants a fresh rework budget.
                task.reworks = 0
                task.steps[STAGES.index(task.stage)].status = "pending"
            elif action == "resume":
                if task.status not in RESUMABLE:
                    raise Conflict("This state cannot be resumed")
                # A rate limit must respect the vendor's retry window, even after user resume.
                task.status = "queued"
                task.error = ""
            else:
                raise ValueError("Unknown action")
            task.run_token = ""
            task.lease_until = 0
            task.steps[STAGES.index(task.stage)].status = task.status
            self._record(db, task, "user_action", {"action": action, "status": task.status})
            return task

    def accept(self, task_id: str, approved: bool, note: str = "") -> Task:
        """A human decision closes the acceptance gate and nothing else may."""
        with self.store.transaction() as db:
            task = self.store.get(db, task_id)
            if task.status != "waiting_acceptance":
                raise Conflict("This task is not waiting for acceptance")
            task.error = ""
            rejected = not approved
            if rejected:
                task.stage = "implement"
                task.status = "queued"
                task.attempt = 0
                task.reworks += 1
                task.steps[STAGES.index("implement")].status = "pending"
                findings = {"findings": [note[:1000] or "验收未通过"]}
                task.artifacts.append(
                    Artifact("review-findings.json", dumps(findings))
                )
                self._record(db, task, "rejected", {"note": note[:500]})
                return task
        # Publishing touches git and other stores, so it must run outside the
        # transaction; a failure returns the task to the gate with the reason.
        published = ""
        if self.publish is not None:
            try:
                published = self.publish(task)
            except Exception as error:
                with self.store.transaction() as db:
                    failed = self.store.get(db, task_id)
                    failed.error = f"推送失败：{type(error).__name__}: {error}"[:400]
                    self._record(db, failed, "publish_failed", {"error": failed.error})
                    return failed
        with self.store.transaction() as db:
            task = self.store.get(db, task_id)
            if task.status != "waiting_acceptance":
                raise Conflict("This task is no longer waiting for acceptance")
            task.error = ""
            if published:
                task.artifacts.append(Artifact("publish.txt", published))
            task.stage = "summarize"
            task.status = "queued"
            task.attempt = 0
            task.steps[STAGES.index("summarize")].status = "queued"
            self._record(db, task, "accepted", {"note": note[:500]})
            return task

    def manual_review(self, task_id: str, provider: str, text: str) -> Task:
        """Attach a human-triggered review result and let the pipeline continue."""
        if provider != "kimi":
            raise ValueError("Unsupported manual provider")
        with self.store.transaction() as db:
            task = self.store.get(db, task_id)
            if task.status != "waiting_manual":
                raise Conflict("This task is not waiting for a manual review")
            task.artifacts.append(Artifact("manual-review.txt", text[:8000]))
            task.steps[STAGES.index("test")].summary = (
                task.steps[STAGES.index("test")].summary + "（含 Kimi 人工复核）"
            )
            task.stage = "summarize"
            task.status = "queued"
            task.attempt = 0
            task.error = ""
            task.steps[STAGES.index("summarize")].status = "queued"
            self._record(db, task, "manual_review", {"provider": provider})
            return task

    def approve(self, task_id: str, approval_id: str, decision: str, digest: str) -> Task:
        if decision not in {"approve", "deny"}:
            raise ValueError("Unknown approval decision")
        with self.store.transaction() as db:
            task = self.store.get(db, task_id)
            approval = task.approval
            if (
                approval is None
                or approval.id != approval_id
                or approval.operation_digest != digest
            ):
                raise Conflict("Approval does not match this exact operation")
            desired = "approved" if decision == "approve" else "denied"
            if approval.status == desired:
                return task
            if approval.status != "pending" or task.status != "waiting_approval":
                raise Conflict("Approval is no longer pending")
            if approval.revision != task.revision:
                raise Conflict("Revision changed; approval is invalid")
            approval.status = desired
            task.approved = decision == "approve"
            task.status = "queued" if task.approved else "cancelled"
            task.error = "" if task.approved else "User denied this operation"
            task.steps[STAGES.index(task.stage)].status = task.status
            self._record(
                db,
                task,
                "approval_decided",
                {
                    "approval_id": approval_id,
                    "decision": decision,
                    "operation_digest": digest,
                    "actor": "user",
                },
            )
            return task
