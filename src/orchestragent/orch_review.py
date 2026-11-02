"""Immutable test-review inputs; applying a verdict requires the same active inputs."""

import json
import uuid
from dataclasses import asdict, dataclass, field, replace

from orchestragent.envelope import digest
from orchestragent.native import ContextPrompt
from orchestragent.orch_build import contract_hash
from orchestragent.orch_journal import CallJournal
from orchestragent.orch_state import IN_FLIGHT, OrchState, OrchTask, Run
from orchestragent.orch_usage import Call, unresolved
from orchestragent.serialization import dumps
from orchestragent.store import Conflict


def review_version(
    spec: str, task: OrchTask, requirements: dict[str, str], tests: dict[str, str],
) -> str:
    return digest(dumps([spec, task.entry(), tests,
                         {rid: requirements.get(rid, "") for rid in task.reqs}]))


@dataclass(frozen=True)
class Review:
    id: str
    run_id: str
    task_id: str
    version: str
    contract_version: str
    task_entry: dict[str, object]
    shared: str
    dynamic: str
    tests: dict[str, str]
    failure: dict[str, str] = field(default_factory=dict)

    def prompt(self) -> ContextPrompt:
        return ContextPrompt(self.shared, self.dynamic)


class ReviewStore(CallJournal):
    def __init__(self, state: OrchState):
        super().__init__(state.store, "review")
        self.state = state
        with self.store.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS orch_reviews "
                       "(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS orch_review_applied "
                       "(id TEXT PRIMARY KEY REFERENCES orch_reviews(id), "
                       "explanation TEXT NOT NULL)")

    @staticmethod
    def phase(call: Call) -> str:
        # The snapshot persists across execution attempts; the receipt keeps its real attempt.
        return dumps([call.purpose, call.task_id, call.sample])

    def pending(self, run_id: str, task_id: str) -> Review | None:
        with self.store.transaction() as db:
            task = self.state._task(db, run_id, task_id)
            if not task.pending_review:
                return None
            row = db.execute("SELECT body FROM orch_reviews WHERE id=?",
                             (task.pending_review,)).fetchone()
        if row is None:
            raise Conflict("Pending review snapshot missing; inspect evidence")
        saved = Review(**json.loads(row["body"]))
        if (saved.run_id, saved.task_id) != (run_id, task_id):
            raise Conflict("Pending review snapshot belongs to another task")
        return saved

    def begin(
        self, run: Run, task: OrchTask, version: str, prompt: ContextPrompt,
        tests: dict[str, str], failure: dict[str, str] | None = None,
    ) -> Review:
        saved = Review(uuid.uuid4().hex, run.id, task.id, version, contract_hash(task),
                       task.entry(), prompt.shared, prompt.dynamic, tests, dict(failure or {}))
        with self.store.transaction() as db:
            current = self.state._task(db, run.id, task.id)
            if (self.state._run(db, run.id).plan_hash != run.plan_hash
                    or current.entry() != task.entry() or current.attempts != task.attempts
                    or current.status not in IN_FLIGHT or current.pending_review):
                raise Conflict("Review inputs changed before snapshot admission")
            db.execute("INSERT INTO orch_reviews VALUES (?,?)", (saved.id, dumps(asdict(saved))))
            self.state._save_task(db, run.id, replace(current, pending_review=saved.id))
            self.state._event(db, run.id, "task.review_started", {
                "review": saved.id, "version": version,
            }, task.id, task.attempts)
        return saved

    def applied(self, saved: Review) -> str | None:
        with self.store.transaction() as db:
            row = db.execute("SELECT explanation FROM orch_review_applied WHERE id=?",
                             (saved.id,)).fetchone()
        return str(row["explanation"]) if row is not None else None

    def acknowledge_retry(self, saved: Review) -> list[str]:
        """Only the existing explicit task retry can authorize an uncertain repeat."""
        authorized = {call_id for event in self.state.events(saved.run_id, saved.task_id)
                      if event.kind == "task.retry"
                      for ids in [event.data.get("acknowledged_calls", [])]
                      if isinstance(ids, list) for call_id in ids if isinstance(call_id, str)}
        calls = [call for call in self.state.usage.calls(saved.run_id)
                 if call.sample == saved.id and unresolved(call.outcome, call.usage)]
        with self.store.transaction() as db:
            active = {row["call_id"] for row in db.execute(
                "SELECT call_id FROM orch_review_calls WHERE run_id=?", (saved.run_id,))}
        acknowledged = [call.id for call in calls if call.id in authorized and call.id in active]
        if acknowledged:
            self.acknowledge(saved.run_id, acknowledged)
        return [call.id for call in calls if call.id not in authorized]

    def close(self, saved: Review, expected_plan: str, explanation: str | None = None) -> None:
        """Checkpoint upheld verdicts until failure consumes them; discard stale inputs."""
        with self.store.transaction() as db:
            task = self.state._task(db, saved.run_id, saved.task_id)
            if (self.state._run(db, saved.run_id).plan_hash != expected_plan
                    or task.pending_review != saved.id or task.status not in IN_FLIGHT):
                raise Conflict("Review no longer matches its active task")
            if explanation is None:
                candidate = ((task.pending_candidate or task.failed_candidate)
                             if contract_hash(task) == saved.contract_version else "")
                updated = replace(task, status="READY", pending_review="", level=0,
                                  pending_candidate=candidate, failed_candidate="", used_levels=[],
                                  last_signature="", last_diff_hash="", last_error="", last_diff="")
                kind = "task.review_stale"
            else:
                row = db.execute("SELECT explanation FROM orch_review_applied WHERE id=?",
                                 (saved.id,)).fetchone()
                if row is not None:
                    if row["explanation"] != explanation:
                        raise Conflict("Applied review cannot change its verdict")
                    return
                if task.entry() != saved.task_entry:
                    raise Conflict("Reviewed task contract changed before verdict application")
                db.execute("INSERT INTO orch_review_applied VALUES (?,?)", (saved.id, explanation))
                # Keep the pointer until the normal failure transition consumes this decision.
                updated = replace(task, disputes=task.disputes + 1,
                                  claude_attempts=task.claude_attempts + 1,
                                  contract=f"{task.contract}\nReviewer (tests are correct): "
                                           + explanation)
                kind = "task.dispute_rejected"
            self.state._save_task(db, saved.run_id, updated)
            self.state._event(db, saved.run_id, kind, {
                "status": updated.status, "review": saved.id, "version": saved.version,
            }, saved.task_id, task.attempts)
