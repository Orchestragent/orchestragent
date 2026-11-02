"""`orch watch`: a read-only, content-free architecture view of one orchestration run.

The state database is opened with SQLite `mode=ro`; nothing here writes to it, and a run
may be writing concurrently. `derive` turns the durable event log and call ledger into a
time-indexed graph model (node state histories, call bars, travelling messages), so live,
replay and offline export all render the same model and only slice it at a different clock.
Only whitelisted summaries leave this module: never prompts, responses, specs or secrets.
"""

import json
import re
import sqlite3
import time
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from orchestragent.orch_state import OrchEvent
from orchestragent.orch_usage import Call, tokens

Json = dict[str, Any]
SCHEMA = 1
HTML = Path(__file__).with_name("watch") / "index.html"
BOOT_MARK = "/*ORCH_BOOT*/null"

STATUS = {"PLANNED": "planned", "READY": "ready", "RUNNING": "running",
          "VERIFYING": "verifying", "MERGING": "merging", "DONE": "done",
          "PARKED": "parked", "REPLANNED": "replanned"}
DESIGN = frozenset({"test-design", "integration-test-design", "test-repair",
                    "integration-test-repair"})
INTEGRATION = "∫"  # marks the integration test-design shard (ASCII views print "int")
TERMINAL = {"run.done": "done", "run.parked": "parked", "run.paused": "paused"}
# Event payload keys that are short, structural and content-free. Anything else (reports,
# error text, diffs, environment paths, usage blobs) never leaves the database.
DATA_KEYS = frozenset({
    "status", "level", "next", "category", "signature", "outcome", "route", "sample",
    "passed", "tests", "skipped", "candidates", "passing", "requests", "selectors", "commit",
    "head", "candidate", "call_id", "purpose", "depends_on", "interface_dependencies", "review",
    "rule", "kind", "task", "source", "base", "jobs", "verify_jobs", "rpm", "cache_mode",
    "peak_generating", "peak_verifying", "blocked_rate", "failures", "planned", "bytes",
    "context_requests", "dependencies", "refreshes", "note", "map_bytes", "score", "limit",
    "cache_affinity", "revision", "max_cents", "candidate_reuses", "reason", "plan",
})
SECRET = re.compile(
    r"(?i)\b(?:sk|pk|rk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{8,}"
    r"|bearer\s+[A-Za-z0-9._~+/=-]+"
    r"|(?:api[_-]?key|token|secret|password|passwd|authorization|cookie)\s*[:=]\s*\S+"
)
URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,15}://\S*")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(value: object, limit: int = 160) -> str:
    """Bounded display text: secrets and URLs redacted, control characters removed."""
    text = CONTROL.sub(" ", str(value))
    text = SECRET.sub("[redacted]", URL.sub("[url]", text))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def scrub(value: object, depth: int = 0) -> object:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, (list, tuple)) and depth < 2:
        items = [scrub(item, depth + 1) for item in list(value)[:12]]
        return items + (["…"] if len(value) > 12 else [])
    if isinstance(value, dict) and depth < 2:
        return {clean(k, 40): scrub(v, depth + 1) for k, v in list(value.items())[:12]}
    text = str(value)
    if re.fullmatch(r"[0-9a-f]{40}", text):
        text = text[:12]  # commit ids: short form is enough for display
    return clean(text, 120)


def summary_data(data: Json) -> Json:
    return {key: scrub(value) for key, value in data.items() if key in DATA_KEYS}


# --- read-only access -------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    run_id: str
    run: Json
    tasks: list[Json]
    events: list[OrchEvent]
    calls: list[Call]
    observed_at: float


def connect(path: Path) -> sqlite3.Connection:
    """Open the state database strictly read-only; a missing file is an error, not a create."""
    if not path.is_file():
        raise FileNotFoundError(f"no orchestration state at {path}")
    uri = "file:" + urllib.parse.quote(str(path.resolve())) + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None,
                         check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def _tables(db: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def runs(db: sqlite3.Connection) -> list[Json]:
    """Every run with its last activity, newest first; planning-only runs included."""
    present = _tables(db)
    parts = [sql for table, sql in (
        ("orch_runs", "SELECT id AS run_id, created_at AS t FROM orch_runs"),
        ("orch_calls", "SELECT run_id, MAX(started_at) AS t FROM orch_calls GROUP BY run_id"),
        ("orch_events", "SELECT run_id, MAX(at) AS t FROM orch_events GROUP BY run_id"),
    ) if table in present]
    if not parts:
        return []
    rows = db.execute(
        "SELECT run_id, MAX(t) AS t FROM (" + " UNION ALL ".join(parts)
        + ") GROUP BY run_id ORDER BY t DESC").fetchall()
    status: dict[str, str] = {}
    if "orch_runs" in present:
        for row in db.execute("SELECT id, body FROM orch_runs"):
            status[str(row["id"])] = str(json.loads(row["body"]).get("status", ""))
    return [{"id": str(row["run_id"]), "last": float(row["t"] or 0.0),
             "status": status.get(str(row["run_id"]), "planning")} for row in rows]


def latest_run(db: sqlite3.Connection) -> str:
    found = runs(db)
    return str(found[0]["id"]) if found else ""


def _call(body: str) -> Call:
    known = {item.name for item in fields(Call)}
    return Call(**{k: v for k, v in json.loads(body).items() if k in known})


def read(db: sqlite3.Connection, run_id: str, observed_at: float) -> Snapshot:
    """One consistent read transaction: events and receipts from the same WAL snapshot."""
    present = _tables(db)
    db.execute("BEGIN")
    try:
        run: Json = {"id": run_id}
        tasks: list[Json] = []
        events: list[OrchEvent] = []
        calls: list[Call] = []
        if "orch_runs" in present:
            row = db.execute("SELECT body FROM orch_runs WHERE id=?", (run_id,)).fetchone()
            if row is not None:
                body = json.loads(row["body"])
                run = {"id": run_id, "status": clean(body.get("status", ""), 24),
                       "message": clean(body.get("message", "")),
                       "branch": clean(body.get("branch", ""), 80),
                       "strategy": clean(body.get("strategy", ""), 40),
                       "base": clean(body.get("base", ""), 12),
                       "created_at": body.get("created_at")}
        if "orch_tasks" in present:
            for row in db.execute("SELECT body FROM orch_tasks WHERE run_id=?", (run_id,)):
                body = json.loads(row["body"])
                tasks.append({
                    "id": clean(body.get("id", ""), 40), "status": body.get("status", ""),
                    "goal": clean(body.get("goal", ""), 200),
                    "reqs": scrub(body.get("reqs", [])), "files": scrub(body.get("files", [])),
                    "depends_on": [str(d) for d in body.get("depends_on", [])],
                    "interface_depends_on": [str(d) for d in body.get("interface_depends_on", [])],
                    "attempts": body.get("attempts", 0), "level": body.get("level", 0),
                    "reason": clean(body.get("reason", "")),
                    "commit": clean(body.get("commit", ""), 12),
                })
        if "orch_events" in present:
            for row in db.execute(
                    "SELECT seq, run_id, task_id, span, parent, kind, at, data FROM orch_events "
                    "WHERE run_id=? ORDER BY seq", (run_id,)):
                data = json.loads(row["data"])
                events.append(OrchEvent(int(row["seq"]), str(row["run_id"]), str(row["task_id"]),
                                        str(row["span"]), str(row["parent"]), str(row["kind"]),
                                        float(row["at"]), data if isinstance(data, dict) else {}))
        if "orch_calls" in present:
            calls = [_call(row["body"]) for row in db.execute(
                "SELECT body FROM orch_calls WHERE run_id=? ORDER BY started_at, rowid",
                (run_id,))]
    finally:
        db.execute("COMMIT")
    return Snapshot(run_id, run, tasks, events, calls, observed_at)


def data_version(db: sqlite3.Connection) -> int:
    """Changes whenever another connection commits; cheap change detection for polling."""
    return int(db.execute("PRAGMA data_version").fetchone()[0])


class Source:
    """Polls one state database read-only; `poll` returns a model only when something changed.

    An empty `run` follows the newest run. A missing file means "not started yet", not an error.
    """

    def __init__(self, path: Path, run: str = "", clock: Callable[[], float] = time.time) -> None:
        self.path, self.run, self.clock = path, run, clock
        self.db: sqlite3.Connection | None = None
        self.seen: tuple[str, int] | None = None

    def poll(self) -> Json | None:
        if self.db is None:
            if not self.path.is_file():
                return None
            self.db = connect(self.path)
        run = self.run or latest_run(self.db)
        marker = (run, data_version(self.db))
        if not run or marker == self.seen:
            return None
        self.seen = marker
        return derive(read(self.db, run, self.clock()))

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None


# --- model derivation (pure) -------------------------------------------------------


@dataclass
class Node:
    id: str
    label: str
    sub: str
    group: str
    order: int = 0
    depth: int = 0
    history: list[list[Any]] = field(default_factory=list)
    meta: Json = field(default_factory=dict)
    critical: bool = False


FIXED = (
    Node("cli", "You / CLI", "orch plan · run · resume", "user"),
    Node("orch", "Orchestrator", "DAG scheduler · ladder", "orch"),
    Node("planner", "Opus planner", "Claude · plan + test DAG", "planner"),
    Node("guard", "DSH guard", "request guard · model pin · CNY ledger", "guard"),
    Node("deepseek", "DeepSeek API", "deepseek-flash", "provider"),
    Node("claude", "Claude API", "claude-opus-5-5", "provider"),
    Node("sandbox", "bwrap sandbox", "verifier · frozen tests", "sandbox"),
    Node("merge", "git merge", "merge queue · integration branch", "merge"),
)


def natural(text: str) -> list[object]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def owner(call: Call) -> str:
    if call.purpose in DESIGN and call.task_id:
        integration = call.purpose.startswith("integration-")
        return f"design:{call.task_id}" + (":int" if integration else "")
    if call.scope in {"planning", "evolve"} or not call.task_id:
        return "planner" if call.scope in {"planning", "evolve"} else "orch"
    return f"task:{call.task_id}"


def route(provider: str) -> list[str]:
    return {"dsh": ["guard", "deepseek"], "claude": ["claude"]}.get(provider, [f"api:{provider}"])


def attempt_of(span: str) -> int:
    match = re.search(r"/a(\d+)$", span)
    return int(match.group(1)) if match else 0


def sweep(intervals: Iterable[tuple[float, float | None, bool]], rest: str) -> list[list[Any]]:
    """Step history from overlapping intervals: running while any is open.

    `rest` is the idle state; "outcome" means done/failed by the last finished interval.
    """
    points: list[tuple[float, int, bool]] = []
    for start, end, ok in intervals:
        points.append((start, 1, ok))
        if end is not None:
            points.append((max(end, start), -1, ok))
    points.sort(key=lambda item: (item[0], -item[1]))
    history: list[list[Any]] = []
    load = 0
    for at, step, ok in points:
        load += step
        if load > 0:
            state = "running"
        elif rest == "outcome":
            state = "done" if ok else "failed"
        else:
            state = rest
        info = f"{load} in flight" if load > 1 else ""
        if history and history[-1][0] == at:
            history[-1] = [at, state, info]
        elif not history or history[-1][1:] != [state, info]:
            history.append([at, state, info])
    return history


def token_counts(call: Call) -> dict[str, int | None]:
    counted = tokens(call.provider, call.usage)
    return {"input": counted.input_tokens, "output": counted.output_tokens,
            "cache_read": counted.cache_read_tokens, "cache_write": counted.cache_write_tokens,
            "total": counted.total_tokens}


def money(call: Call) -> tuple[float | None, float | None]:
    cents = call.usage.get("settled_cents")
    usd = call.usage.get("cost_usd", call.usage.get("provider_estimate_usd"))
    return (float(cents) if isinstance(cents, (int, float)) else None,
            float(usd) if isinstance(usd, (int, float)) else None)


def _receipt(call: Call, node: str) -> Json:
    end = call.finished_at
    counts = token_counts(call)
    cny, usd = money(call)
    seconds = (end - call.started_at) if end is not None else None
    output = counts["output"]
    return {
        "id": clean(call.id, 120), "node": node, "via": route(call.provider),
        "provider": clean(call.provider, 24), "model": clean(call.model, 60),
        "purpose": clean(call.purpose, 40), "scope": clean(call.scope, 20),
        "task": clean(call.task_id, 40), "attempt": call.attempt,
        "sample": clean(call.sample, 40), "effort": clean(call.effort, 20),
        "start": call.started_at, "end": end, "outcome": clean(call.outcome, 40),
        "tokens": counts, "cny_cents": cny, "usd": usd,
        "tps": round(output / seconds, 1) if output and seconds and seconds > 0 else None,
        "request_bytes": call.request_bytes, "critical": False,
    }


def _orphans(events: list[OrchEvent], calls: list[Call]) -> list[Call]:
    """attempt.called events with no ledger receipt (legacy logs): in flight until a result."""
    unused = sorted(calls, key=lambda call: call.started_at)
    taken: set[int] = set()
    found: list[Call] = []
    results = [event for event in events if event.kind == "attempt.result"]
    for event in events:
        if event.kind != "attempt.called":
            continue
        attempt = attempt_of(event.span)
        match = next((index for index, call in enumerate(unused) if index not in taken
                      and call.task_id == event.task_id and call.attempt == attempt
                      and abs(call.started_at - event.at) < 30), None)
        if match is not None:
            taken.add(match)
            continue
        key = event.data.get("review") or event.data.get("sample", "")
        done = next((result for result in results if result.span == event.span
                     and result.at >= event.at
                     and (result.data.get("review") or result.data.get("sample", "")) == key),
                    None)
        route_label = str(event.data.get("route", "?/?"))
        provider, _, model = route_label.partition("/")
        found.append(Call(
            f"event-{event.seq}", event.run_id, provider, model,
            str(event.data.get("purpose", "build")), event.at, task_id=event.task_id,
            attempt=attempt, sample=str(event.data.get("sample", "")),
            finished_at=done.at if done else None,
            outcome=str(done.data.get("outcome", "ok")) if done else "pending",
            usage=_usage(done.data.get("usage") if done else None),
        ))
    return found


def _usage(value: object) -> Json:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _depths(deps: dict[str, list[str]]) -> dict[str, int]:
    depth: dict[str, int] = {}
    for _ in range(len(deps) + 1):  # iterative relaxation; cycles simply stop growing
        changed = False
        for task, parents in deps.items():
            value = 1 + max((depth.get(p, 0) for p in parents if p in deps), default=-1)
            if depth.get(task) != value:
                depth[task], changed = value, True
        if not changed:
            break
    return depth


def derive(snap: Snapshot) -> Json:
    """The whole time-indexed graph model for one run (pure; no I/O, no clock)."""
    events = sorted(snap.events, key=lambda event: event.seq)
    raw_calls = sorted([*snap.calls, *_orphans(events, snap.calls)],
                       key=lambda call: (call.started_at, call.id))
    calls = [_receipt(call, owner(call)) for call in raw_calls]
    nodes: dict[str, Node] = {node.id: Node(**{**asdict(node), "history": [], "meta": {}})
                              for node in FIXED}

    # Tasks: the plan table, plus anything events or receipts mention (planning-only runs).
    task_rows = {str(task["id"]): task for task in snap.tasks}
    deps: dict[str, list[str]] = {tid: list(map(str, row.get("depends_on", [])))
                                  for tid, row in task_rows.items()}
    for event in events:
        if event.kind == "task.planned" and event.task_id and event.task_id not in deps:
            planned = event.data.get("depends_on")
            deps[event.task_id] = [str(d) for d in planned] if isinstance(planned, list) else []
    task_ids = set(deps) | {event.task_id for event in events if event.task_id}
    task_ids |= {str(call["task"]) for call in calls if str(call["node"]).startswith("task:")}
    for tid in task_ids:
        deps.setdefault(tid, [])
    depth = _depths(deps)
    for order, tid in enumerate(sorted(task_ids, key=natural)):
        row = task_rows.get(tid, {})
        goal = str(row.get("goal", ""))
        nodes[f"task:{tid}"] = Node(
            f"task:{tid}", tid, clean(goal, 44) if goal else "build task", "task", order,
            depth.get(tid, 0), meta={k: v for k, v in row.items() if k != "id"} | {
                "depends_on": deps.get(tid, [])})
    # Unit and integration test design for one task are separate shards with their own lanes.
    design_ids = sorted({str(call["node"]) for call in calls
                         if str(call["node"]).startswith("design:")},
                        key=lambda node_id: (natural(node_id.split(":")[1]), node_id))
    for order, node_id in enumerate(design_ids):
        tid, integration = node_id.split(":")[1], node_id.endswith(":int")
        nodes[node_id] = Node(
            node_id, f"design {tid}" + (f" {INTEGRATION}" if integration else ""),
            "DSF shard · integration tests" if integration else "DSF shard · frozen tests",
            "design", order)
    for call in calls:
        for hop in call["via"]:
            if hop not in nodes:
                provider = str(hop).removeprefix("api:")
                nodes[hop] = Node(hop, {"jev": "Jev (AI Gateway)"}.get(provider, provider.title()),
                                  str(call["model"]), "provider", len(nodes))

    # Histories: model-call nodes from receipts, tasks from their state events.
    by_node: dict[str, list[tuple[float, float | None, bool]]] = {}
    for call in calls:
        interval = (float(call["start"]),
                    None if call["end"] is None else float(call["end"]),
                    call["outcome"] == "ok")
        by_node.setdefault(str(call["node"]), []).append(interval)
        for hop in call["via"]:
            by_node.setdefault(str(hop), []).append(interval)
    for node_id, spans in by_node.items():
        if node_id in nodes and not node_id.startswith("task:") and node_id != "orch":
            nodes[node_id].history = sweep(
                spans, "outcome" if nodes[node_id].group in {"planner", "design"} else "idle")

    verify: list[tuple[float, float | None, bool]] = []
    merging: list[tuple[float, float | None, bool]] = []
    intervals: list[Json] = []
    open_phase: dict[str, tuple[str, float]] = {}
    last_done = 0.0
    for event in events:
        tid = event.task_id
        status = event.data.get("status")
        if not tid:
            continue
        node = nodes[f"task:{tid}"]
        state = "failed" if event.kind == "task.failed" else STATUS.get(str(status), "")
        if event.kind == "task.planned":
            state = "planned"
        if event.kind == "task.done":
            last_done = max(last_done, event.at)
        if state and (not node.history or node.history[-1][1] != state):
            node.history.append([event.at, state, f"a{attempt_of(event.span)}"
                                 if attempt_of(event.span) else ""])
            if tid in open_phase:
                kind, began = open_phase.pop(tid)
                intervals.append({"node": f"task:{tid}", "kind": kind, "start": began,
                                  "end": event.at})
                (verify if kind == "verify" else merging).append((began, event.at, True))
            if state in {"verifying", "merging"}:
                open_phase[tid] = ("verify" if state == "verifying" else "merge", event.at)
        if event.kind == "task.gate":
            following = next((later.at for later in events
                              if later.task_id == tid and later.seq > event.seq), None)
            verify.append((event.at, following, True))
            intervals.append({"node": f"task:{tid}", "kind": "gate", "start": event.at,
                              "end": following})
    for tid, (kind, began) in open_phase.items():
        intervals.append({"node": f"task:{tid}", "kind": kind, "start": began, "end": None})
        (verify if kind == "verify" else merging).append((began, None, True))
    run_events = {event.kind: event for event in events if not event.task_id}
    if "run.final_gate" in run_events:
        gate = run_events["run.final_gate"]
        began = last_done or gate.at
        verify.append((began, gate.at, True))
        intervals.append({"node": "sandbox", "kind": "final gate", "start": began, "end": gate.at})
        if "run.project_final" in run_events:
            project = run_events["run.project_final"]
            verify.append((gate.at, project.at, True))
            intervals.append({"node": "sandbox", "kind": "project acceptance",
                              "start": gate.at, "end": project.at})
    nodes["sandbox"].history = sweep(verify, "idle")
    nodes["merge"].history = sweep(merging, "idle")

    starts = [float(call["start"]) for call in calls]
    stamps = [*starts, *(event.at for event in events)]
    t0 = min(stamps) if stamps else snap.observed_at
    ends = [float(call["end"]) for call in calls if call["end"] is not None]
    t1 = max([*stamps, *ends]) if stamps else snap.observed_at
    last_run = next((event for event in reversed(events)
                     if event.kind in TERMINAL or event.kind == "run.started"), None)
    terminal = TERMINAL.get(last_run.kind, "") if last_run else ""
    pending = any(call["end"] is None for call in calls)
    live = not terminal or pending

    phases: list[list[Any]] = [[t0, "planning"]]
    for event in events:
        if event.task_id:
            continue
        phase = {"run.created": "planned", "run.started": "running"}.get(event.kind) or \
            TERMINAL.get(event.kind)
        if event.kind == "run.final_gate":
            phases.append([last_done or event.at, "final gate"])
        if phase:
            phases.append([event.at, phase])
    phases.sort(key=lambda item: float(item[0]))
    orch_state = {"planning": "running", "planned": "ready", "running": "running",
                  "final gate": "verifying", "done": "done", "parked": "parked",
                  "paused": "parked"}
    for node_id in ("orch", "cli"):
        history: list[list[Any]] = []
        for at, phase in phases:
            state = orch_state[str(phase)]
            if node_id == "cli" and state not in {"done", "parked"}:
                state = "running"
            if not history or history[-1][1] != state:
                history.append([at, state, str(phase)])
        nodes[node_id].history = history

    messages = _messages(events, calls)
    critical = _critical(nodes, deps, events, calls)
    edges: dict[str, Json] = {}

    def edge(a: str, b: str, kind: str) -> None:
        if a in nodes and b in nodes and a != b and f"{b}>{a}" not in edges:
            edges.setdefault(f"{a}>{b}", {"id": f"{a}>{b}", "from": a, "to": b, "kind": kind})

    edge("cli", "orch", "control")
    edge("orch", "planner", "control")
    edge("planner", "claude", "call")
    edge("guard", "deepseek", "call")
    for node in nodes.values():
        if node.group == "design":
            edge("orch", node.id, "control")
            edge(node.id, "guard", "call")
        if node.group == "task":
            edge("orch", node.id, "control")
            edge(node.id, "guard", "call")
            edge(node.id, "sandbox", "verify")
            edge(node.id, "merge", "merge")
    edge("merge", "sandbox", "verify")
    edge("merge", "orch", "control")
    for call in calls:
        path = [str(call["node"]), *map(str, call["via"])]
        for a, b in zip(path, path[1:], strict=False):
            edge(a, b, "call")
    for message in messages:
        path = list(map(str, message["path"]))
        for a, b in zip(path, path[1:], strict=False):
            edge(a, b, "control")
    for tid, parents in deps.items():
        for parent in parents:
            edge(f"task:{parent}", f"task:{tid}", "dep")

    for call in calls:
        call["critical"] = call["id"] in critical
    for node_id in {str(call["node"]) for call in calls if call["critical"]}:
        nodes[node_id].critical = True
    log = [_log(event) for event in events]
    log += [{"seq": -1, "at": call["start"], "kind": "call.sent", "task": call["task"],
             "node": call["node"], "call": call["id"],
             "summary": f"{call['purpose']} → {call['provider']}/{call['model']}"
                        f" a{call['attempt']}"} for call in calls]
    log += [{"seq": -1, "at": call["end"], "kind": "call.done", "task": call["task"],
             "node": call["node"], "call": call["id"],
             "summary": f"{call['purpose']} {call['outcome']} {_short(call)}"}
            for call in calls if call["end"] is not None]
    log.sort(key=lambda item: (float(item["at"]), int(item["seq"])))
    return {
        "schema": SCHEMA, "run": snap.run, "now": snap.observed_at, "t0": t0,
        "t1": max(t1, snap.observed_at) if live else t1, "live": live, "terminal": terminal,
        "phases": phases, "nodes": [asdict(node) for node in nodes.values()],
        "edges": list(edges.values()), "calls": calls, "messages": messages,
        "intervals": intervals, "log": log,
    }


def _short(call: Json) -> str:
    counts = call["tokens"]
    duration = (float(call["end"]) - float(call["start"])
                if call["end"] is not None else None)
    parts = [f"{duration:.1f}s" if duration is not None else "",
             f"in {counts['input']}" if counts["input"] is not None else "",
             f"out {counts['output']} tok" if counts["output"] is not None else "",
             f"{call['tps']} tok/s" if call["tps"] else ""]
    return " · ".join(part for part in parts if part)


def _log(event: OrchEvent) -> Json:
    data = summary_data(event.data)
    node = f"task:{event.task_id}" if event.task_id else "orch"
    bits = [f"{key}={value}" for key, value in data.items()
            if key in {"status", "category", "signature", "outcome", "passed", "tests",
                       "level", "next", "requests", "route", "sample"}]
    return {"seq": event.seq, "at": event.at, "kind": event.kind, "task": event.task_id,
            "node": node, "summary": clean(" ".join(bits), 140), "data": data,
            "attempt": attempt_of(event.span)}


def _messages(events: list[OrchEvent], calls: list[Json]) -> list[Json]:
    found: list[Json] = []

    def send(at: float, path: list[str], kind: str, label: str, ref: str = "") -> None:
        found.append({"at": at, "path": path, "kind": kind, "label": clean(label, 80),
                      "ref": ref})

    for call in calls:
        node = str(call["node"])
        via = list(map(str, call["via"]))
        head = ["orch", node] if not node.startswith("task:") and node != "orch" else [node]
        retry = int(call["attempt"]) > 1 or "repair" in str(call["purpose"])
        send(float(call["start"]), [*head, *via],
             "retry" if retry else "request",
             f"{call['purpose']} a{call['attempt']} → {call['model']}", str(call["id"]))
        if call["end"] is not None:
            ok = call["outcome"] == "ok"
            send(float(call["end"]), [*reversed(via), *reversed(head)],
                 "result" if ok else "error", f"{call['purpose']} {call['outcome']}",
                 str(call["id"]))
    for event in events:
        task = f"task:{event.task_id}" if event.task_id else ""
        data = event.data
        ref = f"event:{event.seq}"
        if event.kind == "run.created" and not calls:
            send(event.at, ["cli", "orch"], "plan", "orch plan", ref)
        elif event.kind == "run.started":
            send(event.at, ["cli", "orch"], "dispatch", "orch run", ref)
        elif event.kind == "task.started":
            send(event.at, ["orch", task], "dispatch", f"dispatch a{attempt_of(event.span)}", ref)
        elif event.kind == "context.request":
            selectors = data.get("selectors")
            amount = len(selectors) if isinstance(selectors, list) else 0
            send(event.at, [task, "orch"], "context", f"NEED_FILES ×{amount}", ref)
        elif event.kind == "task.verifying":
            send(event.at, [task, "sandbox"], "verify", "verify candidate", ref)
        elif event.kind == "task.failed":
            category = str(data.get("category", ""))
            send(event.at, ["sandbox", task], "testfail" if category == "test" else "error",
                 f"failed: {category or 'error'}", ref)
        elif event.kind == "task.merging":
            send(event.at, [task, "merge"], "merge", "cherry-pick", ref)
        elif event.kind == "task.gate":
            send(event.at, ["merge", "sandbox"], "verify", f"gate {data.get('tests', '?')} tests",
                 ref)
        elif event.kind == "task.done":
            send(event.at, ["merge", "orch"], "done", f"{event.task_id} merged", ref)
        elif event.kind == "task.parked":
            send(event.at, [task, "orch"], "error", "parked", ref)
        elif event.kind in {"task.tests_fixed", "task.candidate_reused", "task.inputs_changed",
                            "task.thickened", "task.replanned"}:
            send(event.at, ["orch", task], "retry", event.kind.removeprefix("task."), ref)
        elif event.kind == "run.final_gate":
            send(event.at, ["sandbox", "merge"], "result" if data.get("passed") else "testfail",
                 f"final gate {'passed' if data.get('passed') else 'failed'}", ref)
        elif event.kind in TERMINAL:
            send(event.at, ["orch", "cli"], "done" if event.kind == "run.done" else "error",
                 event.kind.removeprefix("run."), ref)
    found.sort(key=lambda item: float(item["at"]))
    for index, item in enumerate(found):
        item["id"] = index
    return found


def _critical(nodes: dict[str, Node], deps: dict[str, list[str]], events: list[OrchEvent],
              calls: list[Json]) -> set[str]:
    """Approximate critical path: the planning chain, then the latest-finishing task chain."""
    critical: set[str] = set()
    planning = [call for call in calls if call["scope"] == "planning"]
    critical |= {str(call["id"]) for call in planning if call["node"] == "planner"}
    designs = [call for call in planning if str(call["node"]).startswith("design:")]
    if designs:
        slowest = max(designs, key=lambda call: float(call["end"] or call["start"]))
        critical |= {str(call["id"]) for call in designs if call["node"] == slowest["node"]}
    finished: dict[str, float] = {}
    for event in events:
        if event.task_id:
            finished[event.task_id] = event.at
    task = max(finished, key=lambda tid: finished[tid], default="")
    seen: set[str] = set()
    while task and task not in seen:
        seen.add(task)
        critical |= {str(call["id"]) for call in calls
                     if call["node"] == f"task:{task}" and call["scope"] != "planning"}
        parents = [p for p in deps.get(task, []) if p in finished]
        task = max(parents, key=lambda tid: finished[tid], default="")
    for tid in seen:
        if f"task:{tid}" in nodes:
            nodes[f"task:{tid}"].critical = True
    return critical


# --- slicing at a clock (the TUI and tests; the web UI does the same in JS) --------


def state_at(history: list[list[Any]], t: float) -> tuple[str, float | None, str]:
    current: tuple[str, float | None, str] = ("idle", None, "")
    for at, state, info in history:
        if float(at) > t:
            break
        current = (str(state), float(at), str(info))
    return current


def view(model: Json, t: float) -> Json:
    """What the graph shows at clock `t`: phase, node states, in-flight calls and totals."""
    phases = model["phases"]
    phase = "planning"
    for at, name in phases:
        if float(at) <= t:
            phase = str(name)
    nodes = {str(node["id"]): state_at(node["history"], t)
             for node in model["nodes"]}
    started = [call for call in model["calls"] if float(call["start"]) <= t]
    live = flying(model, t)
    finished = [call for call in started if call not in live]
    cny = sum(float(call["cny_cents"]) for call in finished if call["cny_cents"] is not None)
    usd = sum(float(call["usd"]) for call in finished if call["usd"] is not None)
    output = sum(int(call["tokens"]["output"] or 0) for call in finished)
    incoming = sum(int(call["tokens"]["input"] or 0) for call in finished)
    return {"phase": phase, "nodes": nodes, "in_flight": live, "finished": finished,
            "calls": len(started), "output": output, "input": incoming,
            "cny_cents": cny, "usd": usd,
            "elapsed": max(0.0, t - float(model["t0"]))}


def stats(model: Json, t: float) -> dict[str, Json]:
    """Per node at clock `t`: calls, in flight, finished input/output tokens, cost, tok/s.

    tok/s is always output tokens over the call's duration.

    Worker nodes count the calls they own; gateway and provider nodes the calls they carry.
    """
    found: dict[str, Json] = {
        str(node["id"]): {"calls": 0, "live": 0, "output": 0, "input": 0, "cny_cents": 0.0,
                          "usd": 0.0, "tps": None} for node in model["nodes"]}
    for call in model["calls"]:
        if float(call["start"]) > t:
            continue
        done = call["end"] is not None and float(call["end"]) <= t
        for node_id in (str(call["node"]), *map(str, call["via"])):
            row = found.get(node_id)
            if row is None:
                continue
            row["calls"] += 1
            if not done:
                row["live"] += 1
                continue
            row["output"] += int(call["tokens"]["output"] or 0)
            row["input"] += int(call["tokens"]["input"] or 0)
            row["cny_cents"] += float(call["cny_cents"] or 0)
            row["usd"] += float(call["usd"] or 0)
            if call["tps"]:
                row["tps"] = call["tps"]
    return found


def flying(model: Json, t: float) -> list[Json]:
    return [call for call in model["calls"] if float(call["start"]) <= t
            and (call["end"] is None or float(call["end"]) > t)]


# --- HTML (served page and offline export) -----------------------------------------


def embed(value: object) -> str:
    """JSON safe inside <script type=application/json>: no tag, comment or entity breakout."""
    text = json.dumps(value, separators=(",", ":"), allow_nan=False, default=str)
    for raw, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                         ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        text = text.replace(raw, escaped)
    return text


def page(boot: Json) -> str:
    template = HTML.read_text(encoding="utf-8")
    if BOOT_MARK not in template:
        raise RuntimeError("watch page template lost its boot marker")
    return template.replace(BOOT_MARK, embed(boot), 1)


def export(model: Json, target: Path, speed: float = 8.0) -> Path:
    """One self-contained HTML file that replays the run offline (no server, no network)."""
    target.write_text(page({"mode": "export", "model": model, "speed": speed}),
                      encoding="utf-8")
    return target
