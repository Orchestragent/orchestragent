"""Where a run's wall time went: stages and model calls, read from the run's own record.

A change to orch is judged by what it does to the critical path, so every gate run reports
it rather than leaving it to be reconstructed by hand:

- plan: the planner's look-around and boundary drafts (first call to last plan call);
- design: test design (components and integration checks), up to the frozen plan;
- build: the run's start to the last task's merge or park;
- final: the final gate (all checks, component suites, any bisect) to the run's end;
- calls: per (provider, purpose): count, summed seconds and the longest call.

Times overlap where work runs in parallel, so stage spans are wall time and call sums are
work; their ratio shows how much parallelism a stage had.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

PLAN_PURPOSES = frozenset({"look", "plan", "boundary-draft", "boundary-review"})
DESIGN_PURPOSES = frozenset({"test-design", "integration-design"})
END_KINDS = ("run.done", "run.parked", "run.failed", "run.metrics")


def state_db(project: Path) -> Path | None:
    """The orch state database of a project directory, if a run happened there."""
    found = sorted(project.glob(".git/orch/state.sqlite"))
    return found[0] if found else None


def stages(db: Path) -> dict[str, Any]:
    """Stage spans (seconds) and per-purpose call time for the run in `db`."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        calls = []
        for (body,) in con.execute("SELECT body FROM orch_calls"):
            item = json.loads(body)
            start, end = item.get("started_at"), item.get("finished_at")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                calls.append((str(item.get("provider", "")), str(item.get("purpose", "")),
                              float(start), float(end)))
        events = [(str(kind), str(task), float(at)) for kind, task, at in con.execute(
            "SELECT kind, task_id, at FROM orch_events ORDER BY seq")]
    finally:
        con.close()
    out: dict[str, Any] = {"plan": None, "design": None, "build": None, "final": None,
                           "calls": {}}
    if not calls and not events:
        return out
    first = min([c[2] for c in calls] + [e[2] for e in events])
    planned = [c for c in calls if c[1] in PLAN_PURPOSES]
    designed = [c for c in calls if c[1] in DESIGN_PURPOSES]
    created = next((at for kind, _, at in events if kind == "run.created"), None)
    started = next((at for kind, _, at in events if kind == "run.started"), None)
    if planned:
        out["plan"] = round(max(c[3] for c in planned) - first, 1)
    if designed or created is not None:
        begin = max(c[3] for c in planned) if planned else first
        end = created if created is not None else max(c[3] for c in designed)
        out["design"] = round(max(0.0, end - begin), 1)
    finished = [at for kind, task, at in events
                if task and kind in {"task.done", "task.parked"}]
    if started is not None and finished:
        last = max(finished)
        out["build"] = round(last - started, 1)
        ends = [at for kind, task, at in events if not task and kind in END_KINDS]
        if ends:
            out["final"] = round(max(0.0, max(ends) - last), 1)
    table: dict[str, dict[str, float]] = {}
    for provider, purpose, start, end in calls:
        row = table.setdefault(f"{provider}:{purpose}", {"n": 0, "seconds": 0.0, "max": 0.0})
        row["n"] += 1
        row["seconds"] = round(row["seconds"] + end - start, 1)
        row["max"] = round(max(row["max"], end - start), 1)
    out["calls"] = dict(sorted(table.items(), key=lambda kv: -kv[1]["seconds"]))
    return out


def stage_line(timing: dict[str, Any]) -> str:
    """plan/design/build/final in seconds, '-' where a stage did not happen."""
    return " / ".join("-" if timing.get(k) is None else f"{timing[k]:.0f}"
                      for k in ("plan", "design", "build", "final"))
