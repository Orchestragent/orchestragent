"""Content-free cluster observations from the existing state machine and call ledger."""

import time
from collections import Counter
from dataclasses import dataclass

from orchestragent.orch_plan import chain_depths, dependency_errors
from orchestragent.orch_planning import PlanningStore
from orchestragent.orch_state import FINISHED, OrchState
from orchestragent.orch_usage import Call, grouped, summary, unresolved
from orchestragent.orch_usage import lines as usage_lines
from orchestragent.store import Conflict


@dataclass(frozen=True)
class TaskProgress:
    id: str
    state: str
    stage: str
    generation_waits: list[str]
    integration_waits: list[str]
    interface_ready: list[str]
    remaining_chain: int
    candidate: str
    commit: str
    attempts: int
    call_ids: list[str]
    reason: str
    review: str = ""


@dataclass(frozen=True)
class ArtifactEvent:
    seq: int
    task_id: str
    kind: str
    at: float
    references: dict[str, str]


@dataclass(frozen=True)
class Progress:
    run_id: str
    status: str
    observed_at: float
    last_recorded_at: float | None
    consistency: str
    counts: dict[str, int]
    remaining_chain: int | None
    tasks: list[TaskProgress]
    calls: list[Call]
    usage: dict[str, object]
    event_counts: dict[str, int]
    artifacts: list[ArtifactEvent]
    unresolved_calls: list[str]
    graph_errors: list[str]


def snapshot(state: OrchState, run_id: str = "") -> Progress:
    if not run_id:
        # A planning interruption may have receipts but no Run yet.
        candidates = state.runs()[:1]
        try:
            latest_call_run = state.usage.latest_run()
        except Conflict:
            latest_call_run = ""
        if latest_call_run:
            receipts = state.usage.calls(latest_call_run)
            if not candidates or max(c.started_at for c in receipts) >= candidates[0].created_at:
                run_id = latest_call_run
        if not run_id:
            run_id = state.latest().id
    try:
        run = state.run(run_id)
    except KeyError:
        PlanningStore(state.store).session(run_id)  # reject misspelled IDs
        run = None
    tasks = state.tasks(run_id) if run else []
    events = state.events(run_id) if run else []
    calls = state.usage_calls(run_id)
    done = {task.id for task in tasks if task.status == "DONE"}
    unavailable = {task.id for task in tasks if task.status in {"PARKED", "REPLANNED"}}
    graph = {task.id: task.depends_on for task in tasks}
    errors = dependency_errors(graph)
    remaining = {task.id for task in tasks if task.status not in FINISHED}
    depth = (chain_depths({tid: [d for d in graph[tid] if d in remaining]
                           for tid in graph if tid in remaining}) if not errors else {})
    observations = []
    for task in tasks:
        waiting = set(task.depends_on) - done if task.status not in FINISHED else set()
        early = waiting & set(task.interface_depends_on) - unavailable
        blocked = waiting - early if not task.pending_candidate else waiting
        stage = {"RUNNING": "generating", "VERIFYING": "verifying", "MERGING": "merging",
                 "DONE": "merged", "PARKED": "parked", "REPLANNED": "replaced"}.get(task.status)
        if task.pending_review and task.status in {"RUNNING", "VERIFYING"}:
            stage = "reviewing"
        elif task.pending_review and task.status == "READY":
            stage = "review_ready"
        if stage is None:
            stage = ("candidate_wait" if task.pending_candidate and waiting
                     else "candidate_ready" if task.pending_candidate
                     else "dependency_wait" if blocked else "ready")
        observations.append(TaskProgress(
            task.id, task.status, stage, sorted(blocked), sorted(waiting), sorted(early),
            depth.get(task.id, 0), task.pending_candidate, task.commit, task.attempts,
            [call.id for call in calls if call.task_id == task.id], task.reason,
            task.pending_review,
        ))
    timestamps = [event.at for event in events]
    timestamps += [call.finished_at or call.started_at for call in calls]
    counts = dict(Counter(task.stage for task in observations))
    artifacts = [ArtifactEvent(event.seq, event.task_id, event.kind, event.at, refs)
                 for event in events
                 if (refs := {key: value for key, value in event.data.items()
                              if key in {"candidate", "source", "commit", "revision", "call_id",
                                         "review", "version"}
                              and isinstance(value, str) and value})]
    usage = {"totals": summary(calls),
             "by_purpose": {key: summary(items)
                            for key, items in grouped(calls, "purpose").items()},
             "execution_first_round": summary([c for c in calls
                                                if c.scope == "execution" and c.attempt <= 1]),
             "execution_later_rounds": summary([c for c in calls
                                                 if c.scope == "execution" and c.attempt > 1])}
    return Progress(
        run_id, run.status if run else "planning (acceptance not recorded)", time.time(),
        max(timestamps) if timestamps else None, "non-atomic observation; liveness unknown",
        counts, max(depth.values(), default=0) if not errors else None,
        observations, calls, usage, dict(Counter(event.kind for event in events)), artifacts,
        [call.id for call in calls if unresolved(call.outcome, call.usage)], errors,
    )


def lines(progress: Progress) -> list[str]:
    active = len([task for task in progress.tasks if task.stage != "replaced"])
    merged = progress.counts.get("merged", 0)
    filled = round(20 * merged / active) if active else 0
    output = [f"run {progress.run_id}  {progress.status}",
              f"[{('#' * filled).ljust(20, '.')}] merged {merged}/{active}",
              "  " + ", ".join(f"{key}={value}" for key, value in progress.counts.items()),
              f"remaining dependency chain: {progress.remaining_chain} task(s); not an ETA",
              f"observation: {progress.consistency}"]
    for task in progress.tasks:
        if task.stage in {"merged", "replaced"}:
            continue
        output.append(f"  {task.id} {task.stage} a{task.attempts} chain={task.remaining_chain} "
                      f"generate_wait={','.join(task.generation_waits) or '-'} "
                      f"merge_wait={','.join(task.integration_waits) or '-'} "
                      f"candidate={task.candidate[:12] or '-'}")
        if task.review:
            output.append(f"    review={task.review}")
        if task.reason:
            output.append(f"    {task.reason}")
    output.append("recovery: " + ", ".join(
        f"{kind}={progress.event_counts.get(kind, 0)}" for kind in (
            "task.interrupted", "task.inputs_changed", "task.candidate_reused", "task.failed")))
    if progress.unresolved_calls:
        output.append("unresolved receipts (in flight or interrupted): "
                      + ", ".join(progress.unresolved_calls))
    output += progress.graph_errors
    output += usage_lines(progress.calls, "purpose")
    output.append(f"trace calls: orch cost --run {progress.run_id} --calls; "
                  "settled receipts do not prove acceptance")
    return output
