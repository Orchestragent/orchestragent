"""Aggregate dispatch metrics from persisted tasks, events and memory."""

import json
import statistics
from collections import defaultdict

from orchestragent.store import Store


def snapshot(store: Store, project: str = "") -> dict[str, object]:
    with store.transaction() as db:
        rows = db.execute("SELECT id, body FROM tasks").fetchall()
        events = db.execute(
            "SELECT task_id, kind, at, data FROM events ORDER BY seq"
        ).fetchall()
    tasks = []
    for task_id, body in rows:
        value = json.loads(body)
        value["id"] = task_id
        if project and str(value.get("project", "default")) != project:
            continue
        tasks.append(value)

    by_status: dict[str, int] = defaultdict(int)
    tokens: dict[str, dict[str, int]] = defaultdict(lambda: {"input": 0, "output": 0, "cache": 0})
    for task in tasks:
        by_status[str(task.get("status", "unknown"))] += 1
        providers = {
            str(step.get("role")): str(step.get("provider") or "")
            for step in task.get("steps", [])
            if isinstance(step, dict)
        }
        for key, entry in (task.get("usage") or {}).items():
            if not isinstance(entry, dict):
                continue
            role = str(key).split(":")[0]
            provider = providers.get(role) or "local"
            bucket = tokens[provider]
            bucket["input"] += int(entry.get("input_tokens", 0) or 0)
            bucket["output"] += int(entry.get("output_tokens", 0) or 0)
            bucket["cache"] += int(entry.get("cache_read_input_tokens", 0) or 0)

    starts: dict[str, list[float]] = defaultdict(list)
    durations: dict[tuple[str, str], list[float]] = defaultdict(list)
    for _task_id, kind, at, raw in events:
        data = json.loads(raw)
        if kind not in {"stage_started", "stage_completed"}:
            continue
        stage = str(data.get("stage", ""))
        if kind == "stage_started":
            starts[stage].append(at)
        elif starts[stage]:
            started = starts[stage].pop()
            provider = str(data.get("provider", "") or "local")
            if at >= started:
                durations[(provider, stage)].append(at - started)

    stage_stats: list[dict[str, object]] = []
    for (provider, stage), values in durations.items():
        if not values:
            continue
        stage_stats.append(
            {
                "provider": provider,
                "stage": stage,
                "samples": len(values),
                "p50_seconds": round(statistics.median(values), 1),
                "max_seconds": round(max(values), 1),
            }
        )
    def order(item: dict[str, object]) -> tuple[int, str]:
        return (-int(str(item["samples"])), str(item["provider"]))

    stage_stats.sort(key=order)
    return {
        "tasks": {"total": len(tasks), "by_status": dict(by_status)},
        "tokens": {provider: bucket for provider, bucket in tokens.items()},
        "stages": stage_stats,
    }
