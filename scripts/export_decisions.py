"""Supervisor decisions and what they led to, as JSON lines: training and evaluation data.

    python3 scripts/export_decisions.py .data/gate/thinker-flash-high > decisions.jsonl
    python3 scripts/export_decisions.py path/to/project --summary

Each line is one supervisor decision: the evidence it read and the answer it gave (kept by
the engine under <run>/supervision/), and facts observed afterwards. Nothing here grades a
decision; the facts are what a reward or an evaluation would be built from:

- applied: the engine accepted the action (a split or a test revision can be refused);
- next: what happened to the task next: "done", another "supervision", "parked", or
  "replanned" into new tasks (with how many of those finished);
- task_final: the task's final status; run_status: the run's;
- acceptance: the benchmark's hidden acceptance tests on the delivered result, when the run
  came from scripts/orch_gate.py (None otherwise). A revised test that let wrong code merge
  shows up here, not in the task's own checks.

Decisions recorded before the engine kept prompts carry "prompt": null.
"""

from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

OUTCOME_KINDS = ("task.done", "task.parked", "task.replanned", "task.supervised")


def databases(root: Path) -> list[Path]:
    """state.sqlite of every orch home under `root` (a project or a directory of runs)."""
    found = set(root.glob("**/.git/orch/state.sqlite")) | set(root.glob(".git/orch/state.sqlite"))
    return sorted(found)


def acceptance_of(db: Path) -> bool | None:
    """The gate's acceptance verdict: report.json beside the project directory
    (<case>/project/.git/orch/state.sqlite -> <case>/report.json)."""
    report = db.parents[3] / "report.json"
    if not report.is_file():
        return None
    value = json.loads(report.read_text()).get("acceptance_passed")
    return value if isinstance(value, bool) else None


def decisions(db: Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    home = db.parent
    acceptance = acceptance_of(db)
    out: list[dict[str, Any]] = []
    for (run_id, body) in con.execute("SELECT id, body FROM orch_runs"):
        run = json.loads(body)
        tasks = {row["id"]: json.loads(row["body"]) for row in con.execute(
            "SELECT id, body FROM orch_tasks WHERE run_id=?", (run_id,))}
        events = [dict(row) | {"data": json.loads(row["data"])} for row in con.execute(
            "SELECT seq, task_id, kind, span, data FROM orch_events WHERE run_id=? ORDER BY seq",
            (run_id,))]
        by_task: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for event in events:
            by_task[event["task_id"]].append(event)
        for task_id, mine in by_task.items():
            for index, event in enumerate(mine):
                if event["kind"] != "task.supervised":
                    continue
                later = mine[index + 1:]
                data = event["data"]
                # The action's application fails right after the decision, or not at all.
                refused = next((e for e in later[:3] if e["kind"] == "task.failed"
                                and e["data"].get("category") == "supervisor"), None)
                following = next((e for e in later if e["kind"] in OUTCOME_KINDS), None)
                nxt: dict[str, Any] = {"kind": None}
                if following is not None:
                    kind = following["kind"].removeprefix("task.")
                    nxt = {"kind": "supervision" if kind == "supervised" else kind}
                    if kind == "replanned":
                        into = following["data"].get("into", [])
                        into = into if isinstance(into, list) else []
                        nxt["into"] = into
                        nxt["into_done"] = sum(tasks.get(t, {}).get("status") == "DONE"
                                               for t in into)
                record = data.get("record")
                kept = home / run_id / "supervision" / record if record else None
                saved = json.loads(kept.read_text()) if kept and kept.is_file() else None
                out.append({
                    "run": run_id, "task": task_id, "attempt": event["span"].rsplit("/a", 1)[-1],
                    "supervision": data.get("supervision"),
                    "route": saved.get("route") if saved else None,
                    "action": data.get("action"), "explanation": data.get("explanation"),
                    "prompt": saved.get("prompt") if saved else None,
                    "answer": saved.get("answer") if saved else None,
                    "applied": refused is None,
                    "refusal": str(refused["data"].get("report", ""))[:500] if refused else "",
                    "next": nxt,
                    "task_final": tasks.get(task_id, {}).get("status"),
                    "run_status": run.get("status"),
                    "acceptance": acceptance,
                    "source": str(db),
                })
    con.close()
    return out


def summary(rows: list[dict[str, Any]]) -> str:
    """Per action: how often it was applied and how often the task then finished."""
    table: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for row in rows:
        counts = table[str(row["action"])]
        counts["n"] += 1
        counts["applied"] += row["applied"]
        counts["task done"] += row["task_final"] == "DONE"
        counts["accepted run"] += row["acceptance"] is True
        counts["next: done"] += row["next"]["kind"] == "done"
        counts["with prompt"] += row["prompt"] is not None
    columns = ["n", "applied", "next: done", "task done", "accepted run", "with prompt"]
    lines = ["| action | " + " | ".join(columns) + " |",
             "|---|" + "---:|" * len(columns)]
    for action in sorted(table):
        lines.append(f"| {action} | " + " | ".join(str(table[action][c]) for c in columns) + " |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("roots", nargs="+", type=Path,
                        help="projects, gate output directories, or directories of them")
    parser.add_argument("--summary", action="store_true", help="a table instead of JSON lines")
    args = parser.parse_args()
    rows = [row for root in args.roots for db in databases(root.resolve())
            for row in decisions(db)]
    if args.summary:
        print(summary(rows))
    else:
        for row in rows:
            sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
