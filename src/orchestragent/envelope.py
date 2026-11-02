"""Versioned handoff envelope: the only contract between two different vendors."""

import hashlib
import json

from orchestragent.models import Task
from orchestragent.serialization import dumps

VERSION = 1


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def build(
    task: Task,
    *,
    stage: str,
    provider: str,
    model: str,
    files: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Describe the exact revision a downstream vendor receives."""
    plan: dict[str, object] = {}
    for artifact in task.artifacts:
        if artifact.name == "plan.json":
            try:
                plan = json.loads(artifact.content)
            except ValueError:
                plan = {}
    artifacts = [
        {"path": item["path"], "sha256": digest(item["content"]), "bytes": len(item["content"])}
        for item in (files or [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    ]
    findings: list[object] = []
    for artifact in task.artifacts:
        if artifact.name == "review-findings.json":
            try:
                findings = list(json.loads(artifact.content).get("findings") or [])
            except (ValueError, AttributeError):
                findings = []
    return {
        "version": VERSION,
        "task_id": task.id,
        "project": task.project,
        "revision": task.revision,
        "stage": stage,
        "spec": {
            "goal": task.prompt,
            "implementation": plan.get("implementation", ""),
            "acceptance": plan.get("acceptance", ""),
        },
        "artifacts": artifacts,
        "findings": findings,
        "provenance": {"provider": provider, "model": model, "attempt": task.attempt},
        "budget": {"reworks": task.reworks, "max_task_cents": task.policy.get("max_task_cents", 0)},
    }


def verify(envelope: dict[str, object], files: list[dict[str, str]]) -> bool:
    """Consumers re-check every hash before trusting the handoff."""
    entries = envelope.get("artifacts")
    listed: dict[str, str] = {}
    if isinstance(entries, list):
        for item in entries:
            if isinstance(item, dict):
                listed[str(item.get("path"))] = str(item.get("sha256"))
    for item in files:
        if not isinstance(item.get("path"), str):
            return False
        if listed.get(item["path"]) != digest(str(item.get("content", ""))):
            return False
    return True


def as_text(envelope: dict[str, object]) -> str:
    return dumps(envelope)
