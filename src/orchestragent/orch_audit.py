"""Deterministic audit of a run: the invariants every finished run must satisfy.

Used by `orch audit`, the scale tests and the scale benchmark. Each finding is one
line naming what is wrong; an empty list means the run's history, git state and (by
default) its merged tests all agree.
"""

from typing import TYPE_CHECKING

from orchestragent.orch_build import REQUIRED_TRAILERS, parse_trailers
from orchestragent.orch_state import FINISHED, TRANSITIONS

if TYPE_CHECKING:
    from orchestragent.orch_run import Orchestrator


def _replayed(kind: str, data: dict[str, object], current: str) -> str:
    """The task status an event leaves behind (events record every change)."""
    if "status" in data and kind.startswith("task."):
        return str(data["status"])
    return {"task.planned": "PLANNED", "task.interrupted": "READY",
            "task.replanned": "REPLANNED"}.get(kind, current)


def audit(orchestrator: "Orchestrator", run_id: str, run_tests: bool = True) -> list[str]:
    state = orchestrator.state
    run = state.run(run_id)
    tasks = {task.id: task for task in state.tasks(run_id)}
    events = state.events(run_id)
    findings: list[str] = []

    spans = {event.span for event in events}
    findings += [f"event {e.seq} {e.kind}: parent span {e.parent} missing"
                 for e in events if e.parent and e.parent not in spans]
    replay: dict[str, str] = {}
    for event in events:
        if not event.task_id:
            continue
        before = replay.get(event.task_id, "")
        after = _replayed(event.kind, event.data, before)
        if before and after != before and after not in TRANSITIONS[before]:
            findings.append(f"{event.task_id}: illegal {before} -> {after} at event {event.seq}")
        replay[event.task_id] = after
    findings += [f"{tid}: log says {replay.get(tid)}, state says {task.status}"
                 for tid, task in tasks.items() if replay.get(tid) != task.status]

    integration = orchestrator.integration(run_id)
    head = orchestrator.git(integration, "rev-parse", "HEAD")
    branch = orchestrator.git(orchestrator.repo, "rev-parse", run.branch)
    if head != branch or (run.verified_head and run.verified_head != head):
        findings.append(f"integration {head[:10]}, branch {branch[:10]}, verified "
                        f"{run.verified_head[:10]} disagree")
    if orchestrator.git(integration, "status", "--porcelain", "--untracked-files=no"):
        findings.append("integration worktree has uncommitted changes")
    if run.status == "done":
        if not all(task.status in FINISHED for task in tasks.values()):
            findings.append("run is done but some tasks are not")
        tag = orchestrator.git(orchestrator.repo, "rev-parse", f"orch/{run_id}/done")
        if tag != head:
            findings.append(f"done tag {tag[:10]} is not the integration HEAD {head[:10]}")
    history = set(orchestrator.git(orchestrator.repo, "rev-list", head).splitlines())
    for task in tasks.values():
        if task.status != "DONE":
            continue
        if task.commit not in history:
            findings.append(f"{task.id}: merge commit {task.commit[:10]} not on the branch")
            continue
        trailers = parse_trailers(
            orchestrator.git(orchestrator.repo, "show", "-s", "--format=%B", task.commit)
        )
        missing = [key for key in REQUIRED_TRAILERS if key not in trailers]
        if missing or trailers.get("Orch-Task") != task.id or trailers.get("Orch-Run") != run_id:
            findings.append(f"{task.id}: commit trailers wrong or missing {missing}")
    listed = orchestrator.git(orchestrator.repo, "worktree", "list", "--porcelain")
    stale = [line for line in listed.splitlines()
             if line.startswith("worktree ") and f"/{run_id}/work/" in line]
    findings += [f"leftover worktree {line.split(' ', 1)[1]}" for line in stale]
    merged = [test for task in tasks.values() if task.status == "DONE" for test in task.tests]
    if run_tests and (merged or run.baseline):
        with orchestrator.worktree(run_id, "audit", head) as tree:
            passed, report = orchestrator.verify(
                tree, orchestrator.scratch(run_id), head, [*run.baseline, *merged]
            )
        orchestrator.release_worktrees(run_id)
        if not passed:
            findings.append(f"merged tests fail at HEAD: {report.strip()[-300:]}")
    return findings
