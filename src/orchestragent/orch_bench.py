"""Held-out, durable suite evaluation of explicit coordinator/builder strategies."""

import ast
import json
import math
import platform
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from orchestragent.codeindex import definitions
from orchestragent.envelope import digest
from orchestragent.live_workspace import LiveWorkspace
from orchestragent.native import NativeResult
from orchestragent.orch_baseline import baseline
from orchestragent.orch_policy import Policy
from orchestragent.orch_run import (
    CLAUDE_PLAN,
    DSF,
    DSF_RESERVATION,
    SONNET_PLAN,
    STOP,
    Caller,
    Orchestrator,
    PlanError,
    Runner,
)
from orchestragent.orch_synthetic import Shape, SyntheticBuilder, synthetic_plan
from orchestragent.orch_thin import shard_path
from orchestragent.orch_usage import COUNTERS, Call, Ledger, report, summary
from orchestragent.serialization import dumps
from orchestragent.store import Store
from orchestragent.workspace import Workspace

ACCEPTANCE_PACKAGE = "_orch_bench_acceptance"
TERMINAL = frozenset({"passed", "failed", "plan_rejected", "paused", "error", "interrupted"})


@dataclass(frozen=True)
class Case:
    id: str
    spec: str
    seed: dict[str, str]
    acceptance: dict[str, str]
    mock_shape: dict[str, int] | None = None


def _shape(case: Case) -> Shape:
    values = case.mock_shape or {}
    if (set(values) - {"tasks", "packages", "fan_in", "seed"}
            or any(type(value) is not int for value in values.values())):
        raise ValueError("Invalid mock_shape fields")
    shape = Shape(**values)
    if not (1 <= shape.tasks <= 999 and 1 <= shape.packages <= shape.tasks
            and 0 <= shape.fan_in <= 999 and shape.seed >= 0):
        raise ValueError("Invalid mock_shape bounds")
    return shape


def load_suite(path: Path) -> list[Case]:
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or set(value) != {"schema_version", "cases"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["cases"], list) or not value["cases"]):
        raise ValueError("Expected schema_version 1 and a nonempty cases list")
    cases: list[Case] = []
    seen: set[str] = set()
    for raw in value["cases"]:
        if (not isinstance(raw, dict)
                or set(raw) - {"id", "spec", "seed", "acceptance", "mock_shape"}
                or not {"id", "spec", "seed", "acceptance"} <= set(raw)):
            raise ValueError("Invalid case fields")
        case_id, spec = raw["id"], raw["spec"]
        if (not isinstance(case_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", case_id)
                or case_id.casefold() in seen or not isinstance(spec, str) or not spec.strip()):
            raise ValueError("Invalid or duplicate case id/spec")
        seen.add(case_id.casefold())
        for key in ("seed", "acceptance"):
            files = raw[key]
            if (not isinstance(files, dict) or (key == "acceptance" and not files)
                    or any(not isinstance(p, str) or not isinstance(c, str)
                           for p, c in files.items())):
                raise ValueError(f"Invalid {key} files")
            paths: set[str] = set()
            for name in files:
                parts = PurePosixPath(name).parts
                if (not LiveWorkspace.safe_path(name) or name.casefold() in paths
                        or parts[0].casefold() in {
                            ACCEPTANCE_PACKAGE.casefold(), f"{ACCEPTANCE_PACKAGE}.py".casefold()
                        } or (key == "acceptance" and not re.fullmatch(
                            r"test_[A-Za-z0-9_]+\.py", name))):
                    raise ValueError(f"Unsafe or duplicate {key} path: {name}")
                paths.add(name.casefold())
            if any(str(parent).casefold() in paths for name in files
                   for parent in PurePosixPath(name).parents if str(parent) != "."):
                raise ValueError("File/directory path collision")
        if "mock_shape" in raw and not isinstance(raw["mock_shape"], dict):
            raise ValueError("mock_shape must be an object")
        case = Case(**raw)
        if case.mock_shape is not None:
            _shape(case)
        cases.append(case)
    return cases


def mock_caller(case: Case, strategy: str = "opus-dsf", *, distributed: bool = False) -> Caller:
    if case.mock_shape is None:
        raise ValueError("mock_caller requires explicit mock_shape")
    plan, bodies = synthetic_plan(_shape(case))
    builder = SyntheticBuilder(plan, bodies)
    if strategy != "sonnet-dsf" and not distributed:
        return builder

    def thin(provider: str, model: str, prompt: str, schema: dict[str, object], run_id: str,
             cancelled: Callable[[], bool], *, effort: str = "") -> NativeResult:
        properties = cast(dict[str, object], schema.get("properties", {}))
        files = cast(list[dict[str, str]], plan["files"])
        usage: dict[str, object] = {
            "input_tokens": len(prompt) // 4, "output_tokens": 60,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }
        if provider == "claude" and "requirements" in properties:
            tasks_schema = cast(dict[str, Any], properties["tasks"])
            fields = tasks_schema["items"]["properties"]
            tasks = cast(list[dict[str, object]], plan["tasks"])
            data = {**plan, "files": [f for f in files
                                      if not LiveWorkspace.is_test_file(f["path"])],
                    "tasks": [{key: ([] if key == "tests" else value)
                               for key, value in task.items() if key in fields} for task in tasks]}
            return NativeResult("ok", data, usage=usage)
        if provider == "dsh" and set(properties) == {"files"}:
            usage["settled_cents"] = 1
            if distributed:
                found = re.search(r"T-[0-9]{3}", run_id)
                assert found is not None
                task = next(t for t in cast(list[dict[str, Any]], plan["tasks"])
                            if t["id"] == found.group())
                module, cls, _ = task["tests"][0].rsplit(".", 2)
                source = next(f["content"] for f in files
                              if f["path"] == module.replace(".", "/") + ".py")
                tree = ast.parse(source)
                node = next(node for name, node, _ in definitions(tree) if name == cls)
                owned = task["files"][0][:-3].replace("/", ".")
                imports = [ast.get_source_segment(source, node) or ""
                           for node in tree.body
                           if isinstance(node, ast.ImportFrom) and node.module == owned]
                # Fixed synthetic oracle, including the extra shard; never quality evidence.
                content = "import unittest\n" + "\n".join(imports) + "\n" + (
                    ast.get_source_segment(source, node) or "")
                name = shard_path(task["id"], integration="-integration-test-design-" in run_id)
                return NativeResult("ok", {"files": [{"path": name, "content": content}]},
                                    usage=usage)
            return NativeResult("ok", {"files": [f for f in files
                                                  if LiveWorkspace.is_test_file(f["path"])]},
                                usage=usage)
        return builder(provider, model, prompt, schema, run_id, cancelled, effort=effort)

    return thin


def _save(store: Store, trial: dict[str, Any]) -> None:
    with store.transaction() as db:
        db.execute("UPDATE bench_trials SET body=? WHERE id=?", (dumps(trial), trial["id"]))


class _Admission:
    """One suite's dispatch slots and conservative, in-flight DSF reservations."""

    def __init__(self, max_calls: int, max_cents: int, *,
                 deadline: float | None = None) -> None:
        self.max_calls, self.max_cents = max_calls, max_cents
        self.deadline = deadline
        self.calls = self.cents = self.inflight = 0
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)

    def wrap(self, caller: Caller, orchestrator: Orchestrator,
             store: Store, trial: dict[str, Any]) -> Caller:
        def call(provider: str, model: str, prompt: str, schema: dict[str, object],
                 run_id: str, cancelled: Callable[[], bool], *, effort: str = ""
                 ) -> NativeResult:
            original_cancelled = cancelled

            def stopped() -> bool:
                return (original_cancelled()
                        or self.deadline is not None and time.monotonic() >= self.deadline)

            cancelled = stopped
            # The scheduler has already durably inserted this receipt before entry.
            receipts = orchestrator.state.usage.calls()
            receipt = next(item for item in receipts if item.id == run_id)
            trial["run_id"] = receipt.run_id
            _save(store, trial)
            reservation = DSF_RESERVATION if provider == "dsh" else 0
            with self.condition:
                while True:
                    if cancelled():
                        return NativeResult("cancelled", {}, message="suite admission cancelled",
                                            usage={**dict.fromkeys(COUNTERS, 0), "rejected": 1,
                                                   "cost_usd": 0, "provider_estimate_usd": 0})
                    reason = ""
                    wait_for_release = False
                    if self.calls >= self.max_calls:
                        reason = "suite max_calls reached"
                    elif self.max_cents and self.cents + reservation > self.max_cents:
                        reason = "suite max_cents reservation blocked"
                        wait_for_release = self.inflight > 0
                    if not reason:
                        self.calls += 1
                        self.cents += reservation
                        self.inflight += reservation
                        break
                    if not wait_for_release:
                        return NativeResult("policy_blocked", {}, message=reason,
                                            usage={**dict.fromkeys(COUNTERS, 0), "rejected": 1,
                                                   "cost_usd": 0, "provider_estimate_usd": 0})
                    # Periodically recheck cancellation while pending calls finish.
                    self.condition.wait(timeout=0.1)
            try:
                result = caller(provider, model, prompt, schema, run_id, cancelled, effort=effort)
                settled = result.usage.get("settled_cents")
                if result.usage.get("forwarded") == 0 or "rejected" in result.usage:
                    settled = 0
                with self.condition:
                    if reservation and type(settled) is int and settled >= 0:
                        self.cents += settled - reservation
                return result
            finally:
                with self.condition:
                    self.inflight -= reservation
                    self.condition.notify_all()
        return call


def _accept(orchestrator: Orchestrator, trial: dict[str, Any], case: Case,
            runner: Runner) -> tuple[bool, str]:
    revision, run_id = trial["result_revision"], trial["run_id"]
    with orchestrator.worktree(run_id, "acceptance", revision) as tree:
        # Never reuse a generated package or follow a generated symlink.
        if any(path.name.casefold() in {ACCEPTANCE_PACKAGE.casefold(),
                                       f"{ACCEPTANCE_PACKAGE}.py".casefold()}
               for path in tree.iterdir()):
            raise ValueError("Generated code collides with reserved acceptance namespace")
        package = tree / ACCEPTANCE_PACKAGE
        package.mkdir()
        (package / "__init__.py").write_text("")
        for name, content in case.acceptance.items():
            target = package / name
            if target.is_symlink() or not target.resolve().is_relative_to(tree.resolve()):
                raise ValueError("Acceptance path escapes disposable worktree")
            target.write_text(content)
        names = [f"{ACCEPTANCE_PACKAGE}.{Path(name).stem}" for name in case.acceptance]
        return runner(tree, orchestrator.scratch(run_id), revision, names)


def run_suite(suite: Path, output: Path, caller_factory: Callable[[Case], Caller], runner: Runner,
              *, mode: str, max_calls: int, jobs: int = 2, repeats: int = 1,
              max_cents: int = 0,
              policy: Policy | None = None, strategy: str = "opus-dsf",
              baseline_attempts: int = 2, provider_timeout: float | None = None,
              transport: str = "", context_mode: str = "full",
              context_bytes: int = 16000, distributed: bool = False) -> dict[str, object]:
    cases = load_suite(suite)
    if (mode not in {"mock", "real"} or strategy not in {
            "opus-dsf", "sonnet-dsf", "single-opus", "single-sonnet", "single-dsf"}
            or context_mode not in {"full", "scoped"}
            or (context_mode != "full" and not strategy.startswith("single-"))
            or type(context_bytes) is not int or not 2048 <= context_bytes <= 32768
            or (policy is not None and strategy != "opus-dsf")
            or (distributed and strategy.startswith("single-"))
            or (policy is not None and policy.tuning is not None and policy.tuning.jobs > jobs)
            or type(baseline_attempts) is not int or not 1 <= baseline_attempts <= 10
            or transport not in {"", "cli", "api"}
            or (provider_timeout is not None and (type(provider_timeout) not in {int, float}
                or not math.isfinite(provider_timeout) or provider_timeout < 0))
            or any(type(v) is not int or v < 1
                                         for v in (max_calls, jobs, repeats))
            or type(max_cents) is not int or max_cents < 0
            or not callable(caller_factory) or not callable(runner)):
        raise ValueError("Invalid benchmark configuration")
    single = strategy.startswith("single-")
    coordinator = (DSF if strategy == "single-dsf" else
                   SONNET_PLAN if "sonnet" in strategy else CLAUDE_PLAN)
    snapshot = {"schema_version": 1, "cases": [
        {key: value for key, value in asdict(case).items() if value is not None} for case in cases
    ]}
    output = output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    store = Store(output / "bench.sqlite")
    source_root = Path(__file__).parent
    source_hash = digest(dumps({path.relative_to(source_root).as_posix(): digest(path.read_text())
                               for path in sorted(source_root.rglob("*.py"))}))
    metadata = {"schema_version": 1, "mode": mode, "synthetic": mode == "mock",
                "policy_id": policy.id if policy else "",
                "policy_snapshot": asdict(policy) if policy else None,
                "case_kind": "synthetic" if all(case.mock_shape is not None for case in cases)
                else "curated",
                "suite_hash": digest(dumps(snapshot)),
                "expected_trials": len(cases) * repeats,
                "suite_snapshot": snapshot,
                "config": {"jobs": jobs, "repeats": repeats, "max_calls": max_calls,
                           "max_cents": max_cents, "strategy": strategy,
                           "distributed": distributed,
                           "baseline_attempts": baseline_attempts,
                           "context_mode": context_mode,
                           "context_bytes": context_bytes if context_mode == "scoped" else None,
                           "provider_base_timeout_seconds": provider_timeout,
                           "dsf_transport": transport,
                           "planner": asdict(coordinator) if not single else None,
                           "builder": asdict(DSF) if not single else None,
                           "test_designer": asdict(DSF) if strategy == "sonnet-dsf" else None,
                           "single_model": asdict(coordinator) if single else None,
                           "python": sys.version,
                           "platform": platform.platform(), "engine_source_hash": source_hash}}
    with store.transaction() as db:
        # executescript would implicitly COMMIT, exposing a partially queued suite.
        db.execute("CREATE TABLE bench_meta (body TEXT NOT NULL)")
        db.execute("CREATE TABLE bench_trials (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        db.execute("INSERT INTO bench_meta VALUES (?)", (dumps(metadata),))
        for case in cases:
            for repeat in range(1, repeats + 1):
                trial_id = f"{case.id}-{repeat}"
                trial: dict[str, Any] = {
                    "id": trial_id, "case_id": case.id, "status": "queued", "run_id": "",
                         "repo": str(output / "repos" / trial_id), "base_revision": "",
                         "result_revision": "", "input_hash": digest(dumps(
                             {"spec": case.spec, "seed": case.seed,
                              "mock_shape": case.mock_shape})),
                         "acceptance_hash": digest(dumps(case.acceptance)),
                         "elapsed_seconds": 0.0, "acceptance_passed": None,
                         "acceptance_report": ""}
                db.execute("INSERT INTO bench_trials VALUES (?,?)", (trial_id, dumps(trial)))
    admission = _Admission(max_calls, max_cents)
    stop_suite = False
    for case in cases:
        if stop_suite:
            break
        for repeat in range(1, repeats + 1):
            with store.transaction() as db:
                trial = json.loads(db.execute("SELECT body FROM bench_trials WHERE id=?",
                                              (f"{case.id}-{repeat}",)).fetchone()["body"])
            started = time.monotonic()
            orchestrator: Orchestrator | None = None
            trial["status"] = "running"
            _save(store, trial)
            stage = "repository setup"
            try:
                repo = Path(trial["repo"])
                repo.mkdir(parents=True)
                Workspace.git(repo, "init", "-b", "main")
                for name, content in case.seed.items():
                    target = repo / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content)
                Workspace.git(repo, "add", "--all")
                Workspace.git(repo, "commit", "--allow-empty", "-m", "benchmark seed")
                trial["base_revision"] = Workspace.git(repo, "rev-parse", "HEAD")
                _save(store, trial)
                stage = "caller setup"
                caller = caller_factory(case)
                if not callable(caller):
                    raise ValueError("caller_factory must return a callable")
                orchestrator = Orchestrator(repo, caller, runner, notify=lambda _: None,
                                            policy=policy,
                                            strategy="opus-dsf" if single else strategy)
                orchestrator.call = admission.wrap(caller, orchestrator, store, trial)
                if single:
                    stage = "single-model baseline"
                    run_id, revision, status = baseline(orchestrator, case.spec,
                                                       attempts=baseline_attempts,
                                                       route=coordinator,
                                                       context_mode=context_mode,
                                                       context_bytes=context_bytes)
                    trial.update(run_id=run_id, result_revision=revision)
                else:
                    stage = "planning"
                    run = orchestrator.plan(case.spec, distributed=distributed)
                    trial["run_id"] = run.id
                    _save(store, trial)
                    stage = "execution"
                    run = orchestrator.execute(
                        run.id, jobs=None if policy is not None and policy.tuning else jobs)
                    starts = [event for event in orchestrator.state.events(run.id)
                              if event.kind == "run.started"]
                    if starts:
                        trial["execution_config"] = starts[-1].data
                    trial["result_revision"] = run.verified_head
                    status = run.status
                trial["status"] = "paused" if status == "paused" else "failed"
                trial["acceptance_report"] = f"run ended {status}"
                if status == "done":
                    stage = "held-out acceptance"
                    passed, detail = _accept(orchestrator, trial, case, runner)
                    trial.update(acceptance_passed=passed, acceptance_report=detail,
                                 status="passed" if passed else "failed")
            except PlanError:
                stopped = (orchestrator is not None and any(
                    call.outcome in STOP for call in orchestrator.state.usage.calls()))
                trial.update(status="paused" if stopped else "plan_rejected",
                             acceptance_report="planner blocked" if stopped else "plan rejected")
            except Exception as error:
                trial.update(status="error",
                             acceptance_report=f"{type(error).__name__} during {stage}")
            except BaseException:
                trial["status"] = "interrupted"
                raise
            finally:
                trial["elapsed_seconds"] = time.monotonic() - started
                _save(store, trial)
                if orchestrator is not None and trial["run_id"]:
                    orchestrator.release_worktrees(trial["run_id"])
            if trial["status"] == "paused":
                stop_suite = True
                break
    return read_report(output)


def read_report(output: Path) -> dict[str, object]:
    if not (output / "bench.sqlite").is_file():
        raise ValueError("Benchmark output not found")
    store = Store(output / "bench.sqlite")
    with store.transaction() as db:
        metadata = db.execute("SELECT body FROM bench_meta").fetchone()[0]
        result: dict[str, object] = json.loads(metadata)
        trials = [json.loads(row[0])
                  for row in db.execute("SELECT body FROM bench_trials ORDER BY rowid")]
    calls: list[Call] = []
    for trial in trials:
        state = Path(trial["repo"]) / ".git" / "orch" / "state.sqlite"
        receipts = Ledger(Store(state), time.time).calls() if state.is_file() else []
        calls.extend(receipts)
        trial["usage"] = {**report(receipts, "model"), "synthetic": result["synthetic"]}
        builds = [call for call in receipts if call.purpose == "build"]
        trial["dispatches"] = {
            "planner": sum(call.scope == "planning" and call.provider == "claude"
                           for call in receipts),
            "builder": len(builds),
            "builder_retries": sum(call.attempt > 1 for call in builds),
            "opus_interventions": sum(call.provider == "claude" and call.scope != "planning"
                                      and call.purpose != "single"
                                      and (call.model.startswith("claude-opus-")
                                           or call.model == "opus")
                                      for call in receipts),
            "sonnet_interventions": sum(call.provider == "claude" and call.scope != "planning"
                                        and call.purpose != "single"
                                        and call.model.startswith("claude-sonnet-")
                                        for call in receipts),
            "test_design": sum(call.purpose == "test-design" for call in receipts),
            "single_model": sum(call.purpose == "single" for call in receipts),
            "locally_rejected": sum(call.usage.get("forwarded") == 0 for call in receipts),
        }
        if not trial["run_id"] and receipts:
            trial["run_id"] = receipts[0].run_id
    completed = sum(trial["status"] in TERMINAL for trial in trials)
    passed = sum(trial["status"] == "passed" for trial in trials)
    finished = all(trial["status"] in {"passed", "failed", "plan_rejected", "error"}
                   for trial in trials)
    result.update(completed_trials=completed, passed_trials=passed,
                  success_rate=passed / len(trials) if finished else None,
                  totals=summary(calls), trials=trials)
    api = [call for call in calls if call.provider in {"dsh", "jev"}]
    unsettled = [call for call in api if type(call.usage.get("settled_cents")) is not int
                 and call.usage.get("forwarded") != 0 and "rejected" not in call.usage]
    result["api_accounting"] = {
        "currency": "CNY", "provider_invoice": False,
        "settled_cents": sum(int(str(call.usage["settled_cents"])) for call in api
                             if type(call.usage.get("settled_cents")) is int),
        "unsettled_calls": len(unsettled),
        "known_reserved_cents": sum(int(str(call.usage["reserved_cents"])) for call in unsettled
                                    if type(call.usage.get("reserved_cents")) is int),
        "missing_reservation_calls": sum(type(call.usage.get("reserved_cents")) is not int
                                         for call in unsettled),
    }
    return result


def report_lines(report: dict[str, object]) -> list[str]:
    rate = report["success_rate"]
    heading = ("SYNTHETIC mock benchmark — NOT intelligence evidence"
               if report.get("synthetic") else "Real benchmark")
    if report.get("case_kind") == "synthetic" and not report.get("synthetic"):
        heading = "Real model calls on SYNTHETIC cases — NOT intelligence evidence"
    lines = [heading, f"Trials: {report['completed_trials']}/{report['expected_trials']} terminal; "
             f"{report['passed_trials']} passed; success_rate="
             + (f"{float(str(rate)):.1%}" if rate is not None else "incomplete")]
    for trial in cast(list[dict[str, object]], report["trials"]):
        lines.append(f"{trial['id']}: {trial['status']} run={trial['run_id'] or '-'}")
    lines.append("Usage: " + dumps(report["totals"]))
    return lines
