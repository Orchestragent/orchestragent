"""Durable, content-free model-call receipts and protocol-aware token accounting.

Anthropic/DSH Messages input excludes cache reads/writes; Codex input
includes them. Reasoning is a breakdown of output, never added a second time.
Missing counters stay unknown. A pending receipt survives even if planning never
creates a run or the process dies while a request is in flight.
"""

import json
import math
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields

from orchestragent.access import REJECTED_STATUSES
from orchestragent.serialization import dumps
from orchestragent.store import Conflict, Store

COUNTERS = (
    "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens", "reasoning_tokens",
    "settled_cents", "reserved_cents", "forwarded", "rejected",
)
MONEY = ("cost_usd", "provider_estimate_usd")
# Request/response shape facts read by our own transports: enum labels and one positive
# integer only. Never bodies, headers, credentials or reasoning text.
WIRE_LABELS: dict[str, frozenset[str]] = {
    "wire_thinking": frozenset({"enabled", "disabled", "absent", "invalid"}),
    "wire_effort": frozenset({"low", "high", "max", "absent", "invalid"}),
    "stop_reason": frozenset({"end_turn", "stop_sequence", "tool_use", "max_tokens",
                              "refusal", "pause_turn", "other"}),
    "turn_end": frozenset({"completed", "max-tokens", "blocked", "aborted", "error", "other"}),
    "dsf_transport": frozenset({"cli", "cli-account", "api"}),
}
METRICS = ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
           "cache_write_tokens", "reasoning_tokens")


def usage_fields(raw: dict[str, object]) -> dict[str, object]:
    """Keep only non-secret, finite counters; never copy arbitrary provider data."""
    result: dict[str, object] = {}
    for key in COUNTERS:
        counter = count(raw, key)
        if counter is not None:
            result[key] = counter
    for key in MONEY:
        value = raw.get(key)
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0):
            result[key] = value
    if (value := count(raw, "wire_max_tokens")) is not None and value > 0:
        result["wire_max_tokens"] = value
    for key, allowed in WIRE_LABELS.items():
        label = raw.get(key)
        if isinstance(label, str) and label in allowed:
            result[key] = label
    code = raw.get("turn_error")  # a provider/SDK error code such as TIMEOUT, never its text
    if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", code, re.ASCII):
        result["turn_error"] = code
    for key in ("served_model", "model"):
        value = raw.get(key)
        if isinstance(value, str) and re.fullmatch(r"[\w./:-]{1,128}", value, re.ASCII):
            result[key] = value
    return result


def count(raw: dict[str, object], key: str) -> int | None:
    value = raw.get(key)
    return value if type(value) is int and value >= 0 else None


def unresolved(outcome: str, usage: dict[str, object]) -> bool:
    """No receipt proves completion or rejection: replay might repeat paid inference."""
    if (count(usage, "forwarded") == 0 or count(usage, "settled_cents") is not None
            or count(usage, "rejected") in REJECTED_STATUSES):
        return False
    return (outcome in {"pending", "timeout", "timed_out", "cancelled"}
            or outcome.startswith("exception:")
            or (count(usage, "reserved_cents") or 0) > 0
            and (count(usage, "forwarded") or 0) > 0)


def output_cut(usage: dict[str, object]) -> bool:
    """The provider itself reported that the answer stopped at the output limit."""
    return usage.get("stop_reason") == "max_tokens" or usage.get("turn_end") == "max-tokens"


def settled_json_failure(outcome: str, message: str, usage: dict[str, object]) -> bool:
    return (outcome == "failed" and not unresolved(outcome, usage)
            and message.startswith(("Invalid DSH JSON result", "Invalid DeepSeek JSON result",
                                    "Invalid JSON result")))


def transient(outcome: str, message: str, usage: dict[str, object]) -> bool:
    """A failed call worth repeating unchanged: a dropped stream, a 5xx, a crashed process.

    Answers that settled (invalid JSON, an output cut) and refusals (auth, quota, policy,
    rejected 4xx) would fail the same way again; rate limits pause the run instead. A
    provider timeout is excluded too: repeating it doubles the longest wait a run allows.
    """
    if outcome.startswith("exception:"):
        return True
    if (outcome != "failed" or output_cut(usage) or "rejected" in usage
            or settled_json_failure(outcome, message, usage)):
        return False
    return (bool(usage.get("turn_error"))
            or message.startswith(("DSH upstream HTTP 5", "upstream HTTP 5"))
            or message == "DSH process stopped without a valid result")


@dataclass
class Tokens:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    issues: list[str] = field(default_factory=list)


def tokens(provider: str, raw: dict[str, object]) -> Tokens:
    """Normalize verified transport conventions without inventing absent usage."""
    incoming, outgoing = count(raw, "input_tokens"), count(raw, "output_tokens")
    read = count(raw, "cache_read_input_tokens")
    if read is None:
        read = count(raw, "cached_input_tokens")
    write = count(raw, "cache_creation_input_tokens")
    reported_total = count(raw, "total_tokens")
    result = Tokens(output_tokens=outgoing, cache_read_tokens=read,
                    cache_write_tokens=write, reasoning_tokens=count(raw, "reasoning_tokens"))
    if provider in {"claude", "dsh"}:
        if incoming is not None and read is not None and write is not None:
            result.input_tokens = incoming + read + write
        elif reported_total is not None and outgoing is not None and reported_total >= outgoing:
            result.input_tokens = reported_total - outgoing
            lower_bound = (incoming or 0) + (read or 0) + (write or 0)
            if lower_bound > result.input_tokens:
                result.issues.append("reported total is smaller than known component counters")
    elif provider == "codex":
        result.input_tokens = incoming
    else:
        result.issues.append("unknown input accounting convention")
    if result.input_tokens is not None and outgoing is not None:
        result.total_tokens = result.input_tokens + outgoing
        if reported_total is not None and result.total_tokens != reported_total:
            result.issues.append("reported total disagrees with component counters")
            result.input_tokens = result.total_tokens = None
    else:
        result.total_tokens = reported_total
    if result.issues and provider in {"claude", "dsh"}:
        result.input_tokens = result.total_tokens = None
    if read is not None and result.input_tokens is not None and read > result.input_tokens:
        result.issues.append("cache read exceeds total input")
        result.cache_read_tokens = None
    return result


@dataclass
class Call:
    id: str
    run_id: str
    provider: str
    model: str
    purpose: str
    started_at: float
    task_id: str = ""
    attempt: int = 0
    sample: str = ""
    effort: str = ""
    scope: str = "execution"
    prompt_hash: str = ""
    prefix_hash: str = ""
    request_bytes: int = 0
    base_revision: str = ""
    session_id: str = ""
    finished_at: float | None = None
    outcome: str = "pending"
    usage: dict[str, object] = field(default_factory=dict)
    legacy: bool = False
    usage_valid: bool | None = None


class Ledger:
    def __init__(self, store: Store, clock: Callable[[], float]):
        self.store, self.clock = store, clock
        with store.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS orch_calls (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    started_at REAL NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS orch_calls_run ON orch_calls(run_id, started_at);
            """)

    @contextmanager
    def capture(
        self, call: Call, checkpoint: Callable[[sqlite3.Connection], None] | None = None,
    ) -> Iterator[Call]:
        with self.store.transaction() as db:
            db.execute("INSERT INTO orch_calls VALUES (?,?,?,?)",
                       (call.id, call.run_id, call.started_at, dumps(asdict(call))))
        try:
            yield call
        except BaseException as error:
            call.outcome = f"exception:{type(error).__name__}"
            raise
        finally:
            call.finished_at = self.clock()
            call.usage_valid = all(
                name not in call.usage or count(call.usage, name) is not None
                for name in COUNTERS
            ) and all(
                name not in call.usage or call.usage[name] == call.model
                for name in ("served_model", "model")
            )
            call.usage = usage_fields(call.usage)
            with self.store.transaction() as db:
                db.execute("UPDATE orch_calls SET body=? WHERE id=?",
                           (dumps(asdict(call)), call.id))
                if checkpoint is not None:
                    checkpoint(db)

    def calls(self, run_id: str = "", *, limit: int = 0) -> list[Call]:
        with self.store.transaction() as db:
            return self.read(db, run_id, limit=limit)

    @staticmethod
    def read(db: sqlite3.Connection, run_id: str = "", *, limit: int = 0) -> list[Call]:
        """Read receipts inside the caller's transaction when they affect a state decision."""
        rows = db.execute(
            "SELECT body FROM orch_calls " + ("WHERE run_id=? " if run_id else "")
            + ("ORDER BY started_at DESC, rowid DESC LIMIT ?" if limit > 0
               else "ORDER BY started_at, rowid"),
            ((*((run_id,) if run_id else ()), limit) if limit > 0
             else (run_id,) if run_id else ()),
        ).fetchall()
        if limit > 0:
            rows.reverse()
        known = {item.name for item in fields(Call)}
        return [Call(**{key: value for key, value in json.loads(row["body"]).items()
                        if key in known}) for row in rows]

    def latest_run(self) -> str:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT run_id FROM orch_calls ORDER BY started_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise Conflict("no call receipts yet")
        return str(row["run_id"])


def summary(calls: list[Call]) -> dict[str, object]:
    normalized = [asdict(tokens(call.provider, call.usage)) for call in calls]
    sums: dict[str, int] = {}
    known: dict[str, int] = {}
    for key in METRICS:
        values = [value[key] for value in normalized if value[key] is not None]
        sums[key] = sum(values)
        known[key] = len(values)
    complete_cache = all(
        value["input_tokens"] is not None and value["cache_read_tokens"] is not None
        for value in normalized
    )
    hit = (sums["cache_read_tokens"] / sums["input_tokens"]
           if complete_cache and sums["input_tokens"] else None)
    costs: dict[str, object] = {}
    for key in ("settled_cents", *MONEY):
        values = [call.usage[key] for call in calls if key in call.usage]
        costs[key] = {"known_sum": sum(float(str(v)) for v in values),
                      "reported_calls": len(values)}
    return {"calls": len(calls), "known_tokens": sums, "reported_calls": known,
            "cache_hit_rate": hit, "costs": costs,
            "pending_calls": sum(call.outcome == "pending" for call in calls),
            "legacy_calls": sum(call.legacy for call in calls)}


def grouped(calls: list[Call], by: str) -> dict[str, list[Call]]:
    groups: dict[str, list[Call]] = {}
    for call in calls:
        key = {"task": call.task_id or "[shared]", "model": f"{call.provider}/{call.model}",
               "purpose": call.purpose}[by]
        groups.setdefault(key, []).append(call)
    return groups


def report(calls: list[Call], by: str) -> dict[str, object]:
    return {"schema_version": 1, "by": by, "totals": summary(calls),
            "groups": {key: summary(items) for key, items in sorted(grouped(calls, by).items())},
            "calls": [{**asdict(call), "tokens": asdict(tokens(call.provider, call.usage))}
                      for call in calls]}


def lines(calls: list[Call], by: str, details: bool = False) -> list[str]:
    groups = grouped(calls, by)
    output = [f"{by:<26} calls input output total cache_read cache_write hit% ledger_CNY"]
    for key, items in [*sorted(groups.items()), ("TOTAL", calls)]:
        counts = [tokens(call.provider, call.usage) for call in items]
        cells = []
        for metric in METRICS[:-1]:
            values = [getattr(value, metric) for value in counts]
            present = [v for v in values if v is not None]
            cells.append(str(sum(present)) + ("+?" if len(present) != len(values) else "")
                         if present else "?")
        hit = summary(items)["cache_hit_rate"]
        money = [count(call.usage, "settled_cents") for call in items]
        cents = sum(v for v in money if v is not None)
        amount = f"{cents / 100:.2f}" + ("+?" if any(v is None for v in money) else "")
        hit_text = f"{float(str(hit)):.1%}" if hit is not None else "?"
        output.append(f"{key:<26} {len(items):>5} " + " ".join(cells) + f" {hit_text} {amount}")
    output += ["? = unreported; N+? = known subtotal, incomplete. Cache hit = read / all input.",
               "ledger_CNY is conservative budget settlement, not the provider invoice."]
    if any(call.legacy for call in calls):
        output.append("Legacy events: planning and interrupted calls may be missing.")
    pending = sum(call.outcome == "pending" for call in calls)
    if pending:
        output.append(f"{pending} pending receipt(s): in flight or interrupted; usage unknown.")
    if details:
        for call in calls:
            output.append(
                f"call {call.id} task={call.task_id or '[shared]'} a{call.attempt} "
                f"sample={call.sample or '-'} {call.provider}/{call.model} "
                f"purpose={call.purpose} scope={call.scope} outcome={call.outcome} "
                f"prefix={call.prefix_hash or '-'} prompt={call.prompt_hash or '-'} "
                f"base={call.base_revision or '-'} session={call.session_id or '-'} "
                f"started={call.started_at} finished={call.finished_at}"
            )
            output.append("  tokens=" + dumps(asdict(tokens(call.provider, call.usage))))
            output.append("  reported_usage=" + dumps(call.usage))
    return output
