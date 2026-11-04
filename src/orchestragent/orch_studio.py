"""`orch studio` over a real repository (contract: docs/STUDIO.md).

Reads go to the repository's orchestration state (read-only views of tasks, events and
receipts, plus git for file contents); every change goes through OrchState's own guarded
operations (message, set_route, retry), and work is done by `orch run` processes started
exactly like MCP jobs. The studio never writes a task's code itself.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestragent import orch_models, orch_verify
from orchestragent.orch_state import FINISHED, IN_FLIGHT, OrchEvent, OrchState, OrchTask, Run
from orchestragent.orch_studio_server import StudioError
from orchestragent.store import Conflict
from orchestragent.workspace import Workspace

RUNGS = {0: "first try", 1: "retry", 2: "samples", 3: "Claude patch", 4: "thicken",
         5: "replan"}
VENDORS = {"claude": "anthropic", "dsh": "deepseek", "codex": "openai", "cursor": "cursor",
           "human": "person"}
NAMES = {"claude-opus-5-5": "Claude Opus 5.5", "claude-sonnet-5-5": "Claude Sonnet 5.5",
         "claude-haiku-5-5": "Claude Haiku 5.5", "deepseek-flash": "DeepSeek V4.1 Flash",
         "gpt-6-astra": "GPT-6 Astra", "composer-2.5": "Cursor Composer 2.5", "person": "a person"}
# What each subscription or key can build with (DeepSeek V4 Pro is never offered).
OFFERED: dict[str, list[str]] = {
    "claude": [f"claude/claude-opus-5-5:{e}" for e in orch_models.CLAUDE_EFFORTS]
    + [f"claude/claude-sonnet-5-5:{e}" for e in orch_models.CLAUDE_EFFORTS],
    "dsh": [f"dsh/deepseek-flash:{e}" for e in orch_models.DSF_EFFORTS],
    "codex": [f"codex/gpt-6-astra:{e}" for e in ("low", "medium", "high", "xhigh")],
    "cursor": ["cursor/composer-2.5"],
}
LOGINS = {"claude": {"command": "claude", "then": "/login"},
          "codex": {"command": "codex login", "then": ""},
          "cursor": {"command": "cursor-agent login", "then": ""}}
TITLES = {"claude": "Claude (Claude Code subscription)", "dsh": "DeepSeek (API key)",
          "codex": "Codex (ChatGPT subscription)", "cursor": "Cursor (subscription)"}
NOT_RUNNABLE = re.compile(r"^(FAIL \(exit 12[67]\)|TIMEOUT): ", re.MULTILINE)
RIPPLE_SHOWN = 120.0  # seconds a ripple marker stays on a node
FILE_LIMIT = 200_000


def vendor_of(provider: str, hint: str = "") -> str:
    if provider in VENDORS:
        return VENDORS[provider]
    text = f"{provider} {hint}".lower()
    for word, vendor in (("anthropic", "anthropic"), ("deepseek", "deepseek"),
                         ("openai", "openai"), ("gemini", "google"), ("google", "google"),
                         ("moonshot", "moonshot"), ("kimi", "moonshot"), ("qwen", "qwen"),
                         ("dashscope", "qwen")):
        if word in text:
            return vendor
    return "other"


def route_view(label: str, *, override: bool = False) -> dict[str, Any]:
    provider, _, rest = label.partition("/")
    model, _, effort = rest.rpartition(":") if ":" in rest else (rest, "", "")
    name = NAMES.get(model, model) + (f" · {effort}" if effort else "")
    view: dict[str, Any] = {"label": label, "provider": provider, "model": model,
                            "effort": effort, "vendor": vendor_of(provider, model), "name": name,
                            "verified": label in set(orch_models.VERIFIED.values())}
    if override:
        view["override"] = True
    return view


def attempt_of(span: str) -> int:
    match = re.search(r"/a(\d+)$", span)
    return int(match.group(1)) if match else 0


def money(calls: list[Any]) -> dict[str, float]:
    usd = cny = 0.0
    for call in calls:
        usage = call.usage or {}
        for key in ("provider_estimate_usd", "cost_usd"):
            if isinstance(usage.get(key), (int, float)):
                usd += float(usage[key])
                break
        if isinstance(usage.get("settled_cents"), (int, float)):
            cny += float(usage["settled_cents"]) / 100
    return {"usd": round(usd, 4), "cny": round(cny, 4)}


def title_of(spec: str) -> str:
    """The first line a person wrote (headings unmarked; HTML comments skipped)."""
    for line in re.sub(r"<!--.*?-->", "", spec, flags=re.DOTALL).splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:120]
    return "untitled run"


def rung_of(data: dict[str, object]) -> str:
    purpose = str(data.get("purpose", ""))
    if purpose in {"thicken", "replan", "review"}:
        return {"review": "test review"}.get(purpose, purpose)
    level = data.get("level", 0)
    return RUNGS.get(level if isinstance(level, int) else 0, "replan")


def diagnose(task: OrchTask, tasks: dict[str, OrchTask], events: list[OrchEvent],
             owners: dict[str, str], check_owner: dict[str, str]) -> dict[str, Any]:
    """Why a node is where it is, from its state and its last failure (language-neutral:
    only exit codes, check ids and file paths are read)."""
    def result(kind: str, summary: str, blamed: list[str] | None = None,
               checks: list[str] | None = None) -> dict[str, Any]:
        return {"kind": kind, "summary": summary, "blamed": blamed or [], "checks": checks or []}

    if task.status == "DONE":
        return result("done", "merged; its checks pass on the integration branch")
    if task.status == "REPLANNED":
        return result("replanned", "replaced by new nodes after a replan")
    if task.status in IN_FLIGHT:
        return result("running", {"RUNNING": "building", "VERIFYING": "running its frozen "
                                  "checks", "MERGING": "merging; every merged node's checks run"
                                  }[task.status])
    waiting = [dep for dep in task.depends_on if dep in tasks
               and tasks[dep].status != "DONE"]
    if task.status == "PLANNED" and waiting:
        return result("waiting", "waiting on " + ", ".join(waiting), waiting)
    failure = next((event for event in reversed(events)
                    if event.kind in {"task.failed", "task.parked"}), None)
    if task.status == "PLANNED" or (task.status == "READY"
                                    and (failure is None or not task.last_error)):
        return result("queued", "ready; waiting for a worker slot")
    data = failure.data if failure is not None else {}
    report = str(data.get("report") or task.last_error or "")
    category = str(data.get("category") or "")
    reason = task.reason or str(data.get("reason") or "")
    prefix = "retrying — " if task.status == "READY" else ""
    all_checks = list(check_owner)
    failing = orch_verify.failed(report, all_checks)
    if category == "dispute" or "TEST_DISPUTE" in reason:
        return result("test", prefix + "the builder disputes a frozen test; a review decides",
                      checks=failing)
    if NOT_RUNNABLE.search(report):
        return result("environment", prefix + "a check could not run (tool missing, exit "
                      "126/127) or timed out", checks=failing)
    if category in {"provider", "output_limit", "context"}:
        return result("environment", prefix + f"model call problem ({category}): "
                      + (reason or report)[:160])
    foreign = sorted({check_owner[check] for check in failing
                      if check_owner.get(check) not in {None, task.id}})
    if category == "integration" and foreign:
        return result("upstream", prefix + "merging broke checks of " + ", ".join(foreign),
                      foreign, failing)
    named = sorted({owner for path, owner in owners.items()
                    if owner != task.id and path in report})
    if named:
        return result("upstream", prefix + "the failure names files owned by "
                      + ", ".join(named), named, failing)
    own = [check for check in failing if check in task.tests]
    if own:
        command = (orch_verify.parse(own[0]) or orch_verify.Check("?", ())).command
        return result("own", prefix + f"{len(own)} of its own checks fail: {command[:120]}",
                      checks=own)
    return result("own", prefix + (reason or "its last attempt failed")[:200], checks=failing)


class RepoBackend:
    demo = False

    def __init__(self, repo: Path, data_dir: Path, *, clock: Callable[[], float] = time.time,
                 launch: Callable[[Path, str, str], Path] | None = None) -> None:
        try:
            common = Workspace.git(repo, "rev-parse", "--path-format=absolute",
                                   "--git-common-dir")
        except Exception:  # noqa: BLE001 - reported as one line
            raise SystemExit(f"orch studio: {repo} is not a git repository") from None
        self.repo = repo.resolve()
        self.home = Path(common) / "orch"
        self.db = self.home / "state.sqlite"
        self.data_dir = data_dir
        self.clock = clock
        self._state: OrchState | None = None
        self._launch = launch
        self._lock = threading.Lock()
        self._providers: tuple[float, dict[str, Any]] = (0.0, {})

    # --- plumbing ---------------------------------------------------------------------
    def state(self) -> OrchState | None:
        if self._state is None and self.db.is_file():
            self._state = OrchState(self.db, clock=self.clock)
        return self._state

    def _need_state(self) -> OrchState:
        state = self.state()
        if state is None:
            raise StudioError("not_found", "no orch runs in this repository yet")
        return state

    def _run(self, run_id: str) -> Run:
        try:
            return self._need_state().run(run_id)
        except KeyError:
            raise StudioError("not_found", f"no run {run_id}") from None

    def _task(self, run_id: str, task_id: str) -> OrchTask:
        self._run(run_id)
        try:
            return self._need_state().task(run_id, task_id)
        except KeyError:
            raise StudioError("not_found", f"no node {task_id} in {run_id}") from None

    def _query(self, sql: str, *args: object) -> list[tuple[Any, ...]]:
        if not self.db.is_file():
            return []
        db = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=2)
        try:
            return db.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []
        finally:
            db.close()

    def live(self, run_id: str) -> bool:
        lock = self.home / run_id / "lock"
        if not lock.is_file():
            return False
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False

    def _jobs(self) -> list[dict[str, Any]]:
        """Studio/MCP jobs: pid, run id (once the log names it), alive."""
        folder = self.home / "mcp" / "jobs"
        found = []
        for path in folder.iterdir() if folder.is_dir() else []:
            try:
                meta = json.loads((path / "job.json").read_text())
                log = (path / "log").read_text(errors="replace")[:20000]
            except (OSError, ValueError):
                continue
            match = re.search(r"\brun (r[0-9a-f]{8})\b", log)
            alive = not (path / "exit").exists() and _alive(int(meta.get("pid", 0)))
            spec = (path / "spec.md").read_text(errors="replace") if (path / "spec.md").exists() \
                else ""
            found.append({"path": path, "pid": int(meta.get("pid", 0)), "alive": alive,
                          "run": match.group(1) if match else "", "spec": spec,
                          "started": float(meta.get("started_at", 0))})
        return found

    def _ensure_worker(self, run_id: str) -> None:
        if self.live(run_id) or any(job["alive"] and job["run"] == run_id
                                    for job in self._jobs()):
            return
        self._start_job("", run_id)

    def _start_job(self, spec: str, run_id: str) -> Path:
        if self._launch is not None:
            return self._launch(self.repo, spec, run_id)
        from orchestragent.orch_mcp import launch
        return launch(self.repo, spec, run_id)

    # --- reads --------------------------------------------------------------------------
    def boot(self) -> dict[str, Any]:
        from orchestragent.orch_cli import package_version
        return {"version": package_version(), "repo": str(self.repo),
                "repo_name": self.repo.name, "platform": sys.platform, "demo": False,
                "runs": self.runs()}

    def runs(self) -> list[dict[str, Any]]:
        state = self.state()
        listed: list[dict[str, Any]] = []
        known: set[str] = set()
        if state is not None:
            for run in state.runs():
                known.add(run.id)
                listed.append(self._run_view(state, run))
        for job in self._jobs():  # a run still being planned has no tasks yet
            if job["alive"] and job["spec"] and job["run"] not in known:
                listed.append({"id": job["run"], "title": title_of(job["spec"]),
                               "status": "planning", "live": True, "created": job["started"],
                               "tasks": 0, "done": 0, "parked": 0,
                               "cost": {"usd": 0.0, "cny": 0.0}})
        return sorted((item for item in listed if item["id"]), key=lambda r: -r["created"])

    def _run_view(self, state: OrchState, run: Run) -> dict[str, Any]:
        tasks = [t for t in state.tasks(run.id) if t.status != "REPLANNED"]
        live = self.live(run.id)
        status = run.status
        if status in {"planned", "ready"}:
            status = "running" if live else "paused"
        if status == "running" and not live:
            status = "paused"
        if live:
            status = "running"
        return {"id": run.id, "title": title_of(run.spec), "status": status, "live": live,
                "created": run.created_at, "tasks": len(tasks),
                "done": sum(t.status == "DONE" for t in tasks),
                "parked": sum(t.status == "PARKED" for t in tasks),
                "cost": money(state.usage.calls(run.id))}

    def seq(self, run: str) -> int:
        rows = self._query("SELECT COALESCE(MAX(seq), 0) FROM orch_events WHERE run_id=?", run)
        return int(rows[0][0]) + (1_000_000_000 if self.live(run) else 0) if rows else 0

    def runs_seq(self) -> int:
        rows = self._query("SELECT COUNT(*), COALESCE(MAX(seq), 0) FROM orch_events "
                           "WHERE task_id=''")
        jobs = sum(1 for job in self._jobs() if job["alive"])
        return (int(rows[0][0]) * 1009 + int(rows[0][1]) if rows else 0) * 31 + jobs

    def _context(self, run_id: str) -> tuple[Run, list[OrchTask], list[OrchEvent], list[Any]]:
        state = self._need_state()
        run = self._run(run_id)
        return run, state.tasks(run_id), state.events(run_id), state.usage.calls(run_id)

    def graph(self, run_id: str) -> dict[str, Any]:
        state = self.state()
        if state is None or run_id not in {run.id for run in state.runs()}:
            job = next((job for job in self._jobs() if job["run"] == run_id), None)
            if job is None:
                raise StudioError("not_found", f"no run {run_id}")
            head = {"id": run_id, "title": title_of(job["spec"]), "status": "planning",
                    "live": job["alive"], "created": job["started"], "tasks": 0, "done": 0,
                    "parked": 0, "cost": {"usd": 0.0, "cny": 0.0}, "started": job["started"],
                    "elapsed_s": round(self.clock() - job["started"], 1), "phase": "planning"}
            return {"run": head, "nodes": [], "edges": []}
        run, tasks, events, calls = self._context(run_id)
        by_id = {task.id: task for task in tasks}
        owners = {path: task.id for task in tasks if task.status != "REPLANNED"
                  for path in task.files}
        check_owner = {test: task.id for task in tasks for test in task.tests}
        per_task: dict[str, list[OrchEvent]] = {}
        for event in events:
            per_task.setdefault(event.task_id, []).append(event)
        nodes = [self._node_view(run, task, by_id, per_task.get(task.id, []), owners,
                                 check_owner, [c for c in calls if c.task_id == task.id])
                 for task in tasks]
        head = self._run_view(self._need_state(), run)
        live_or_now = self.clock() if head["live"] else (events[-1].at if events
                                                         else run.created_at)
        finished = all(task.status in FINISHED for task in tasks)
        phase = ("finished" if finished and not head["live"] else "final gate" if finished
                 else "building")
        head.update(started=run.created_at, elapsed_s=round(live_or_now - run.created_at, 1),
                    phase=phase)
        return {"run": head, "nodes": nodes,
                "edges": [{"from": dep, "to": task.id} for task in tasks
                          for dep in task.depends_on if dep in by_id]}

    def _builder_label(self, task: OrchTask) -> str:
        if task.level == 3:
            return orch_models.VERIFIED["planner"]  # Claude patches use the planner's route
        if task.route:
            return task.route
        builder = orch_models.choose("builder", self.data_dir)
        return str(builder)

    def _node_view(self, run: Run, task: OrchTask, tasks: dict[str, OrchTask],
                   events: list[OrchEvent], owners: dict[str, str],
                   check_owner: dict[str, str], calls: list[Any]) -> dict[str, Any]:
        changed = [event.at for event in events if "status" in event.data
                   or event.kind in {"task.done", "task.failed", "task.parked"}]
        ripples = [event for event in events if event.kind == "task.ripple"]
        ripple = ""
        if ripples and self.clock() - ripples[-1].at <= RIPPLE_SHOWN:
            ripple = "affected" if ripples[-1].data.get("affected") else "unaffected"
        elif task.reopened_by not in {"", "person"}:
            ripple = "affected"
        into = next((event.data.get("into") for event in reversed(events)
                     if event.kind == "task.replanned"), None)
        replaced = [str(item) for item in into] if isinstance(into, list) else []
        return {
            "id": task.id, "goal": task.goal, "status": task.status,
            "since": changed[-1] if changed else run.created_at, "attempts": task.attempts,
            "rung": RUNGS.get(task.level, "replan"),
            "route": route_view(self._builder_label(task), override=bool(task.route)),
            "route_override": bool(task.route), "files": task.files, "reqs": task.reqs,
            "depends_on": task.depends_on,
            "diagnosis": diagnose(task, tasks, events, owners, check_owner),
            "cost": money(calls),
            "person": {"messages": sum(e.kind in {"task.message", "task.hint"} for e in events),
                       "taken": task.status == "PARKED" and any(
                           e.kind == "task.manual_taken" for e in events)},
            "ripple": ripple, "replaced_by": replaced,
        }

    def node(self, run_id: str, task_id: str) -> dict[str, Any]:
        self._task(run_id, task_id)
        run, tasks, events, calls = self._context(run_id)
        by_id = {task.id: task for task in tasks}
        task = by_id[task_id]
        own = [event for event in events if event.task_id == task_id]
        owners = {path: t.id for t in tasks if t.status != "REPLANNED" for path in t.files}
        check_owner = {test: t.id for t in tasks for test in t.tests}
        files_raw = run.plan.get("files", [])
        reqs_raw = run.plan.get("requirements", [])
        plan_files = {str(f["path"]): str(f["content"])
                      for f in (files_raw if isinstance(files_raw, list) else [])
                      if isinstance(f, dict)}
        texts = {str(r.get("id")): str(r.get("text", ""))
                 for r in (reqs_raw if isinstance(reqs_raw, list) else [])
                 if isinstance(r, dict)}
        checks = []
        for test in task.tests:
            check = orch_verify.parse(test)
            if check is not None:
                checks.append({"id": test, "command": check.command,
                               "files": [{"path": p, "content": plan_files.get(p, "")[:FILE_LIMIT]}
                                         for p in check.files]})
        return {
            "node": self._node_view(run, task, by_id, own, owners, check_owner,
                                    [c for c in calls if c.task_id == task_id]),
            "contract": task.contract,
            "requirements": [{"id": rid, "text": texts.get(rid, "")} for rid in task.reqs],
            "files": self._files(run, task, plan_files),
            "checks": checks,
            "attempts": self._attempts(task, own),
            "thread": self._thread(own),
            "routes": self._routes(task),
        }

    def _show(self, revision: str, path: str) -> str | None:
        try:
            return Workspace.git(self.repo, "show", f"{revision}:{path}", strip=False)[:FILE_LIMIT]
        except Exception:  # noqa: BLE001 - missing at that revision
            return None

    def _files(self, run: Run, task: OrchTask, stubs: dict[str, str]) -> list[dict[str, str]]:
        out = []
        for path in task.files:
            content, state = None, "stub"
            if task.status == "DONE" and (task.commit or run.verified_head):
                content, state = self._show(task.commit or run.verified_head, path), "built"
            elif task.pending_candidate or task.failed_candidate:
                content = self._show(task.pending_candidate or task.failed_candidate, path)
                state = "candidate"
            if content is None:
                content, state = stubs.get(path, ""), "stub"
            out.append({"path": path, "content": content, "state": state})
        return out

    def _attempts(self, task: OrchTask, events: list[OrchEvent]) -> list[dict[str, Any]]:
        attempts: dict[int, dict[str, Any]] = {}
        for event in events:
            n = attempt_of(event.span)
            if not n:
                continue
            if event.kind == "attempt.called" and n not in attempts:
                label = str(event.data.get("route", ""))
                attempts[n] = {"n": n, "label": f"a{n}", "route": route_view(
                    self._label_of(label, event.data)), "rung": rung_of(event.data),
                    "started": event.at, "ended": None, "outcome": "running",
                    "category": "", "report": "", "diff": "", "summary": ""}
            item = attempts.get(n)
            if item is None:
                continue
            if event.kind == "attempt.called" and event.data.get("purpose"):
                item["rung"] = rung_of(event.data)  # the attempt went on to thicken/replan
            elif event.kind == "task.replanned":
                into = event.data.get("into")
                item.update(ended=event.at, outcome="replanned", category="replan",
                            report="replaced by " + ", ".join(map(str, into))
                            if isinstance(into, list) else "replaced by new nodes")
            elif event.kind == "attempt.summary":
                item["summary"] = str(event.data.get("summary", ""))
            elif event.kind in {"task.failed", "task.parked"}:
                item.update(ended=event.at, outcome="failed",
                            category=str(event.data.get("category", "")),
                            report=str(event.data.get("report") or event.data.get("reason", "")),
                            diff=str(event.data.get("diff", "")))
            elif event.kind == "task.done":
                item.update(ended=event.at, outcome="passed")
                commit = str(event.data.get("commit", ""))
                if commit:
                    item["diff"] = self._diff(commit, task.files)
            elif event.kind == "task.deferred":
                item.update(ended=event.at, outcome="error",
                            report=str(event.data.get("reason", "")))
        for n, item in attempts.items():
            if item["outcome"] == "running" and not (n == task.attempts
                                                     and task.status in IN_FLIGHT):
                item["outcome"] = "error"
        return [attempts[n] for n in sorted(attempts)]

    @staticmethod
    def _label_of(label: str, data: dict[str, object]) -> str:
        """attempt.called carries `provider/model`; add the effort when it is known."""
        if ":" in label.partition("/")[2]:
            return label
        if label.startswith("dsh/"):
            return label + ":" + str(orch_models.choose("builder")).rpartition(":")[2] \
                if ":" in str(orch_models.choose("builder")) else label
        return label

    def _diff(self, commit: str, files: list[str]) -> str:
        try:
            return Workspace.git(self.repo, "diff", f"{commit}^", commit, "--", *files,
                                 strip=False)[-8000:]
        except Exception:  # noqa: BLE001
            return ""

    def _thread(self, events: list[OrchEvent]) -> list[dict[str, Any]]:
        thread: list[dict[str, Any]] = []
        started: set[int] = set()

        def add(event: OrchEvent, author: str, kind: str, text: str) -> None:
            item: dict[str, Any] = {"id": f"e{event.seq}", "at": event.at, "author": author,
                                    "kind": kind, "text": text}
            if attempt_of(event.span):
                item["attempt"] = attempt_of(event.span)
            thread.append(item)

        for event in events:
            data, n = event.data, attempt_of(event.span)
            if event.kind == "task.message":
                add(event, "you", "message", str(data.get("text", "")))
            elif event.kind == "task.hint":
                add(event, "you", "message", str(data.get("hint", "")))
            elif event.kind == "attempt.called" and n and n not in started:
                started.add(n)
                add(event, "orch", "started", f"attempt a{n} → {data.get('route', '')} "
                    f"({rung_of(data)})")
            elif event.kind == "attempt.summary":
                add(event, "agent", "summary", str(data.get("summary", "")))
            elif event.kind == "task.failed":
                add(event, "orch", "failed", f"a{n} failed ({data.get('category', '')})")
            elif event.kind == "task.parked":
                add(event, "orch", "parked", "Parked: " + str(data.get("reason", ""))[:300])
            elif event.kind == "task.done":
                add(event, "orch", "merged", f"a{n} passed its frozen checks and merged")
            elif event.kind == "task.ripple":
                add(event, "orch", "ripple",
                    f"Upstream {data.get('from')} changed: "
                    + ("this node's checks now fail; reopened to rebuild"
                       if data.get("affected") else "its checks still pass, nothing to do"))
            elif event.kind == "task.route":
                route = str(data.get("route", ""))
                add(event, "orch", "route", f"next attempts use {route}" if route
                    else "back to the role's model")
            elif event.kind == "task.manual_taken":
                add(event, "orch", "note", "taken over by a person (orch take)")
            elif event.kind == "task.retry":
                add(event, "orch", "note", "retry requested: fresh ladder")
            elif event.kind == "task.replanned":
                into = data.get("into")
                add(event, "orch", "note", "replanned: replaced by " + (
                    ", ".join(map(str, into)) if isinstance(into, list) else "new nodes"))
        return thread

    # --- routes and settings -------------------------------------------------------------
    def _provider_status(self) -> dict[str, Any]:
        with self._lock:
            at, cached = self._providers
            if self.clock() - at < 15 and cached:
                return cached
        try:
            from orchestragent.orch_cli import providers
            clients = providers(self.data_dir)
            try:
                status = clients.status()
            finally:
                clients.close()
        except Exception as error:  # noqa: BLE001 - shown in settings, never fatal
            status = {"error": {"ready": False, "message": str(error)[:200]}}
        with self._lock:
            self._providers = (self.clock(), status)
        return status

    def _registered(self) -> dict[str, Any]:
        try:
            from orchestragent import byok
            return dict(byok.registry(self.data_dir))
        except Exception:  # noqa: BLE001
            return {}

    def configured_routes(self) -> list[str]:
        status = self._provider_status()
        labels = []
        for provider, offered in OFFERED.items():
            if isinstance(status.get(provider), dict) and status[provider].get("ready"):
                labels += offered
        for name, entry in self._registered().items():
            model = str(getattr(entry, "model", "") or (entry.get("model", "")
                                                         if isinstance(entry, dict) else ""))
            if model:
                labels.append(f"{name}/{model}")
        return labels

    def _routes(self, task: OrchTask) -> list[dict[str, Any]]:
        labels = self.configured_routes()
        current = self._builder_label(task)
        if current not in labels:
            labels.insert(0, current)
        return [route_view(label) for label in labels]

    def settings(self) -> dict[str, Any]:
        status = self._provider_status()
        providers = []
        for name in ("claude", "dsh", "codex", "cursor"):
            item = status.get(name, {}) if isinstance(status.get(name), dict) else {}
            ready = bool(item.get("ready"))
            kind = "key" if name == "dsh" else "subscription"
            detail = ("ready" if ready else "CLI not installed" if item.get("installed") is False
                      else "no key" if kind == "key" else "not logged in")
            providers.append({"name": name, "vendor": vendor_of(name), "title": TITLES[name],
                              "kind": kind, "configured": ready, "detail": detail,
                              "login": LOGINS.get(name)})
        for name, entry in self._registered().items():
            url = str(getattr(entry, "base_url", "") or "")
            try:
                from orchestragent.access import Access
                has_key = bool(Access(self.data_dir).load_secret(name))
            except Exception:  # noqa: BLE001
                has_key = False
            providers.append({"name": name, "vendor": vendor_of(name, url),
                              "title": f"{name} ({url or 'OpenAI-compatible'})", "kind": "key",
                              "configured": has_key, "detail": "key saved" if has_key
                              else "no key", "login": None})
        effective = orch_models.effective(self.data_dir)
        roles = []
        for role in orch_models.ROLES:
            value = effective.get(role, ("", ""))[0]
            default = orch_models.VERIFIED[role]
            roles.append({"role": role, "title": role.replace("_", " "),
                          "description": orch_models.DESCRIPTIONS[role],
                          "route": route_view(value) if "/" in value else None,
                          "default": route_view(default) if "/" in default else None,
                          "value": value})
        return {"providers": providers, "roles": roles,
                "routes": [route_view(label) for label in self.configured_routes()]}

    # --- changes --------------------------------------------------------------------------
    def message(self, run_id: str, task_id: str, text: str, rework: bool) -> dict[str, Any]:
        self._task(run_id, task_id)
        try:
            task = self._need_state().message(run_id, task_id, text, rework=rework)
        except Conflict as error:
            raise StudioError("conflict", str(error)) from None
        if task.status == "READY":
            self._ensure_worker(run_id)
        return self._fresh_node(run_id, task_id)

    def route(self, run_id: str, task_id: str, label: str) -> dict[str, Any]:
        task = self._task(run_id, task_id)
        if label and label not in self.configured_routes() and label != task.route:
            raise StudioError("invalid", f"{label} is not a configured model; add its key or "
                              "log in under Settings")
        if label:
            try:
                orch_models.parse("builder", label, self.data_dir)
            except ValueError as error:
                raise StudioError("invalid", str(error)) from None
        try:
            self._need_state().set_route(run_id, task_id, label)
        except Conflict as error:
            raise StudioError("conflict", str(error)) from None
        return self._fresh_node(run_id, task_id)

    def retry(self, run_id: str, task_id: str) -> dict[str, Any]:
        self._task(run_id, task_id)
        try:
            self._need_state().retry(run_id, task_id)
        except Conflict as error:
            raise StudioError("conflict", str(error)) from None
        self._ensure_worker(run_id)
        return self._fresh_node(run_id, task_id)

    def _fresh_node(self, run_id: str, task_id: str) -> dict[str, Any]:
        return dict(self.node(run_id, task_id)["node"])

    def start(self, spec: str) -> dict[str, Any]:
        if len(spec.strip()) < 20:
            raise StudioError("bad_request", "write the complete numbered requirements "
                              "(R1. …, R2. …)")
        path = self._start_job(spec, "")
        deadline = self.clock() + 30
        while self.clock() < deadline:
            log = (path / "log").read_text(errors="replace") if (path / "log").exists() else ""
            match = re.search(r"\brun (r[0-9a-f]{8})\b", log)
            if match:
                return {"id": match.group(1), "title": title_of(spec), "status": "planning",
                        "live": True, "created": self.clock(), "tasks": 0, "done": 0,
                        "parked": 0, "cost": {"usd": 0.0, "cny": 0.0}}
            if (path / "exit").exists():
                raise StudioError("internal", "planning stopped: " + log[-400:])
            time.sleep(0.2)
        raise StudioError("internal", "planning did not report a run id within 30 s")

    def resume(self, run_id: str) -> dict[str, Any]:
        run = self._run(run_id)
        self._ensure_worker(run_id)
        return self._run_view(self._need_state(), run)

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self._run(run_id)
        jobs = [job for job in self._jobs() if job["alive"] and job["run"] == run_id]
        if not jobs and self.live(run_id):
            raise StudioError("conflict", "this run is executing in another terminal; stop it "
                              "there (Ctrl-C)")
        for job in jobs:
            try:
                os.killpg(job["pid"], signal.SIGINT)
            except ProcessLookupError:
                pass
        return self._run_view(self._need_state(), run)

    def set_key(self, provider: str, key: str) -> dict[str, Any]:
        key = key.strip()
        if not key:
            raise StudioError("bad_request", "the key is empty")
        from orchestragent.access import Access
        access = Access(self.data_dir)
        if provider == "dsh":
            operation = access.operation(provider, 0, "balance")
            access.authorize(provider, 0, "balance", str(operation["operation_digest"]), key)
        elif provider in self._registered():
            access.save_secret(provider, key)
        else:
            raise StudioError("invalid", f"{provider} does not take a key; log in instead")
        with self._lock:
            self._providers = (0.0, {})
        return next(item for item in self.settings()["providers"] if item["name"] == provider)

    def set_role(self, role: str, label: str) -> dict[str, Any]:
        try:
            if label:
                orch_models.set_role(role, label, self.data_dir)
            else:
                orch_models.reset(role, self.data_dir)
        except ValueError as error:
            raise StudioError("invalid", str(error)) from None
        return {}


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
