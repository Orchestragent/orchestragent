"""Versioned decision policies: immutable snapshots, held-out gates and rollback.

This is system-policy learning, not model-weight training. Local reports are audit
evidence, not cryptographic attestations from an independent evaluator.
"""

import json
import math
from dataclasses import asdict, dataclass, replace
from typing import Any, cast

from orchestragent.envelope import digest
from orchestragent.orch_rules import KINDS, Rule, layer
from orchestragent.orch_state import OrchState
from orchestragent.orch_usage import unresolved
from orchestragent.serialization import dumps, normalize
from orchestragent.store import Conflict

MIN_PAIRS = 12
MIN_CASES = 6


@dataclass(frozen=True)
class Tuning:
    jobs: int = 4
    test_jobs: int = 4
    verify_jobs: int = 0
    cache_mode: str = "auto"

    def __post_init__(self) -> None:
        if (any(type(value) is not int or not low <= value <= high for value, low, high in
                ((self.jobs, 1, 64), (self.test_jobs, 1, 16), (self.verify_jobs, 0, 64)))
                or not isinstance(self.cache_mode, str) or self.cache_mode not in {"auto", "off"}):
            raise ValueError("Invalid scheduling tuning")


@dataclass(frozen=True)
class Policy:
    id: str
    parent: str
    created_at: float
    rules: tuple[Rule, ...]
    training_specs: tuple[str, ...]
    tuning: Tuning | None = None

    @classmethod
    def decode(cls, value: dict[str, Any]) -> "Policy":
        value = cast(dict[str, Any], normalize(value))
        policy = cls(**{**value, "rules": tuple(Rule(**row) for row in value["rules"]),
                        "training_specs": tuple(value["training_specs"]),
                        "tuning": (Tuning(**value["tuning"])
                                   if value.get("tuning") is not None else None)})
        body = {key: val for key, val in asdict(policy).items() if key != "id"}
        identities = {digest(dumps(body))}
        if policy.tuning is None:
            body.pop("tuning")  # preserve identities of existing text-only snapshots
            identities.add(digest(dumps(body)))
        if policy.id not in identities:
            raise ValueError("Policy fingerprint mismatch")
        if (len({rule.id for rule in policy.rules}) != len(policy.rules)
                or any(rule.kind not in KINDS or rule.status != "active"
                       or not rule.text.strip() or len(rule.text) > 240 for rule in policy.rules)
                or any(layer(list(policy.rules), kind)[2] for kind in KINDS)):
            raise ValueError("Invalid or oversized policy rules")
        return policy


@dataclass(frozen=True)
class Evidence:
    pairs: int
    cases: int
    baseline_passed: int
    candidate_passed: int
    baseline_tokens: int
    candidate_tokens: int
    baseline_api_cents: int
    candidate_api_cents: int
    baseline_hash: str
    candidate_hash: str
    baseline_seconds: float | None = None
    candidate_seconds: float | None = None


def evaluate(policy: Policy, baseline: dict[str, Any], candidate: dict[str, Any]) -> Evidence:
    """Fail closed on incomplete, synthetic, contaminated or incomparable evidence.

The initial gate is deliberately a pilot rule, not a statistical significance claim:
    >=12 pairs from >=6 distinct specs, no per-case acceptance regression, <=10% more
    aggregate tokens, and either more passing cases or >=5% fewer tokens. Scheduling
    changes require complete acceptance on both arms and <=10% more elapsed time;
    >=10% less elapsed time also qualifies as a benefit for these frozen parameters.
    """
    for report, expected in ((baseline, policy.parent), (candidate, policy.id)):
        if (report.get("mode") != "real" or report.get("synthetic") is not False
                or report.get("case_kind") != "curated" or report.get("policy_id") != expected
                or report.get("success_rate") is None
                or report.get("completed_trials") != report.get("expected_trials")):
            raise ValueError("Need complete real curated reports for both frozen policies")
        cases = report["suite_snapshot"]["cases"]
        if report["suite_hash"] != digest(dumps(report["suite_snapshot"])):
            raise ValueError("Suite fingerprint mismatch")
        if any(digest(case["spec"]) in policy.training_specs for case in cases):
            raise ValueError("Held-out spec overlaps recorded training history")
        totals = report["totals"]
        if (totals["pending_calls"] or totals["legacy_calls"]
                or totals["reported_calls"]["total_tokens"] != totals["calls"]
                or totals["known_tokens"]["total_tokens"] <= 0):
            raise ValueError("Complete attributable token receipts are required")
        accounting = report.get("api_accounting")
        if (not isinstance(accounting, dict) or accounting.get("currency") != "CNY"
                or any(type(accounting.get(key)) is not int or accounting[key] < 0 for key in
                       ("settled_cents", "unsettled_calls", "known_reserved_cents",
                        "missing_reservation_calls"))
                or any(accounting[key] for key in
                       ("unsettled_calls", "known_reserved_cents", "missing_reservation_calls"))):
            raise ValueError("Complete settled API accounting is required")
        for trial in report["trials"]:
            if trial["status"] not in {"passed", "failed", "plan_rejected"}:
                raise ValueError("Interrupted or infrastructure-error trial is not evidence")
            if (trial["status"] == "passed") != (trial["acceptance_passed"] is True):
                raise ValueError("Success must come from independent acceptance")
    if (baseline["suite_hash"] != candidate["suite_hash"]
            or baseline["config"] != candidate["config"]):
        raise ValueError("Paired reports must use the same suite, engine and execution config")
    before = {trial["id"]: trial for trial in baseline["trials"]}
    after = {trial["id"]: trial for trial in candidate["trials"]}
    distinct = {digest(case["spec"]) for case in candidate["suite_snapshot"]["cases"]}
    if (before.keys() != after.keys() or len(before) != baseline["expected_trials"]
            or len(after) != candidate["expected_trials"]
            or len(before) < MIN_PAIRS or len(distinct) < MIN_CASES):
        raise ValueError("Need at least 12 unique paired trials across 6 distinct held-out specs")
    for key, old in before.items():
        new = after[key]
        if any(old[field] != new[field] for field in
               ("case_id", "input_hash", "acceptance_hash")):
            raise ValueError("Paired trial inputs or acceptance changed")
        if old["status"] == "passed" and new["status"] != "passed":
            raise ValueError("Candidate regressed a previously passing held-out trial")
    old_passed = sum(row["status"] == "passed" for row in before.values())
    new_passed = sum(row["status"] == "passed" for row in after.values())
    old_tokens = baseline["totals"]["known_tokens"]["total_tokens"]
    new_tokens = candidate["totals"]["known_tokens"]["total_tokens"]
    old_cents = baseline["api_accounting"]["settled_cents"]
    new_cents = candidate["api_accounting"]["settled_cents"]
    old_seconds = new_seconds = None
    if policy.tuning is not None:
        for report, expected in ((baseline, policy.parent), (candidate, policy.id)):
            snapshot = report.get("policy_snapshot")
            if not isinstance(snapshot, dict) or Policy.decode(snapshot).id != expected:
                raise ValueError("Scheduling evidence needs the exact policy snapshot")
            frozen = Policy.decode(snapshot)
            settings = frozen.tuning
            for row in report["trials"]:
                config = row.get("execution_config", {})
                if (config.get("policy_id") != expected
                        or config.get("jobs") != (settings.jobs if settings else
                                                  report["config"]["jobs"])
                        or config.get("cache_mode") != (settings.cache_mode if settings else "auto")
                        or settings and settings.verify_jobs
                        and config.get("verify_jobs") != settings.verify_jobs):
                    raise ValueError("Scheduling trial did not execute the frozen settings")
            if any(type(row.get("elapsed_seconds")) not in {int, float}
                   or not math.isfinite(row["elapsed_seconds"]) or row["elapsed_seconds"] <= 0
                   for row in report["trials"]):
                raise ValueError("Scheduling evidence needs complete elapsed time")
        if new_passed != len(after) or old_passed != len(before):
            raise ValueError("Both scheduling policies must pass every independent acceptance")
        old_seconds = sum(row["elapsed_seconds"] for row in baseline["trials"])
        new_seconds = sum(row["elapsed_seconds"] for row in candidate["trials"])
        if new_seconds > old_seconds * 1.10:
            raise ValueError("Scheduling candidate exceeds the elapsed-time regression cap")
    if new_cents > max(old_cents * 1.10, old_cents + 1):
        raise ValueError("Candidate API accounting exceeds the cost-regression cap")
    if (new_tokens > old_tokens * 1.10
            or not (new_passed > old_passed or new_tokens <= old_tokens * 0.95
                    or (new_seconds is not None and old_seconds is not None
                        and new_seconds <= old_seconds * 0.90))):
        raise ValueError("Candidate lacks acceptance, token or measured time gain within token cap")
    return Evidence(len(before), len(distinct), old_passed, new_passed, old_tokens, new_tokens,
                    old_cents, new_cents,
                    digest(dumps(baseline)), digest(dumps(candidate)), old_seconds, new_seconds)


class PolicyStore:
    def __init__(self, state: OrchState) -> None:
        self.state = state
        with state.store.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS orch_policies "
                       "(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS orch_policy_head "
                       "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS orch_policy_events "
                       "(seq INTEGER PRIMARY KEY, at REAL NOT NULL, id TEXT NOT NULL, "
                       "action TEXT NOT NULL, evidence TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS orch_policy_training "
                       "(spec_hash TEXT PRIMARY KEY)")

    def observe_spec(self, spec: str) -> None:
        """Record exposure before planning, including drafts that never create a run."""
        with self.state.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO orch_policy_training VALUES (?)", (digest(spec),))

    def get(self, policy_id: str) -> Policy:
        with self.state.store.transaction() as db:
            row = db.execute("SELECT body FROM orch_policies WHERE id=?", (policy_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown policy {policy_id}")
        return Policy.decode(json.loads(row[0]))

    def current(self) -> Policy | None:
        with self.state.store.transaction() as db:
            row = db.execute("SELECT id FROM orch_policy_head WHERE singleton=1").fetchone()
        return self.get(row[0]) if row else None

    def save(self, policy: Policy) -> None:
        Policy.decode(asdict(policy))
        with self.state.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO orch_policies VALUES (?,?)",
                       (policy.id, dumps(asdict(policy))))

    def snapshot(self, include: tuple[str, ...] = (), exclude: tuple[str, ...] = (),
                 *, initial: bool = False, tuning: Tuning | None = None,
                 expected_parent: str | None = None) -> Policy:
        parent = self.current()
        if expected_parent is not None and (parent is None or parent.id != expected_parent):
            raise Conflict("Active policy changed while preparing the candidate")
        if parent is None and not initial:
            raise ValueError("Enable a baseline policy first")
        known = {rule.id: rule for rule in self.state.rules()}
        if (set(include) & set(exclude) or not set(include) <= known.keys()
                or any(known[key].status == "retired" for key in include)):
            raise ValueError("Candidate must name known non-retired rules")
        selected = {rule.id: rule for rule in (parent.rules if parent else known.values())
                    if rule.status == "active"}
        if not set(exclude) <= selected.keys():
            raise ValueError("Cannot exclude a rule absent from the parent policy")
        selected.update({key: replace(known[key], status="active") for key in include})
        for key in exclude:
            selected.pop(key)
        with self.state.store.transaction() as db:
            observed = {row[0] for row in db.execute("SELECT spec_hash FROM orch_policy_training")}
        body = {"parent": parent.id if parent else "", "created_at": self.state.clock(),
                "rules": tuple(asdict(selected[key]) for key in sorted(selected)),
                "tuning": asdict(tuning) if tuning is not None else (
                    asdict(parent.tuning) if parent and parent.tuning else None),
                "training_specs": tuple(sorted({digest(run.spec) for run in self.state.runs()}
                                              | observed
                                              | set(parent.training_specs if parent else ())))}
        policy = Policy.decode({"id": digest(dumps(body)), **body})
        self.save(policy)
        return policy

    def enable(self, *, tuning: Tuning | None = None) -> Policy:
        current = self.current()
        if current is not None:
            if tuning is not None and tuning != current.tuning:
                raise ValueError("An active policy already exists; stage a candidate snapshot")
            return current
        baseline = self.snapshot(initial=True, tuning=tuning)
        with self.state.store.transaction() as db:
            if db.execute("SELECT 1 FROM orch_policy_head").fetchone():
                raise Conflict("Another policy was enabled concurrently")
            db.execute("INSERT INTO orch_policy_head VALUES (1,?)", (baseline.id,))
            db.execute("INSERT INTO orch_policy_events(at,id,action,evidence) VALUES(?,?,?,?)",
                       (self.state.clock(), baseline.id, "enabled", "{}"))
        return baseline

    def suggest(self, run_id: str) -> Policy:
        """Stage one observed scheduling hypothesis; never activate it from proxy metrics."""
        parent = self.current()
        run = self.state.run(run_id)
        if parent is None or run.policy_id != parent.id or run.status not in {"done", "parked"}:
            raise ValueError("Need a finished run of the current frozen policy")
        calls = self.state.usage.calls(run_id)
        if not calls or any(unresolved(call.outcome, call.usage) for call in calls):
            raise ValueError("Unresolved or absent receipts cannot support a tuning suggestion")
        events = self.state.events(run_id)
        starts = [event for event in events if event.kind == "run.started"]
        measured = [event for event in events if event.kind == "run.metrics"]
        tuning = parent.tuning or Tuning()
        if len(starts) != 1 or len(measured) != 1 or starts[0].data.get("jobs") != tuning.jobs:
            raise ValueError("Need one uninterrupted run at the parent generation concurrency")
        limited = sum(call.provider == "dsh" and call.scope == "execution"
                      and call.outcome == "rate_limited" for call in calls)
        metrics = measured[0].data
        if limited >= 2 and tuning.jobs > 1:
            proposed = replace(tuning, jobs=max(1, tuning.jobs // 2))
            reason = "repeated observed DSF rate limits"
        elif (not limited and type(metrics.get("ready_backlog_peak")) is int
              and cast(int, metrics["ready_backlog_peak"]) > 0
              and metrics.get("peak_generating") == tuning.jobs and tuning.jobs < 64):
            proposed = replace(tuning, jobs=min(64, tuning.jobs * 2))
            reason = "ready work queued while generation concurrency was full"
        else:
            raise ValueError("No supported one-parameter scheduling hypothesis in this run")
        candidate = self.snapshot(tuning=proposed, expected_parent=parent.id)
        with self.state.store.transaction() as db:
            db.execute("INSERT INTO orch_policy_events(at,id,action,evidence) VALUES(?,?,?,?)",
                       (self.state.clock(), candidate.id, "suggested", dumps({
                           "source_run": run_id, "metrics_event": measured[0].seq,
                           "reason": reason, "call_ids": [call.id for call in calls],
                       })))
        return candidate

    def promote(self, policy_id: str, baseline: dict[str, Any],
                candidate: dict[str, Any]) -> Evidence:
        policy = self.get(policy_id)
        if not policy.parent:
            raise ValueError("Baseline is not a candidate")
        evidence = evaluate(policy, baseline, candidate)
        with self.state.store.transaction() as db:
            row = db.execute("SELECT id FROM orch_policy_head").fetchone()
            if row is None or row[0] != policy.parent:
                raise Conflict("Active policy changed; evaluate against its current version")
            for case in candidate["suite_snapshot"]["cases"]:
                db.execute("INSERT OR IGNORE INTO orch_policy_training VALUES (?)",
                           (digest(case["spec"]),))
            db.execute("UPDATE orch_policy_head SET id=? WHERE singleton=1", (policy.id,))
            db.execute("INSERT INTO orch_policy_events(at,id,action,evidence) VALUES(?,?,?,?)",
                       (self.state.clock(), policy.id, "promoted", dumps(asdict(evidence))))
        return evidence

    def rollback(self, policy_id: str) -> Policy:
        policy = self.get(policy_id)
        with self.state.store.transaction() as db:
            if db.execute("SELECT 1 FROM orch_policy_events WHERE id=? "
                          "AND action IN ('enabled','promoted')", (policy_id,)).fetchone() is None:
                raise ValueError("Rollback target was never an accepted active policy")
            db.execute("UPDATE orch_policy_head SET id=? WHERE singleton=1", (policy_id,))
            db.execute("INSERT INTO orch_policy_events(at,id,action,evidence) VALUES(?,?,?,?)",
                       (self.state.clock(), policy_id, "rolled_back", "{}"))
        return policy

    def history(self) -> list[dict[str, object]]:
        with self.state.store.transaction() as db:
            rows = db.execute(
                "SELECT seq,at,id,action,evidence FROM orch_policy_events ORDER BY seq")
            return [{**dict(row), "evidence": json.loads(row["evidence"])} for row in rows]
