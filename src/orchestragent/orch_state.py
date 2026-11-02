"""Durable orchestration state: one run, a DAG of tasks, and a span-linked event log.

Only the scheduler writes here. Every state change and its event share a transaction,
so a killed process resumes from the log instead of a transcript.
"""

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from orchestragent.envelope import digest
from orchestragent.orch_graph import replacement_frontier
from orchestragent.orch_rules import KINDS, Rule
from orchestragent.orch_usage import Call, Ledger, unresolved, usage_fields
from orchestragent.serialization import dumps
from orchestragent.store import Conflict, Store

# Main line PLANNED -> READY -> RUNNING -> VERIFYING -> MERGING -> DONE, plus PARKED and
# REPLANNED (a terminal hand-over to replacement tasks).
# DONE reopens only when the final gate bisects a regression to it (or a dependency).
TRANSITIONS: dict[str, frozenset[str]] = {
    "PLANNED": frozenset({"READY", "REPLANNED", "PARKED"}),
    "READY": frozenset({"RUNNING", "PARKED"}),
    "RUNNING": frozenset({"VERIFYING", "READY", "PARKED", "REPLANNED"}),
    "VERIFYING": frozenset({"MERGING", "READY", "PARKED", "PLANNED"}),  # PLANNED: waits
    "MERGING": frozenset({"DONE", "READY", "PARKED"}),
    "DONE": frozenset({"READY", "PLANNED"}),
    "PARKED": frozenset({"READY"}),
    "REPLANNED": frozenset(),
}
FINISHED = frozenset({"DONE", "REPLANNED"})
IN_FLIGHT = frozenset({"RUNNING", "VERIFYING", "MERGING"})


@dataclass
class OrchTask:
    id: str
    goal: str
    reqs: list[str]
    files: list[str]
    depends_on: list[str]
    tests: list[str]
    contract: str
    status: str = "PLANNED"
    attempts: int = 0
    level: int = 0  # failure-ladder rung of the next attempt (see orch_ladder)
    used_levels: list[int] = field(default_factory=list)
    claude_attempts: int = 0
    rebases: int = 0
    disputes: int = 0
    generation: int = 0  # 0 for planned tasks, +1 for each replan that produced it
    last_signature: str = ""
    last_diff_hash: str = ""
    last_error: str = ""
    last_diff: str = ""
    commit: str = ""
    reason: str = ""
    mutation: float = -1.0  # mutation score of its tests at `mutated_at`; -1 unmeasured
    mutated_at: str = ""
    context_requests: int = 0
    context_selectors: list[str] = field(default_factory=list)
    pending_candidate: str = ""  # immutable commit; resumes verification without a model call
    failed_candidate: str = ""  # structurally valid code; reconsider only after tests change
    interface_depends_on: list[str] = field(default_factory=list)  # subset of integration edges
    input_revisions: list[str] = field(default_factory=list)  # bounded automatic contract refreshes
    pending_review: str = ""  # immutable input snapshot; resume it before another implementation
    human_hint: str = ""  # guidance a person gave this task (`orch hint`); every later attempt

    def entry(self) -> dict[str, object]:
        """This task as a plan entry (the shape the planner and lint use)."""
        entry: dict[str, object] = {
            "id": self.id,
            "goal": self.goal,
            "reqs": self.reqs,
            "files": self.files,
            "depends_on": self.depends_on,
            "tests": self.tests,
            "contract": self.contract,
        }
        if self.interface_depends_on:
            entry["interface_depends_on"] = list(self.interface_depends_on)
        return entry


def plan_hash(plan: dict[str, object]) -> str:
    return digest(dumps(plan))[:12]


@dataclass
class Run:
    id: str
    repo: str
    base: str
    branch: str
    spec: str
    plan: dict[str, object]
    plan_hash: str
    repo_map: str
    created_at: float
    status: str = "planned"
    message: str = ""
    checkpoint: str = ""
    baseline: list[str] = field(default_factory=list)  # pre-existing tests green at base
    regressions: int = 0
    # Integration HEAD whose every commit passed its gate; resume rolls back to it.
    verified_head: str = ""
    policy_id: str = ""  # immutable decision snapshot, empty for legacy online trials
    environment_root: str = ""
    environment_fingerprint: str = ""
    strategy: str = "opus-dsf"


@dataclass(frozen=True)
class OrchEvent:
    seq: int
    run_id: str
    task_id: str
    span: str
    parent: str
    kind: str
    at: float
    data: dict[str, object] = field(default_factory=dict)


def span_of(run_id: str, task_id: str = "", attempt: int = 0) -> tuple[str, str]:
    """(span, parent) for the run -> task -> attempt chain."""
    if not task_id:
        return run_id, ""
    if not attempt:
        return f"{run_id}/{task_id}", run_id
    return f"{run_id}/{task_id}/a{attempt}", f"{run_id}/{task_id}"


class OrchState:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.store = Store(path)
        self.clock = clock
        self.usage = Ledger(self.store, clock)
        with self.store.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS orch_runs (
                    id TEXT PRIMARY KEY, created_at REAL NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS orch_tasks (
                    run_id TEXT NOT NULL REFERENCES orch_runs(id), id TEXT NOT NULL,
                    status TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(run_id, id));
                CREATE TABLE IF NOT EXISTS orch_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES orch_runs(id),
                    task_id TEXT NOT NULL, span TEXT NOT NULL,
                    parent TEXT NOT NULL CHECK (parent <> '' OR span = run_id),
                    kind TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS orch_events_run ON orch_events(run_id, seq);
                CREATE TABLE IF NOT EXISTS orch_rules (id TEXT PRIMARY KEY, body TEXT NOT NULL);
            """)

    # --- writes ---------------------------------------------------------
    def _event(
        self,
        db: sqlite3.Connection,
        run_id: str,
        kind: str,
        data: dict[str, object],
        task_id: str = "",
        attempt: int = 0,
    ) -> None:
        span, parent = span_of(run_id, task_id, attempt)
        db.execute(
            "INSERT INTO orch_events(run_id,task_id,span,parent,kind,at,data) "
            "VALUES(?,?,?,?,?,?,?)",
            (run_id, task_id, span, parent, kind, self.clock(), dumps(data)),
        )

    @staticmethod
    def _save_task(db: sqlite3.Connection, run_id: str, task: OrchTask) -> None:
        db.execute(
            "INSERT INTO orch_tasks VALUES(?,?,?,?) ON CONFLICT(run_id, id) DO UPDATE SET "
            "status=excluded.status, body=excluded.body",
            (run_id, task.id, task.status, dumps(asdict(task))),
        )

    def create_run(self, run: Run, tasks: list[OrchTask]) -> None:
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO orch_runs VALUES(?,?,?)",
                (run.id, run.created_at, dumps(asdict(run))),
            )
            self._event(db, run.id, "run.created", {"plan": run.plan_hash, "base": run.base})
            for task in tasks:
                self._save_task(db, run.id, task)
                self._event(db, run.id, "task.planned", {"depends_on": task.depends_on}, task.id)

    @staticmethod
    def _save_run(db: sqlite3.Connection, run: Run) -> None:
        db.execute("UPDATE orch_runs SET body=? WHERE id=?", (dumps(asdict(run)), run.id))

    def update_run(
        self, run_id: str, kind: str, data: dict[str, object], **changes: object
    ) -> Run:
        with self.store.transaction() as db:
            run = replace(self._run(db, run_id), **changes)  # type: ignore[arg-type]
            self._save_run(db, run)
            self._event(db, run_id, kind, data)
            return run

    def transition(
        self,
        run_id: str,
        task_id: str,
        status: str,
        kind: str,
        data: dict[str, object],
        attempt: int = 0,
        verified_head: str = "",
        **changes: object,
    ) -> OrchTask:
        """Apply a legal state change and its event atomically.

        `verified_head` moves the run's verified integration HEAD in the same
        transaction, so a crash can never separate "merged" from "recorded".
        """
        with self.store.transaction() as db:
            task = self._task(db, run_id, task_id)
            if status != task.status and status not in TRANSITIONS[task.status]:
                raise Conflict(f"{task_id}: {task.status} -> {status} is not a legal transition")
            task = replace(task, status=status, **changes)  # type: ignore[arg-type]
            self._save_task(db, run_id, task)
            self._event(db, run_id, kind, {"status": status, **data}, task_id, attempt)
            if verified_head:
                self._save_run(db, replace(self._run(db, run_id), verified_head=verified_head))
            return task

    def record(
        self, run_id: str, kind: str, data: dict[str, object], task_id: str = "", attempt: int = 0
    ) -> None:
        with self.store.transaction() as db:
            self._run(db, run_id)
            self._event(db, run_id, kind, data, task_id, attempt)

    def revise_tests(
        self, run_id: str, task_id: str, plan: dict[str, object], head: str, *,
        expected_plan: str, attempt: int, candidate: str, level: int, expected_review: str = "",
        expected_entry: dict[str, object] | None = None,
    ) -> None:
        """Publish a test revision and invalidate its old verification atomically."""
        with self.store.transaction() as db:
            run, task = self._run(db, run_id), self._task(db, run_id, task_id)
            if (run.plan_hash != expected_plan or task.attempts != attempt
                    or task.pending_review != expected_review
                    or expected_entry is not None and task.entry() != expected_entry
                    or task.status not in IN_FLIGHT or level not in {0, 3}
                    or (not candidate and level != 0)):
                raise Conflict("Test revision no longer matches its active attempt")
            self._save_run(db, replace(run, plan=plan, plan_hash=plan_hash(plan),
                                       verified_head=head))
            self._save_task(db, run_id, replace(
                task, status="READY", level=level, used_levels=[], disputes=task.disputes + 1,
                claude_attempts=task.claude_attempts + 1, last_signature="", last_diff_hash="",
                last_error="", last_diff="", pending_candidate=candidate, failed_candidate="",
                pending_review="",
            ))
            self._event(db, run_id, "plan.tests_revised", {
                "task": task_id, "commit": head, "previous_plan": expected_plan,
            })
            self._event(db, run_id, "task.tests_fixed", {
                "status": "READY", "commit": head, "candidate": candidate,
            }, task_id, attempt)

    def refresh_inputs(
        self, run_id: str, task_id: str, attempt: int, candidate: str, revision: str,
        changed: list[str], diff: str,
    ) -> None:
        """Invalidate only the stale consumer, preserving the immutable artifact as evidence."""
        with self.store.transaction() as db:
            task = self._task(db, run_id, task_id)
            if (task.status not in IN_FLIGHT or task.attempts != attempt
                    or not candidate or task.pending_candidate != candidate):
                raise Conflict("Contract refresh no longer matches its active candidate")
            if revision in task.input_revisions or len(task.input_revisions) >= 3:
                raise Conflict("Unstable dependency contracts; automatic refresh limit reached")
            updated = replace(
                task, status="READY", pending_candidate="", failed_candidate="",
                input_revisions=[*task.input_revisions, revision],
                level=3 if task.level == 3 else 0, used_levels=[],
                last_signature="", last_diff_hash="", last_diff=diff[-4000:],
                last_error=f"Dependency contracts changed: {', '.join(changed)}. "
                           "Repair this task against the current dependency contracts; "
                           "the prior candidate diff is evidence, not applied code.",
            )
            self._save_task(db, run_id, updated)
            self._event(db, run_id, "task.inputs_changed", {
                "status": "READY", "candidate": candidate, "revision": revision,
                "dependencies": changed, "refreshes": len(updated.input_revisions),
            }, task_id, attempt)

    def request_context(
        self, run_id: str, task_id: str, selectors: list[str], revision: str, error: str = "",
        *, call_id: str = "", observed_requests: int | None = None,
        partial_files: tuple[str, ...] = (),
    ) -> OrchTask:
        """Bound context turns across retries, parallel samples and process restarts."""
        with self.store.transaction() as db:
            task = self._task(db, run_id, task_id)
            wanted = list(dict.fromkeys([*task.context_selectors, *selectors]))
            progress = any(item not in task.context_selectors
                           and (item.split("::", 1)[0] not in task.context_selectors
                                or item.split("::", 1)[0] in partial_files)
                           for item in selectors)
            if selectors and not progress:
                if observed_requests is not None and observed_requests < task.context_requests:
                    self._event(db, run_id, "context.shared", {
                        "selectors": selectors, "revision": revision, "call_id": call_id,
                        "requests": task.context_requests,
                    }, task_id, task.attempts)
                    return task  # another sample supplied context absent from this call's snapshot
                raise Conflict(f"{task_id}: context request made no progress (already supplied)")
            if task.context_requests >= 3:
                raise Conflict(f"{task_id}: context request limit (3) exhausted")
            if len(wanted) > 16:
                raise Conflict(f"{task_id}: context selector limit (16) exceeded")
            task = replace(task, context_requests=task.context_requests + 1,
                           context_selectors=wanted)
            self._save_task(db, run_id, task)
            self._event(db, run_id, "context.request", {
                "selectors": selectors, "revision": revision, "error": error,
                "requests": task.context_requests, "call_id": call_id,
                "partial_files": list(partial_files),
            }, task_id, task.attempts)
            return task

    def wait_for(
        self, run_id: str, task_id: str, number: int, owners: list[str], reason: str,
        *, validate: Callable[[dict[str, list[str]]], list[str]],
    ) -> None:
        """Commit the candidate handoff and its DAG edge atomically, including cycle checks."""
        with self.store.transaction() as db:
            task = self._task(db, run_id, task_id)
            if (task.status == "PLANNED" and task.attempts == number - 1
                    and owners and set(owners) <= set(task.depends_on)):
                return  # duplicate delivery of an already committed handoff
            if task.status != "VERIFYING" or task.attempts != number:
                raise Conflict(f"{task_id}: stale dependency handoff")
            graph = {t.id: t.depends_on for t in self._tasks(db, run_id)
                     if t.status != "REPLANNED"}
            graph[task_id] = sorted({*task.depends_on, *owners})
            errors = validate(graph)
            if errors:
                raise Conflict("dependency handoff rejected: " + "; ".join(errors[:3]))
            if not owners or (graph[task_id] == sorted(task.depends_on)
                              and not (task.interface_depends_on and task.pending_candidate)):
                raise Conflict(f"{task_id}: dependency handoff made no progress")
            self._save_task(db, run_id, replace(
                task, status="PLANNED", attempts=number - 1, depends_on=graph[task_id],
            ))
            self._event(db, run_id, "task.waits", {
                "status": "PLANNED", "reason": reason, "depends_on": graph[task_id],
                "candidate": task.pending_candidate,
            }, task_id, number)

    def replan(
        self,
        run_id: str,
        failing: str,
        chosen: list[str],
        replacements: list[OrchTask],
        summary: str,
        *,
        validate: Callable[[dict[str, object]], list[str]],
    ) -> Run:
        """Hand a subtree over to replacement tasks, or change nothing (Conflict).

        The revised plan is rebuilt from the run and tasks as they are now, so a test
        correction committed during the replan call is never overwritten.
        """
        with self.store.transaction() as db:
            current = self._run(db, run_id)
            tasks = self._tasks(db, run_id)
            ids = [task.id for task in replacements]
            if len(ids) != len(set(ids)) or set(ids) & {task.id for task in tasks}:
                raise Conflict("replacement task ids must be new and unique")
            wanted = {test for task in tasks if task.id in chosen for test in task.tests}
            given = {test for task in replacements for test in task.tests}
            if wanted != given:
                raise Conflict("replacement tests differ from the replaced tasks")
            frontier = replacement_frontier(
                {path for task in tasks if task.id in chosen for path in task.files},
                {task.id: task.depends_on for task in replacements},
                {task.id: task.files for task in replacements},
            )
            if frontier:
                raise Conflict("; ".join(frontier[:3]))
            kept = [
                task.entry()
                for task in tasks
                if task.status != "REPLANNED" and task.id not in chosen
            ]
            plan = {**current.plan, "tasks": [*kept, *[task.entry() for task in replacements]]}
            errors = validate(plan)
            if errors:
                raise Conflict(f"revised plan is invalid: {'; '.join(errors[:3])}")
            run = replace(current, plan=plan, plan_hash=plan_hash(plan))
            for task_id in chosen:
                task = self._task(db, run.id, task_id)
                expected = "RUNNING" if task_id == failing else "PLANNED"
                if task.status != expected:
                    raise Conflict(f"{task_id} became {task.status} during the replan")
                moved = [new.id for new in replacements]
                self._save_task(db, run.id, replace(task, status="REPLANNED", reason=f"-> {moved}"))
                self._event(db, run.id, "task.replanned", {"into": moved}, task_id)
            for task in replacements:
                self._save_task(db, run.id, task)
                self._event(db, run.id, "task.planned", {"depends_on": task.depends_on}, task.id)
            self._save_run(db, run)
            self._event(db, run.id, "plan.revised", {"plan": run.plan_hash, "why": summary})
            return run

    def reopen(
        self,
        run_id: str,
        culprit: str,
        dependents: list[str],
        head: str,
        evidence: dict[str, object],
        **changes: object,
    ) -> None:
        """Final-gate regression: reverted tasks reopen with the new HEAD, atomically."""
        with self.store.transaction() as db:
            run = self._run(db, run_id)
            for task_id in [culprit, *dependents]:
                task = self._task(db, run_id, task_id)
                if task.status != "DONE":
                    raise Conflict(f"{task_id} is {task.status}, not DONE")
                if task_id == culprit:
                    task = replace(
                        task, status="READY", commit="", **changes  # type: ignore[arg-type]
                    )
                    kind = "task.regression"
                else:
                    task = replace(task, status="PLANNED", commit="")
                    kind = "task.reopened"
                self._save_task(db, run_id, task)
                self._event(db, run_id, kind, {"status": task.status, **evidence}, task_id)
            self._save_run(
                db, replace(run, verified_head=head, regressions=run.regressions + 1)
            )
            reopened = [culprit, *dependents]
            self._event(db, run_id, "run.regression", {"culprit": culprit, "reopened": reopened})

    def outcomes(self, route: str, level: int) -> tuple[int, int]:
        """(passed, total) attempts at one ladder rung across every run in this repo."""
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT data FROM orch_events WHERE kind='attempt.outcome'"
            ).fetchall()
        found = [json.loads(row["data"]) for row in rows]
        mine = [item for item in found if item["route"] == route and item["level"] == level]
        return sum(1 for item in mine if item["passed"]), len(mine)

    def recover(self, run_id: str) -> list[str]:
        """Recover durable artifacts; never silently replay an unacknowledged model call."""
        with self.store.transaction() as db:
            receipts = self.usage.read(db, run_id)
            interrupted = [
                task for task in self._tasks(db, run_id) if task.status in IN_FLIGHT
            ]
            for task in interrupted:
                unknown = [call.id for call in receipts if call.task_id == task.id
                           and call.attempt == task.attempts
                           and unresolved(call.outcome, call.usage)]
                blocked = bool(unknown) and (not task.pending_candidate or any(
                    call.id in unknown and call.sample == task.pending_review
                    for call in receipts
                ))
                status = "PARKED" if blocked else "READY"
                reason = "unresolved provider calls; inspect `orch reconcile`" if blocked else ""
                self._save_task(db, run_id, replace(task, status=status, reason=reason))
                self._event(
                    db, run_id, "task.interrupted", {
                        "from": task.status, "status": status, "unresolved_calls": unknown,
                        "candidate": task.pending_candidate,
                    }, task.id, task.attempts
                )
            return [task.id for task in interrupted]

    # --- builder rules (shared by every run in this repository) ----------
    def retry(self, run_id: str, task_id: str, *, ack_unknown: bool = False,
              discard_candidate: bool = False) -> OrchTask:
        """Explicit retry is a single state decision, not a read-then-write race."""
        with self.store.transaction() as db:
            task = self._task(db, run_id, task_id)
            if task.status != "PARKED":
                raise Conflict(f"{task_id} is not PARKED; only parked tasks can be retried")
            calls = self.usage.read(db, run_id)
            unknown = [call.id for call in calls if call.task_id == task_id
                       and unresolved(call.outcome, call.usage)]
            if unknown and not ack_unknown:
                raise Conflict("Unresolved calls may still be billed; inspect `orch reconcile`, "
                               "then use --ack-unknown to authorize a possible repeat")
            pending = "" if discard_candidate else task.pending_candidate
            task = replace(task, status="READY", reason="", pending_candidate=pending,
                           failed_candidate="" if discard_candidate else task.failed_candidate,
                           level=task.level if pending else 0,
                           used_levels=task.used_levels if pending else [], rebases=0)
            self._save_task(db, run_id, task)
            self._event(db, run_id, "task.retry", {
                "status": "READY", "acknowledged_calls": unknown,
                "discarded_candidate": discard_candidate,
            }, task_id)
            return task

    def hint(self, run_id: str, task_id: str, text: str) -> OrchTask:
        """Add a person's guidance to a task; a parked task goes back to READY, fresh ladder."""
        text = " ".join(text.split())
        if not text:
            raise Conflict("a hint needs text")
        with self.store.transaction() as db:
            task = self._task(db, run_id, task_id)
            if task.status in {"DONE", "REPLANNED"}:
                raise Conflict(f"{task_id} is {task.status}; a hint can no longer change it")
            hints = f"{task.human_hint}\n- {text}" if task.human_hint else f"- {text}"
            task = replace(task, human_hint=hints)
            if task.status == "PARKED":
                task = replace(task, status="READY", reason="", level=0, used_levels=[],
                               rebases=0)
            self._save_task(db, run_id, task)
            self._event(db, run_id, "task.hint", {"hint": text, "status": task.status},
                        task_id)
            return task

    def add_rule(self, text: str, source: str, kind: str = "builder") -> Rule | None:
        """A new candidate, or None when the same lesson is already known for that kind."""
        with self.store.transaction() as db:
            rows = db.execute("SELECT body FROM orch_rules").fetchall()
            known = [Rule(**json.loads(row["body"])) for row in rows]
            same = [rule for rule in known if rule.kind == kind]
            if any(rule.text.casefold() == text.casefold() for rule in same):
                return None
            rule = Rule(f"{KINDS[kind]}{len(same) + 1}", text, source, self.clock(), kind=kind)
            db.execute("INSERT INTO orch_rules VALUES(?,?)", (rule.id, dumps(asdict(rule))))
            return rule

    def rules(self, kind: str = "") -> list[Rule]:
        """Every rule, or one kind's; builder rules first, then by number."""
        with self.store.transaction() as db:
            rows = db.execute("SELECT body FROM orch_rules").fetchall()
        found = [Rule(**json.loads(row["body"])) for row in rows]
        return sorted(
            (rule for rule in found if not kind or rule.kind == kind),
            key=lambda rule: (rule.kind != "builder", int(rule.id[1:])),
        )

    def save_rule(self, rule: Rule) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE orch_rules SET body=? WHERE id=?", (dumps(asdict(rule)), rule.id))

    @staticmethod
    def _count(
        db: sqlite3.Connection, rule_id: str, shown: bool, passed: bool, step: int,
        proxy: bool = False,
    ) -> bool:
        row = db.execute("SELECT body FROM orch_rules WHERE id=?", (rule_id,)).fetchone()
        if row is None:
            return False
        rule = Rule(**json.loads(row["body"]))
        prefix = ("proxy_" if proxy else "") + ("with" if shown else "without")
        for suffix, amount in (("total", step), ("passed", step * int(passed))):
            name = f"{prefix}_{suffix}"
            setattr(rule, name, getattr(rule, name) + amount)
        db.execute("UPDATE orch_rules SET body=? WHERE id=?", (dumps(asdict(rule)), rule_id))
        return True

    def plan_outcome(
        self, run_id: str, rule_id: str, shown: bool, passed: bool, kind: str,
        data: dict[str, object],
    ) -> None:
        """Count a planner A/B observation and log it on the run: `kind` is proxy (a new
        plan), replay (an offline re-plan of a past spec) or outcome (the real result)."""
        with self.store.transaction() as db:
            if rule_id:
                self._count(db, rule_id, shown, passed, 1, proxy=kind != "outcome")
            self._event(db, run_id, f"plan.{kind}",
                        {**data, "rule": rule_id, "shown": shown, "passed": passed})

    def planning_history(self) -> list[tuple[str, float, float]]:
        """(run, proxy score, real blocked rate) for runs that have both."""
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT run_id, kind, data FROM orch_events "
                "WHERE kind IN ('plan.proxy', 'plan.outcome') ORDER BY seq"
            ).fetchall()
        proxy: dict[str, float] = {}
        real: dict[str, float] = {}
        for row in rows:
            data = json.loads(row["data"])
            if row["kind"] == "plan.proxy":
                proxy.setdefault(row["run_id"], float(data["score"]))
            else:
                real.setdefault(row["run_id"], float(data["blocked_rate"]))
        return [(run, proxy[run], real[run]) for run in proxy if run in real]

    def rule_outcome(
        self, rule_id: str, shown: bool, passed: bool, run_id: str, task_id: str, attempt: int
    ) -> None:
        """Count one A/B observation and log it on the attempt, atomically."""
        with self.store.transaction() as db:
            if self._count(db, rule_id, shown, passed, 1):
                self._event(db, run_id, "rule.trial",
                            {"rule": rule_id, "shown": shown, "passed": passed}, task_id, attempt)

    def discount(self, run_id: str, task_id: str, why: str) -> list[str]:
        """Withdraw a task's A/B observations (its tests proved too weak to be evidence).

        Each observation is withdrawn at most once; returns the rules touched.
        """
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT seq, kind, data FROM orch_events WHERE run_id=? AND task_id=? "
                "AND kind IN ('rule.trial', 'rule.discounted') ORDER BY seq",
                (run_id, task_id),
            ).fetchall()
            done = {seq for row in rows if row["kind"] == "rule.discounted"
                    for seq in json.loads(row["data"])["trials"]}
            fresh = [(row["seq"], json.loads(row["data"])) for row in rows
                     if row["kind"] == "rule.trial" and row["seq"] not in done]
            for _, data in fresh:
                self._count(db, data["rule"], data["shown"], data["passed"], -1)
            if fresh:
                self._event(db, run_id, "rule.discounted",
                            {"trials": [seq for seq, _ in fresh], "why": why}, task_id)
            return sorted({data["rule"] for _, data in fresh})

    # --- reads ----------------------------------------------------------
    @staticmethod
    def _run(db: sqlite3.Connection, run_id: str) -> Run:
        row = db.execute("SELECT body FROM orch_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown run {run_id}")
        return Run(**json.loads(row["body"]))

    @staticmethod
    def _task(db: sqlite3.Connection, run_id: str, task_id: str) -> OrchTask:
        row = db.execute(
            "SELECT body FROM orch_tasks WHERE run_id=? AND id=?", (run_id, task_id)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown task {task_id}")
        return OrchTask(**json.loads(row["body"]))

    @staticmethod
    def _tasks(db: sqlite3.Connection, run_id: str) -> list[OrchTask]:
        return [
            OrchTask(**json.loads(row["body"]))
            for row in db.execute(
                "SELECT body FROM orch_tasks WHERE run_id=? ORDER BY id", (run_id,)
            )
        ]

    def run(self, run_id: str) -> Run:
        with self.store.transaction() as db:
            return self._run(db, run_id)

    def spent(self, run_id: str) -> int:
        """Cents the run's model calls (builder, Claude, Jev) have settled so far."""
        return sum(cents for call in self.usage_calls(run_id)
                   if type(cents := call.usage.get("settled_cents", 0)) is int)

    def usage_calls(self, run_id: str) -> list[Call]:
        """New receipts plus historical events, never counting a linked result twice."""
        calls = self.usage.calls(run_id)
        ids = {call.id for call in calls}
        started: dict[tuple[str, object], OrchEvent] = {}
        for event in self.events(run_id):
            key = (event.span, event.data.get("sample"))
            if event.kind == "attempt.called":
                started[key] = event
            if event.kind not in {"attempt.result", "jev.call"}:
                continue
            if event.data.get("call_id") in ids:
                continue
            raw = event.data.get("usage")
            route = str(event.data.get("route", "unknown/unknown"))
            provider, _, model = route.partition("/")
            if route == "vercel/typesafe-ai/jev":
                provider, model = "jev", "typesafe-ai/jev"
            before = started.get(key)
            meta = before.data if before else {}
            rules = meta.get("rules", [])
            suffix = event.span.rsplit("/a", 1)[-1]
            calls.append(Call(
                id=f"legacy-{event.seq}", run_id=run_id, provider=provider, model=model,
                purpose=str(event.data.get("purpose", meta.get("purpose", "legacy"))),
                started_at=before.at if before else event.at, finished_at=event.at,
                task_id=event.task_id, sample=str(event.data.get("sample", "")),
                attempt=int(suffix) if suffix.isdigit() else 0,
                outcome=str(event.data.get("outcome", "unknown")),
                prefix_hash=str(meta.get("prefix", "")),
                rules=[str(r) for r in rules] if isinstance(rules, list) else [],
                usage=usage_fields(raw) if isinstance(raw, dict) else {}, legacy=True,
            ))
        return sorted(calls, key=lambda call: call.started_at)

    def runs(self) -> list[Run]:
        """Every run, newest first."""
        with self.store.transaction() as db:
            rows = db.execute("SELECT body FROM orch_runs ORDER BY created_at DESC, id DESC")
            return [Run(**json.loads(row["body"])) for row in rows.fetchall()]

    def latest(self) -> Run:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT id FROM orch_runs ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise KeyError("No orchestration run yet; start with `orch plan`")
            return self._run(db, row["id"])

    def task(self, run_id: str, task_id: str) -> OrchTask:
        with self.store.transaction() as db:
            return self._task(db, run_id, task_id)

    def tasks(self, run_id: str) -> list[OrchTask]:
        with self.store.transaction() as db:
            return self._tasks(db, run_id)

    def events(self, run_id: str, task_id: str = "") -> list[OrchEvent]:
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM orch_events WHERE run_id=? AND (?='' OR task_id=?) ORDER BY seq",
                (run_id, task_id, task_id),
            ).fetchall()
        return [
            OrchEvent(
                row["seq"], row["run_id"], row["task_id"], row["span"], row["parent"],
                row["kind"], row["at"], json.loads(row["data"]),
            )
            for row in rows
        ]
