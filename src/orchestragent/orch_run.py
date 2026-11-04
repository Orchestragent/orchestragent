"""Deterministic scheduler: plan checkpoint, parallel DAG execution, verify, merge.

LLMs only answer leaf calls (Claude plans and handles exceptions, DSF builds). Every
decision in between is code, and a task is done only when its frozen tests pass on
the integration branch. The failure ladder lives in orch_ladder.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

from orchestragent import orch_generic, orch_verify
from orchestragent.dsh import EXTENDED, DshClient, json_failure
from orchestragent.envelope import digest
from orchestragent.native import ContextPrompt, NativeResult
from orchestragent.orch_build import (
    EDIT_SCHEMA,
    PATCH_SCHEMA,
    REQUIRED_TRAILERS,
    ContextTooLarge,
    EditError,
    Pack,
    apply_edits,
    commit_message,
    context_pack,
    contract_hash,
    parse_trailers,
    signature,
)
from orchestragent.orch_journal import CallJournal
from orchestragent.orch_models import Choice as ModelChoice
from orchestragent.orch_models import choose as choose_model
from orchestragent.orch_pace import TokenBucket
from orchestragent.orch_plan import (
    PLAN_SCHEMA,
    chain_depths,
    closure,
    dependency_errors,
    plan_files,
    tasks_from,
)
from orchestragent.orch_planning import PlanningSession, PlanningStore
from orchestragent.orch_repo import MAX_REQUESTS, Repo, mentioned_paths
from orchestragent.orch_state import FINISHED, IN_FLIGHT, OrchState, OrchTask, Run, plan_hash
from orchestragent.orch_supervisor import (
    MAX_INVESTIGATIONS,
    MAX_SUPERVISIONS,
    SUPERVISOR_SCHEMA,
    next_task_id,
    subtree,
    supervise_prompt,
)
from orchestragent.orch_thin import (
    BOUNDARY_REVIEW_SCHEMA,
    LOOK_SCHEMA,
    RED_JUDGE_SCHEMA,
    BoundaryStop,
    boundary_fingerprint,
    boundary_prompt,
    boundary_review_prompt,
    canonical_boundary,
    component_test_prompt,
    fill_requirements,
    integration_prompt,
    look_prompt,
    red_judge_prompt,
    red_verdicts,
    review_verdict,
    spec_requirements,
)
from orchestragent.orch_usage import (
    Call,
    output_cut,
    settled_json_failure,
    transient,
    unresolved,
)
from orchestragent.providers import DSH_PRICE_FEN_PER_MTOK
from orchestragent.serialization import dumps
from orchestragent.store import Conflict
from orchestragent.workspace import Workspace


@dataclass(frozen=True)
class Route:
    provider: str
    model: str
    effort: str = ""

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


# The builder is pinned to DeepSeek V4.1 Flash 1017 on the budgeted DSH route; the
# DSH transport additionally refuses any request naming another model. Its test design
# and implementation run with thinking at low effort, set explicitly on both transports.
# Linux run r6f34ed35 (af303bb) at high spent 314,522 DSF output tokens including
# reasoning, and one 56,400-token test-design call held planning for 428.5 seconds.
# Each role's model is the benchmarked (verified) set unless the user chose otherwise with
# `orch models set` or an environment variable (see orch_models).
_BUILDER = cast(ModelChoice, choose_model("builder"))
DSF = Route(_BUILDER.provider, _BUILDER.model, _BUILDER.effort)
# Dependency files a building-on task reads up front (see _dependency_files).
DEPENDENCY_READS = 8
# Whether interface_depends_on lets a task build before those dependencies are merged.
# Off: on Boltons the integrating task built early from contracts failed its frozen tests
# in 3 of 4 Linux runs (each ~100-150 s lost), while waiting for its dependencies (10-40 s)
# and reading their merged files passed first time. The mechanism stays for opt-in use.
EARLY_INTERFACE_START = False
# The same model and reasoning effort with the wider output budget. Every DSH call (any
# role: a DeepSeek planner or supervisor thinks long too) uses it whenever the call
# reservation covers that exact request (see _widened); the limit is a ceiling, not a
# target length. Larger requests keep the plain route. Other providers have no such tier.
DSF_EXTENDED = (Route("dsh", DSF.model, DSF.effort + EXTENDED) if DSF.provider == "dsh"
                else DSF)
# Test design's reasoning effort. On Boltons at 6d701aa ~85% of each test-design call's
# output was hidden reasoning (the T-004 integration shard: ~5k answer tokens, 28-40k
# output, 150-199 s on the critical path). Thinking off made wrong tests (49c8313), so the
# verified test designer keeps low; it is the `test_designer` role.
_DESIGNER = cast(ModelChoice, choose_model("test_designer"))
DSF_TEST = Route(_DESIGNER.provider, _DESIGNER.model, _DESIGNER.effort)
# Effort of a task's FIRST DSF build attempt only; later attempts keep DSF. A build, unlike
# a frozen test, is checked at once by the tests and a retry sees the exact failure, so a
# fast optimistic first attempt may win overall (T-004 builds at 6d701aa: ~3-4k answer
# tokens in 9-28k output, 43-141 s). ORCHESTRA_DSF_BUILD_FIRST_EFFORT=off|low|high, for a
# measured comparison; low (= DSF) stays the default.
DSF_BUILD_FIRST = Route(DSF.provider, DSF.model,
                        os.environ.get("ORCHESTRA_DSF_BUILD_FIRST_EFFORT", DSF.effort))
if DSF_BUILD_FIRST.effort not in {"off", "low", "high"}:
    raise ValueError("ORCHESTRA_DSF_BUILD_FIRST_EFFORT must be off, low or high")
CLAUDE_PLAN = Route("claude", "claude-opus-5-5", "high")
# The orchestrated planner's effort (the single-agent baseline keeps CLAUDE_PLAN at high).
# Boltons, Linux: Opus high planned in 74-105 s (6d701aa, 5 runs); medium in 37-54 s with
# 5/5 runs passing acceptance and no replans (284bde9). It is the `planner` role.
_PLANNER = cast(ModelChoice, choose_model("planner"))
PLANNER = Route(_PLANNER.provider, _PLANNER.model, _PLANNER.effort)
# Repairs (failure ladder level 3) use Opus at medium with a Claude planner; with any other
# planner they use the planner's own route, so no provider is used that was not chosen.
# The judgment role: supervision and the escalated attempt. Its own role so the experiment
# "a cheap model that thinks well plans and judges; cheap workers build" can be run by
# switching planner and supervisor without touching the architecture.
_SUPERVISOR = cast(ModelChoice, choose_model("supervisor"))
CLAUDE_PATCH = Route(_SUPERVISOR.provider, _SUPERVISOR.model, _SUPERVISOR.effort)
SONNET_PLAN = Route("claude", "claude-sonnet-5-5", "medium")
# Outcomes that say nothing about the task: stop dispatching, keep the task READY.
STOP = frozenset(
    {"auth_required", "quota_exhausted", "rate_limited", "waiting_approval", "policy_blocked",
     "cancelled", "timeout"}
)
MAX_REBASES = 3
# Whether a model judges why each designed check is red on the stubs (orch_thin). On by
# default; ORCHESTRA_RED_JUDGMENT=0 turns it off so its effect can be measured.
RED_JUDGMENT = os.environ.get("ORCHESTRA_RED_JUDGMENT", "1") != "0"
MAX_REGRESSIONS = 2  # final-gate bisect rounds before the run gives up
HUMAN = Route("human", "person")  # code a person finished (`orch take` / `orch submit`)
DEFAULT_TEST_JOBS = 16  # concurrent DSF test-design shards
MAX_FORMAT_REPAIRS = 1  # DSF self-repairs of a settled unparseable/unappliable answer per attempt
# Repeats of one planning call after a transient transport/provider failure (run r073174b7
# lost the whole run to one DSF STREAM_CLOSED), and the pause before each repeat.
# Transport trouble (503, closed streams) is not a judgment call: wait and ask again, for
# a few minutes in total, before a planning call counts as failed.
TRANSIENT_RETRY_DELAYS = (2.0, 8.0, 30.0, 60.0, 120.0)
MAX_THROTTLES = 6  # consecutive rate limits before the run pauses
DSF_RESERVATION = 300  # cents each DSH call reserves before it may start
DSF_BUDGET = DshClient.input_limit - len(dumps(EDIT_SCHEMA).encode()) - 512
CLAUDE_BUDGET = 400_000
PLANNER_MAP_BYTES = 60_000
BUILDER_MAP_BYTES = 8_000
DEPENDENCY_BYTES = DshClient.input_limit  # relevant merged dependencies, not the whole repository


class Caller(Protocol):
    def __call__(
        self,
        provider: str,
        model: str,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        cancelled: Callable[[], bool],
        *,
        effort: str = "",
    ) -> NativeResult: ...


class Runner(Protocol):
    """Sandboxed unittest run: (execution dir, scratch dir, revision, test ids)."""

    def __call__(
        self, execution: Path, scratch: Path, revision: str, names: list[str] | None, /,
        *, timeout: float = 25.0,
    ) -> tuple[bool, str]: ...


class PlanError(RuntimeError):
    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class AttemptFailed(Exception):
    def __init__(self, category: str, detail: str, diff: str = "") -> None:
        super().__init__(f"{category}: {detail[:200]}")
        self.category = category
        self.detail = detail
        self.diff = diff

    @property
    def sampleable(self) -> bool:
        """Only a behavioral test failure permits alternative implementation sampling."""
        return self.category == "test"


class Waiting(AttemptFailed):
    """Code needs a dependency absent from its snapshot: refresh, spend no rung."""

    def __init__(self, owners: list[str], detail: str, diff: str = "") -> None:
        super().__init__("import", detail, diff)
        self.owners = owners


class Parked(Exception):
    pass


class StaleInputs(Parked):
    def __init__(self, candidate: str, revision: str, changed: list[str]):
        super().__init__(f"Candidate dependency contract changed: {', '.join(changed)}")
        self.candidate, self.revision, self.changed = candidate, revision, changed


class Deferred(Exception):
    pass


def orch_home(repo: Path) -> Path:
    """Orchestration state lives inside the repository's git dir, never in its tree."""
    common = Workspace.git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common) / "orch"


GENERIC_SOURCE_BYTES = 200_000  # larger tracked files (lockfiles, data) are not prompt context


class Orchestrator:
    def __init__(
        self,
        repo: Path,
        call: Caller,
        runner: Runner,
        notify: Callable[[str], None] = print,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._durable_planning: set[str] = set()
        self.repo = repo.resolve()
        self.call = call
        # Every task is verified by the checks its test designer declared, in any language.
        self.runner = runner
        self.notify = notify
        self.clock = clock
        self.sleep = sleep
        self.home = orch_home(self.repo)
        self.home.mkdir(parents=True, exist_ok=True)
        self.state = OrchState(self.home / "state.sqlite", clock)
        self.planning = PlanningStore(self.state.store)
        self.repo_view = Repo(self.repo)
        self.cancel = threading.Event()
        # git_lock covers short ref/worktree mutations; merge_lock is the merge train.
        self.git_lock = threading.RLock()
        self.merge_lock = threading.Lock()
        self.stop_reason = ""
        self.gate_failure = ""
        # AIMD: halve on rate limits and merge conflicts, +1 per merged task.
        self.aimd_lock = threading.Lock()
        self.max_jobs = self.limit = 1
        self.throttled = 0
        self.cooldown_until = 0.0
        # Wide runs: generation (model calls) and verification (sandbox runs) are
        # separate pools.
        self.flow = threading.Lock()
        self.generating: set[str] = set()
        self.slot_freed = threading.Event()  # a task left generation: dispatch may resume
        self.verifying = 0
        self.max_verify_jobs = default_verify_jobs()
        self.verify_slots = threading.BoundedSemaphore(self.max_verify_jobs)
        self.early_interface_start = EARLY_INTERFACE_START
        self.bucket: TokenBucket | None = None
        self.red_output: dict[str, str] = {}  # check id -> its output on the stubs
        self.max_cents = 0
        self.spent = 0
        self.pools: dict[tuple[str, str], list[Path]] = {}
        self.metrics: dict[str, int] = {}
        self.timing = threading.local()

    @staticmethod
    def _builder_route(task: OrchTask) -> Route:
        """Who builds a task on its own rungs: the model a person chose for it (the studio's
        per-node choice), else the builder role. Claude patches (level 3) stay the planner's."""
        if not task.route:
            return DSF
        from orchestragent import orch_models
        try:
            choice = orch_models.parse("builder", task.route)
        except ValueError as error:
            raise Parked(f"its chosen model {task.route!r} is not usable: {error}") from None
        assert isinstance(choice, ModelChoice)
        chosen = Route(choice.provider, choice.model, choice.effort)
        return DSF if chosen == DSF else chosen

    def _claude_route(self, run_id: str, *, planning: bool = False) -> Route:
        return PLANNER if planning else CLAUDE_PATCH

    # --- git ------------------------------------------------------------
    def git(self, cwd: Path, *args: str, author: str = "") -> str:
        """Hooks, user config and auto-gc never run; dates are real, not fixtures."""
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))
        env = {
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
            "GIT_COMMITTER_NAME": "orch",
            "GIT_COMMITTER_EMAIL": "orch@localhost",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "gc.auto",
            "GIT_CONFIG_VALUE_0": "0",
        }
        if author:
            env.update(GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL="orch@localhost")
        try:
            return Workspace.git(cwd, *args, env=env, timeout=120)
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or "").strip()[:400]
            raise RuntimeError(f"git {' '.join(args[:3])} failed: {detail}") from None

    def sources(self, revision: str) -> dict[str, str]:
        """Every tracked text file of a reasonable size at `revision`, in one git batch."""
        sizes = self.git(self.repo, "ls-tree", "-r", "-l", revision).splitlines()
        paths = [line.split("\t", 1)[1] for line in sizes
                 if line.split()[3].isdigit() and int(line.split()[3]) <= GENERIC_SOURCE_BYTES]
        return self.repo_view.read(revision, paths)

    def run_dir(self, run_id: str) -> Path:
        return self.home / run_id

    def integration(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "integration"

    @contextmanager
    def integration_change(self, run_id: str) -> Iterator[tuple[Path, str]]:
        """Serialize integration changes; an exception restores the durable verified HEAD.

        Read the commit point from SQLite, including when a write succeeded just before
        raising. A failed rollback stops the run before another task can adopt its bytes.
        """
        with self.merge_lock:
            if self.cancel.is_set():
                raise Deferred("interrupted before changing the integration branch")
            integration = self.integration(run_id)
            before = self.git(integration, "rev-parse", "HEAD")
            try:
                yield integration, before
            except BaseException:
                try:
                    with self.git_lock:
                        verified = self.state.run(run_id).verified_head or before
                        self.git(integration, "reset", "--hard", verified)
                except BaseException:
                    self.stop_reason = (
                        "integration rollback failed; resume to restore verified head"
                    )
                    self.cancel.set()
                    raise
                raise

    @contextmanager
    def worktree(self, run_id: str, name: str, revision: str) -> Iterator[Path]:
        """A detached worktree at `revision`, reused from a per-kind pool when one is
        free: a forced checkout plus `git clean` is far cheaper than a new worktree."""
        kind = "task" if name.startswith("T-") else name.split("-")[0]
        path = self._reuse(run_id, kind, revision)
        if path is None:
            path = self.run_dir(run_id) / "work" / f"{kind}-{uuid.uuid4().hex[:6]}"
            with self.git_lock:
                self.git(self.repo, "worktree", "add", "--detach", str(path), revision)
            self._count("worktrees_created")
        orch_verify.link_ignored(self.repo, path)
        try:
            yield path
        finally:
            with self.git_lock:
                pool = self.pools.setdefault((run_id, kind), [])
                if len(pool) < self.max_jobs + 2 and not self.cancel.is_set():
                    pool.append(path)
                else:
                    self.git(self.repo, "worktree", "remove", "--force", str(path))

    def _reuse(self, run_id: str, kind: str, revision: str) -> Path | None:
        with self.git_lock:
            pool = self.pools.get((run_id, kind), [])
            path = pool.pop() if pool else None
        if path is None:
            return None
        try:
            self.git(path, "checkout", "--detach", "--force", "--quiet", revision)
            self.git(path, "clean", "-ffdxq")
        except RuntimeError:
            with self.git_lock:
                self.git(self.repo, "worktree", "remove", "--force", str(path))
            return None
        self._count("worktrees_reused")
        return path

    def release_worktrees(self, run_id: str) -> None:
        """Remove the run's pooled worktrees (at the end of every command)."""
        with self.git_lock:
            for (owner, _), pool in self.pools.items():
                while owner == run_id and pool:
                    try:
                        self.git(self.repo, "worktree", "remove", "--force", str(pool.pop()))
                    except RuntimeError:
                        pass  # _restore prunes whatever a failed removal leaves

    def _count(self, key: str, value: int = 1, peak: bool = False) -> None:
        with self.flow:
            self.metrics[key] = max(self.metrics.get(key, 0), value) if peak else (
                self.metrics.get(key, 0) + value
            )

    def verify(
        self, execution: Path, scratch: Path, revision: str, names: list[str] | None,
        timeout: float = 25.0, *, runner: Runner | None = None,
    ) -> tuple[bool, str]:
        """Every sandbox run goes through the verification pool (CPU-bound, separate
        from the model-call concurrency)."""
        while not self.verify_slots.acquire(timeout=0.05):
            if self.cancel.is_set():
                raise Deferred("interrupted while waiting for verification")
        try:
            if self.cancel.is_set():
                raise Deferred("interrupted before verification")
            with self.flow:
                self.verifying += 1
                busy = self.verifying
            self._count("peak_verifying", busy, peak=True)
            started = time.monotonic()
            try:
                selected = self.runner if runner is None else runner
                return selected(execution, scratch, revision, names, timeout=timeout)
            finally:
                self.timing.last = time.monotonic() - started
                with self.flow:
                    self.verifying -= 1
        finally:
            self.verify_slots.release()

    def _checks_at(self, run_id: str, kind: str, revision: str,
                   tests: list[str]) -> tuple[bool, str]:
        """Checks at one revision, each in its own worktree and all at once (the verification
        pool bounds them): the verdict and report lines of one runner call over all of them,
        without one check's files or processes meeting another's. The merge gate ran them
        one after another: 20-80 s per merge on polyglot-ledger (2026-11-03, S series)."""
        unique = list(dict.fromkeys(tests))

        def one(names: list[str]) -> tuple[bool, str]:
            with self.worktree(run_id, kind, revision) as path:
                return self.verify(path, self.scratch(run_id), revision, names)

        if len(unique) <= 1:
            return one(unique)
        with ThreadPoolExecutor(max_workers=max(1, self.max_verify_jobs)) as pool:
            results = list(pool.map(lambda test: one([test]), unique))
        # A check that failed while others ran beside it is run once more on its own before
        # the failure counts: a test with a tight timeout fails under load and passes alone
        # (polyglot-ledger: vitest's 5 s limit on a test that compiles Go, at the final
        # gate's parallel suites). A pass alone is recorded, never hidden.
        for index, (ok, report) in enumerate(results):
            if ok:
                continue
            again, alone = one([unique[index]])
            if again:
                self.state.record(run_id, "check.load_sensitive", {
                    "check": unique[index][:500], "kind": kind, "revision": revision,
                    "first": report[-2000:]})
                self.notify(f"[run] {kind}: a check failed beside others and passed alone; "
                            "counted as passed (recorded as load-sensitive)")
                results[index] = (True, alone)
        return all(ok for ok, _ in results), combined([report for _, report in results])

    def _gate_scope(self, run_id: str, task: OrchTask, changed: list[str]) -> set[str]:
        """DONE tasks a merge could break: the task's dependents and the tasks of the same
        component (one build, one namespace). Everything else waits for the final gate, which
        runs every check and bisects a regression to its commit."""
        plan = self.state.run(run_id).plan
        tasks = self.state.tasks(run_id)
        entries = {t.id: t.entry() for t in tasks}
        mine = orch_generic.component_of(plan, entries[task.id])["id"] if task.id in entries \
            else ""
        scope = self._descendants(run_id, task.id)
        for other in tasks:
            if orch_generic.component_of(plan, entries[other.id])["id"] == mine:
                scope.add(other.id)
        return scope

    def _language_note(self) -> str:
        return ("Write each owned file in its own language. Stub bodies fail with 'not "
                "implemented'; replace every one in your owned files. "
                + orch_verify.sandbox_note())

    def elapsed(self) -> float:
        """Seconds this thread's last sandbox run took (its wait for a slot excluded)."""
        return float(getattr(self.timing, "last", 0.0))

    # --- planning -------------------------------------------------------
    def _call_model(
        self, run_id: str, route: Route, prompt: str, schema: dict[str, object],
        call_id: str, *, purpose: str, task_id: str = "", attempt: int = 0,
        sample: str = "", scope: str = "execution", prefix: str = "",
        base: str = "",
        journal: CallJournal | None = None,
    ) -> NativeResult:
        route = self._widened(route, prompt, schema) or route
        payload: dict[str, object] = {"prompt": prompt, "schema": schema}
        if isinstance(prompt, ContextPrompt):
            payload["context_boundary_bytes"] = len(prompt.shared.encode())
            prefix = digest(prompt.shared)[:12]
        request = dumps(payload)
        receipt = Call(
            call_id, run_id, route.provider, route.model, purpose, self.clock(),
            task_id=task_id, attempt=attempt, sample=sample, effort=route.effort,
            scope=scope, prompt_hash=digest(request), prefix_hash=prefix,
            request_bytes=len(request.encode()), base_revision=base,
        )
        if scope == "planning" and run_id in self._durable_planning:
            journal = self.planning
        if journal is not None:
            cached = journal.claim(receipt)
            if cached is not None:
                self.notify(f"[{scope}] reuse settled {purpose} {task_id or sample} a{attempt}")
                return cached[0]
        if scope == "execution" and (self.cancel.is_set() or self.stop_reason
                                     or self.max_cents and self.spent >= self.max_cents):
            raise Deferred(self.stop_reason or "execution stopped before model dispatch")
        result: NativeResult | None = None
        with self.state.usage.capture(
            receipt,
            (lambda db: journal.finish(db, receipt, result)) if journal is not None else None,
        ):
            result = self.call(route.provider, route.model, prompt, schema, call_id,
                               self.cancel.is_set, effort=route.effort)
            receipt.outcome, receipt.usage = result.outcome, result.usage
            receipt.session_id = result.session_id
        if scope in {"execution", "harden"}:
            self._spend(receipt.usage)
        assert result is not None
        return result

    def plan(
        self, spec: str, base: str = "HEAD", attempts: int = 3, test_jobs: int | None = None,
        reviewed_boundary: bool = False, integration_checks: bool = True,
    ) -> Run:
        """The planner drafts the boundary (requirements, tasks, stubs in any language); one
        test designer per task then writes frozen tests and the command that runs them."""
        if test_jobs is None:
            # One wave for up to 16 shards: networkx runs at 4b12bef queued 12-16 shards in
            # waves of 4 (planning 468-617 s); DSF showed no per-call slowdown at 4-8 calls.
            test_jobs = DEFAULT_TEST_JOBS
        if not 1 <= test_jobs <= 16:
            raise PlanError("planning needs 1..16 test jobs")
        run_id = "r" + uuid.uuid4().hex[:8]
        self.notify(f"[plan] run {run_id}; usage: orch cost --run {run_id}")
        try:
            with self.exclusive(run_id):
                base = self.git(self.repo, "rev-parse", "--verify", f"{base}^{{commit}}")
                route = self._claude_route(run_id, planning=True)
                self.planning.create(PlanningSession(
                    run_id, spec, base, route.label, route.effort,
                    attempts, test_jobs, "", "", DSF.label, DSF.effort, reviewed_boundary,
                    integration_checks, integration_scope="joins",
                ))
                self._durable_planning.add(run_id)
                return self._plan(run_id, spec, base, attempts, test_jobs, reviewed_boundary,
                                  integration_checks)
        finally:
            self.release_worktrees(run_id)  # red-check and probe worktrees
            with self.git_lock:
                self._durable_planning.discard(run_id)

    def resume_plan(self, run_id: str, *, ack_unknown_calls: list[str] | None = None) -> Run:
        """Resume planning at pinned inputs, reusing settled phase responses."""
        session = self.planning.session(run_id)
        with self.exclusive(session.id):
            try:
                return self.state.run(session.id)
            except KeyError:
                pass
            self.cancel.clear()
            self.stop_reason = ""
            self.planning.acknowledge(session.id, ack_unknown_calls or [])
            with self.git_lock:
                self._durable_planning.add(session.id)
            try:
                route = self._claude_route(session.id, planning=True)
                if ((route.label, route.effort) != (session.planner, session.effort)
                        or (DSF.label, DSF.effort) != (session.designer, session.designer_effort)):
                    raise PlanError("Pinned planner route changed; no automatic model substitution")
                return self._plan(session.id, session.spec, session.base, session.attempts,
                                  session.test_jobs, session.reviewed_boundary,
                                  session.integration_checks)
            finally:
                with self.git_lock:
                    self._durable_planning.discard(session.id)

    def _plan(
        self, run_id: str, spec: str, base: str, attempts: int, test_jobs: int = 4,
        reviewed_boundary: bool = False, integration_checks: bool = True,
    ) -> Run:
        base_sha = self.git(self.repo, "rev-parse", "--verify", f"{base}^{{commit}}")
        listing = self.repo_view.listing(base_sha)
        inputs = PlanInputs(
            spec, base_sha,
            frozenset(self.git(self.repo, "ls-tree", "-r", "--name-only", base_sha).splitlines()),
            listing + self._look_around(run_id, spec, base_sha, listing), "",
        )
        outcome = self._draft_thin(run_id, inputs, attempts, jobs=test_jobs,
                                   reviewed_boundary=reviewed_boundary,
                                   integration_checks=integration_checks)
        if isinstance(outcome, PlanError):
            raise outcome
        if outcome.plan is None:
            raise PlanError("plan failed lint", outcome.errors[-1] if outcome.errors else [])
        run = self._checkpoint(run_id, spec, base_sha, outcome.plan, outcome)
        self.state.record(run_id, "plan.distributed", {
            "map_bytes": len(inputs.repo_map.encode()), "test_jobs": test_jobs,
            "tasks": len(cast(list[object], outcome.plan["tasks"])),
            "reviewed_boundary": reviewed_boundary,
            "integration_checks": integration_checks,
        })
        return run

    def _look_around(self, run_id: str, spec: str, base: str, listing: str,
                     rounds: int = 2) -> str:
        """Before planning, the planner may open and grep the repository like a person
        would; what it saw is appended to its view. Files the spec names are shown unasked."""
        shown = ""
        named = mentioned_paths(spec, self.repo_view.tree(base))
        if named:
            shown = self.repo_view.answer(base, [{"read": p} for p in named[:MAX_REQUESTS]])
        route = self._claude_route(run_id, planning=True)
        for round_ in range(1, rounds + 1):
            result = self._call_planning(
                run_id, route, look_prompt(spec, listing, shown, round_), LOOK_SCHEMA,
                f"orch-{run_id}-look-{round_}-{uuid.uuid4().hex[:12]}", purpose="look",
                attempt=round_, base=base)
            if result.outcome != "ok":
                break  # looking is optional; planning proceeds with what is shown
            requests = result.data.get("requests")
            note = str(result.data.get("note", "")).strip()
            if not requests:
                if note:
                    shown += f"\n## PLANNER'S NOTE\n{note}\n"
                break
            try:
                answers = self.repo_view.answer(base, requests)
            except ValueError as error:
                answers = f"(requests not understood: {error})"
            shown += ("\n" if shown else "") + answers + (f"\n## NOTE\n{note}\n" if note
                                                           else "")
            self.notify("[plan] planner looked at the repository "
                        f"({len(cast(list[object], requests))} request(s))")
        return f"\n## WHAT THE PLANNER SAW\n{shown}\n" if shown else ""

    def _draft_thin(
        self, run_id: str, inputs: "PlanInputs", attempts: int, *, distributed: bool = True,
        jobs: int = 4, reviewed_boundary: bool = False,
        integration_checks: bool = True,
    ) -> "Draft | PlanError":
        """One boundary decision, then bounded independent DSF test design.

        Structural and review feedback both persist into later revisions. A draft identical
        to a rejected one, or the same structural problem fed back before, stops with a
        named code instead of buying another draft; resume replays to the same stop.
        """
        errors: list[str] = []
        seen: list[list[str]] = []
        boundary: dict[str, object] | None = None
        structural_failures = review_failures = 0
        reviewed_draft: dict[str, object] | None = None
        review_feedback: list[str] = []
        structural_feedback: list[str] = []
        rejected: set[str] = set()
        fingerprints: set[str] = set()
        stop = fingerprint = ""
        texts = spec_requirements(inputs.spec)
        for number in range(1, attempts * (2 if reviewed_boundary else 1) + 1):
            if reviewed_boundary and max(structural_failures, review_failures) >= attempts:
                break
            if self.cancel.is_set():
                return PlanError("thin planning cancelled")
            drafter = DSF if reviewed_boundary else self._claude_route(run_id, planning=True)
            self.notify(f"[plan] asking {drafter.label} "
                        f"for boundaries only (attempt {number})")
            feedback = list(dict.fromkeys([*review_feedback, *structural_feedback, *errors]))
            prompt = boundary_prompt(inputs.spec, inputs.repo_map, feedback,
                                     existing_edits=True,
                                     coupling=inputs.coupling if reviewed_boundary else "",
                                     previous=reviewed_draft)
            if reviewed_boundary:
                if len(DshClient.task_text(prompt, PLAN_SCHEMA).encode()) > DshClient.input_limit:
                    return PlanError("Boundary drafting request exceeds DSF input limit")
                result = self._call_planning(
                    run_id, DSF, prompt, PLAN_SCHEMA,
                    f"orch-{run_id}-boundary-draft-{number}-{uuid.uuid4().hex[:12]}",
                    purpose="boundary-draft", attempt=number, base=inputs.base)
                if result.outcome != "ok":
                    if settled_json_failure(result.outcome, result.message, result.usage):
                        errors = ["Previous boundary draft was invalid/truncated JSON; "
                                  "return a complete compact boundary object."]
                        seen.append(errors)
                        structural_failures += 1
                        continue
                    return PlanError(f"boundary drafter returned {result.outcome}")
                answer: dict[str, object] | PlanError = result.data
            else:
                answer = self._ask_planner(
                    f"orch-{run_id}-plan-{number}", prompt, PLAN_SCHEMA,
                    run_id=run_id, purpose="plan", attempt=number, base=inputs.base)
            if isinstance(answer, PlanError):
                return answer
            answer = canonical_boundary(fill_requirements(answer, texts))
            draft = digest(dumps(answer))
            if draft in rejected:
                stop = "repeated_boundary"
                break
            rejected.add(draft)
            errors = orch_generic.boundary_errors(answer, inputs.existing, existing_edits=True)
            if not errors:
                errors = self._pack_errors({**answer, **self._engine_key()}, inputs.base,
                                           inputs.spec)
            if not errors:
                errors = self._suite_problems(run_id, inputs.base, answer)
            if errors:
                structural_failures += 1
                fingerprint = boundary_fingerprint(errors)
                if fingerprint in fingerprints:
                    stop = "repeated_structural_error"
                fingerprints.add(fingerprint)
                structural_feedback = list(dict.fromkeys([*structural_feedback, *errors]))
            if not errors and reviewed_boundary:
                verdict = self._ask_planner(
                    f"orch-{run_id}-boundary-review-{number}",
                    boundary_review_prompt(inputs.spec, inputs.repo_map, answer, inputs.coupling,
                                           findings=review_feedback),
                    BOUNDARY_REVIEW_SCHEMA, run_id=run_id, purpose="boundary-review",
                    attempt=number, base=inputs.base)
                if isinstance(verdict, PlanError):
                    return verdict
                try:
                    errors = review_verdict(verdict)
                except ValueError as error:
                    return PlanError(str(error))  # a malformed review does not justify re-drafting
                if errors:
                    review_failures += 1
                    reviewed_draft = answer
                    review_feedback = list(dict.fromkeys([*review_feedback, *errors]))
            if not errors:
                boundary = answer
                break
            seen.append(errors)
            if stop:
                break
        planning = self.run_dir(run_id) / "planning"
        planning.mkdir(parents=True, exist_ok=True)
        if boundary is None:
            stop = stop or ("review_budget" if reviewed_boundary and review_failures >= attempts
                            else "structural_budget")
            record = BoundaryStop(stop, len(seen) + (stop == "repeated_boundary"),
                                  structural_failures, review_failures, fingerprint,
                                  seen[-1] if seen else [])
            (planning / "stop.json").write_text(dumps(asdict(record)))
            self.notify(f"[plan] boundary stopped: {stop}")
            return PlanError(f"plan failed lint ({stop})", record.errors)
        (planning / "boundary.json").write_text(dumps({
            "base": inputs.base, "boundary": boundary,
            "spec_requirements": sorted(texts) if texts is not None else "unsupported",
        }))
        return self._draft_test_shards(run_id, inputs, boundary, attempts, jobs, seen,
                                       integration_checks=integration_checks)

    @staticmethod
    def _extended(route: Route) -> Route:
        return replace(route, effort=route.effort + EXTENDED) if route.provider == "dsh" \
            else route

    def _widened(self, route: Route, prompt: str, schema: dict[str, object]) -> Route | None:
        """The extended-output route when the per-call reservation bounds this exact
        request's worst case: every request byte billed as an input token plus the full
        extended output."""
        if route.provider != "dsh" or route.model not in DSH_PRICE_FEN_PER_MTOK \
                or route.effort.endswith(EXTENDED):
            return None
        wide = self._extended(route)
        prices = DSH_PRICE_FEN_PER_MTOK[route.model]
        request = len(DshClient.task_text(prompt, schema).encode())
        worst = (request * max(prices["input_tokens"], prices["cache_creation_input_tokens"])
                 + DshClient.output_settings(wide.effort)[0] * prices["output_tokens"])
        return wide if worst <= DSF_RESERVATION * 1_000_000 else None

    def _draft_test_shards(
        self, run_id: str, inputs: "PlanInputs", boundary: dict[str, object], attempts: int,
        jobs: int, seen: list[list[str]],
        *, integration_checks: bool = True,
    ) -> "Draft | PlanError":
        """Tests for a boundary: one designer per component (so a component's tests are
        coherent with each other and its runner), then the planner's integration checks
        where tasks meet. Every check must fail on the stubs; the rest is the designers'."""
        tasks = cast(list[dict[str, Any]], boundary["tasks"])
        graph = {task["id"]: task["depends_on"] for task in tasks}
        comps = [c for c in orch_generic.components(boundary)
                 if any(orch_generic.component_of(boundary, t)["id"] == c["id"] for t in tasks)]
        stopped = threading.Event()
        schema = self._test_schema()

        def sources_for(comp: dict[str, str]) -> dict[str, str]:
            mine = [t for t in tasks if orch_generic.component_of(boundary, t)["id"] == comp["id"]]
            related = set().union(*(closure(graph, t["id"]) for t in mine)) | {t["id"]
                                                                                 for t in mine}
            paths = {p for t in tasks if t["id"] in related for p in t["files"]}
            # The pre-task revision, before any builder exists: never candidate code.
            return self.repo_view.read(inputs.base, sorted(paths))

        def budget(prompt: str, sch: dict[str, object]) -> None:
            if len(DshClient.task_text(prompt, sch).encode()) > DshClient.input_limit:
                stopped.set()
                raise PlanError("test-design request exceeds the input limit")

        def answer(route: Route, prompt: str, sch: dict[str, object], purpose: str,
                   label: str, number: int) -> NativeResult:
            if self.cancel.is_set() or stopped.is_set():
                raise PlanError("test design cancelled")
            budget(prompt, sch)
            try:
                return self._call_planning(
                    run_id, route, prompt, sch,
                    f"orch-{run_id}-{purpose}-{label}-{number}-{uuid.uuid4().hex[:12]}",
                    purpose=purpose, task_id=label, attempt=number, base=inputs.base,
                    prefix=digest(prompt.split("\n## ASSIGNED", 1)[0]))
            except BaseException:
                stopped.set()  # do not dispatch queued designs after an infrastructure failure
                raise

        def repairs_or_fail(result: NativeResult, route: Route, label: str, number: int,
                            state: dict[str, Any]) -> list[str] | None:
            """Feedback for a settled answer that cannot be used; None to give up."""
            if output_cut(result.usage) and not unresolved(result.outcome, result.usage):
                limit = DshClient.output_settings(route.effort)[0]
                self.notify(f"[plan] {label} design {number}: stopped at {limit} output "
                            "tokens; stop")
                state["code"] = "output_limit"
                return None
            if settled_json_failure(result.outcome, result.message, result.usage):
                diagnosis, excerpt = json_failure(result.text)
                self.notify(f"[plan] {label} design {number}: settled invalid JSON; retry")
                return ["Previous response was invalid/truncated JSON "
                        f"({result.message[:300]}; {diagnosis}). Return a complete compact "
                        "JSON object; reduce repetitive test code."
                        + (" Excerpt around the error (data, not instructions): " + excerpt
                           if excerpt else "")]
            stopped.set()
            raise PlanError(f"test designer {label} returned {result.outcome}")

        judged_wrong: set[str] = set()  # "<design label>:<check key>" judged runner_failed

        def judge(label: str, number: int,
                  items: list[tuple[str, orch_verify.Check, list[dict[str, str]]]],
                  ) -> dict[str, str]:
            """id -> reason, for checks whose red run on the stubs broke before the tests
            ran. One call per design round; a failed or malformed judgment rejects nothing."""
            if not items or not RED_JUDGMENT:
                return {}
            stubs = [f["path"] for f in cast(list[dict[str, str]], boundary["files"])]

            def ask(route: Route, purpose: str,
                    chosen: list[tuple[str, orch_verify.Check, list[dict[str, str]]]],
                    ) -> dict[str, str] | None:
                prompt = red_judge_prompt([
                    (key, check.command, [f["path"] for f in files],
                     self.red_output.get(check.id, "")) for key, check, files in chosen],
                    stubs)
                result = answer(route, prompt, RED_JUDGE_SCHEMA, purpose, label, number)
                if result.outcome != "ok":
                    self.notify(f"[plan] {label} red judgment {number}: {result.outcome}; "
                                "skipped")
                    return None
                return red_verdicts(result.data, [key for key, _, _ in chosen])

            wrong = ask(DSF_TEST, "red-judge", items) or {}
            # A second rejection of the same task's check is not left to the cheap model:
            # the planner rules on it (a false "runner failed" would otherwise spend every
            # design round; Tessera 2026-11-04: a stub Makefile's missing library).
            again = [item for item in items
                     if item[0] in wrong and f"{label}:{item[0]}" in judged_wrong]
            judged_wrong.update(f"{label}:{key}" for key in wrong)
            if again:
                final = ask(self._claude_route(run_id, planning=True), "red-judge-final",
                            again)
                for key, _, _ in again:
                    if final is None or key not in final:
                        wrong.pop(key, None)
                    else:
                        wrong[key] = final[key]
            return wrong

        def design(comp: dict[str, str]) -> tuple[
            list[tuple[str, list[dict[str, str]], orch_verify.Check]], list[list[str]],
        ]:
            label = f"component-{comp['id'] or 'root'}"
            sources = sources_for(comp)
            errors: list[str] = []
            rejected: list[list[str]] = []
            state: dict[str, Any] = {"code": "lint_budget"}
            marks: set[str] = set()
            repairs = revisions = 0
            mine = [t["id"] for t in tasks
                    if orch_generic.component_of(boundary, t)["id"] == comp["id"]]
            # Shards that passed every gate stay accepted; a repair round asks only for the
            # rest (one long component answer is not re-derived for one bad shard).
            accepted: dict[str, tuple[list[dict[str, str]], orch_verify.Check]] = {}
            for number in range(1, attempts + MAX_FORMAT_REPAIRS + 2):
                if revisions >= attempts:
                    break
                pending = [tid for tid in mine if tid not in accepted]
                prompt = component_test_prompt(inputs.spec, boundary, inputs.repo_map, errors,
                                               component=comp, sources=sources,
                                               only=pending if accepted else None)
                route = self._widened(DSF_TEST, prompt, schema) or DSF_TEST
                result = answer(route, prompt, schema, "test-design", label, number)
                if result.outcome != "ok":
                    feedback = repairs_or_fail(result, route, label, number, state)
                    if feedback is None:
                        rejected.append([f"Answer stopped at the output limit "
                                         f"({result.message[:200]})"])
                        break
                    errors = feedback
                    rejected.append(errors)
                    if repairs < MAX_FORMAT_REPAIRS:
                        repairs += 1
                    else:
                        revisions += 1
                    continue
                try:
                    fresh = orch_generic.component_shards(
                        boundary, comp, result.data, existing=inputs.existing,
                        only=pending if accepted else None)
                    problems: list[str] = []
                    passing: list[tuple[str, list[dict[str, str]], orch_verify.Check]] = []
                    for tid, files, check in fresh:
                        problem = self._red_problem(run_id, inputs.base, boundary, files, check)
                        if not problem:
                            trial = orch_generic.assemble(
                                boundary, [(t, f, c.id) for t, (f, c) in accepted.items()
                                           if t != tid]  # a resent shard replaces its old one
                                + [(tid, files, check.id)], inputs.existing,
                                existing_edits=True)
                            packed = self._pack_errors(trial, inputs.base, inputs.spec,
                                                       only_task=tid)
                            problem = "; ".join(packed)
                        if problem:
                            problems.append(f"{tid}: {problem}")
                        else:
                            passing.append((tid, files, check))
                    wrong = judge(label, number, [(tid, check, files)
                                                  for tid, files, check in passing])
                    for tid, files, check in passing:
                        if tid in wrong:
                            problems.append(f"{tid}: on the stubs this command failed before "
                                            f"its tests ran, so it is red for the wrong "
                                            f"reason: {wrong[tid]}")
                        else:
                            accepted[tid] = (files, check)
                    errors = problems
                    (self.run_dir(run_id) / "planning").mkdir(parents=True, exist_ok=True)
                    (self.run_dir(run_id) / "planning" / f"{label}-{number}.json").write_text(
                        dumps({"shards": [{"task": tid, "files": files, "check": check.id}
                                          for tid, files, check in fresh],
                               "errors": errors, "accepted": not errors,
                               "accepted_tasks": sorted(accepted),
                               "source_revision": inputs.base,
                               "source_selectors": sorted(sources)}))
                    if not errors:
                        return [(tid, *accepted[tid]) for tid in mine], rejected
                    self.notify(f"[plan] {label} design {number}: {len(errors)} shard(s) sent "
                                f"back: {errors[0][:200]}")
                except (ValueError, TypeError, KeyError) as error:
                    errors = [str(error)]
                    self.notify(f"[plan] {label} design {number}: {str(error)[:300]}")
                rejected.append(errors)
                revisions += 1
                mark = boundary_fingerprint(errors[:1])
                if mark in marks:
                    state["code"] = "repeated_lint_error"
                    break
                marks.add(mark)
            stopped.set()
            raise PlanError(f"test designer {label} failed lint ({state['code']})", errors)

        Integration = list[tuple[list[str], list[dict[str, str]], orch_verify.Check]]

        def integrate(taken: set[str] | None, start: int = 1,
                      errors: list[str] | None = None) -> tuple[Integration, int]:
            """The planner's checks where tasks meet (none when nothing meets), and the last
            attempt number used. It needs only the boundary, so it runs alongside the
            component designs; `taken` (their test paths) is None then, and a path clash is
            sent back afterwards as a further attempt."""
            if not integration_checks or not orch_generic.joins(boundary):
                return [], 0
            route = self._claude_route(run_id, planning=True)
            errors = list(errors or [])
            for number in range(start, attempts + 1):
                prompt = integration_prompt(inputs.spec, boundary, inputs.repo_map, errors)
                result = answer(route, prompt, orch_generic.INTEGRATION_SCHEMA,
                                "integration-design", "planner", number)
                if result.outcome != "ok":
                    stopped.set()
                    raise PlanError(f"integration design returned {result.outcome}")
                try:
                    found = orch_generic.integration_checks(boundary, result.data,
                                                            existing=inputs.existing)
                    for covered, files, check in found:
                        for file in files:
                            if taken is not None and file["path"].casefold() in taken:
                                raise ValueError(f"{file['path']}: already a component's test")
                        problem = self._red_problem(run_id, inputs.base, boundary, files, check)
                        if problem:
                            raise ValueError(f"check covering {covered}: {problem}")
                    labels = {f"I{n}": covered for n, (covered, _, _) in enumerate(found, 1)}
                    wrong = judge("integration", number, [
                        (f"I{n}", check, files) for n, (_, files, check) in enumerate(found, 1)])
                    if wrong:
                        raise ValueError("; ".join(
                            f"check covering {labels[key]}: on the stubs this command failed "
                            f"before its tests ran: {reason}" for key, reason in wrong.items()))
                    return found, number
                except (ValueError, TypeError, KeyError) as error:
                    errors = [str(error)]
                    self.notify(f"[plan] integration design {number}: {str(error)[:300]}")
            stopped.set()
            raise PlanError("integration design failed lint", errors)

        joined = bool(integration_checks and orch_generic.joins(boundary))
        self.notify(f"[plan] {len(comps)} component test design(s)"
                    + (", integration checks alongside" if joined else ""))
        try:
            # The integration design needs only the boundary: it runs alongside the
            # component designs instead of after them (on the Opus control it was 20-40%
            # of each run's wall time, all on the critical path).
            with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(comps))) + 1) as pool:
                early = pool.submit(integrate, None) if joined else None
                try:
                    designed = list(pool.map(design, comps))
                except PlanError as error:
                    # A design stopped because the integration design failed first: report
                    # the cause, not the cancellation it caused.
                    cause = early.exception() if early is not None and "cancelled" in str(
                        error) else None
                    if isinstance(cause, BaseException):
                        raise cause from None
                    raise
                first, used = early.result() if early is not None else ([], 0)
            shards = [shard for found, _ in designed for shard in found]
            rejected = [*seen, *(errors for _, failures in designed for errors in failures)]
            taken = {f["path"].casefold() for _, files, _ in shards for f in files}
            clashes = sorted({f["path"] for _, files, _ in first for f in files
                              if f["path"].casefold() in taken})
            integration = first
            if clashes:
                self.notify(f"[plan] integration design {used}: {clashes[0]}: already a "
                            "component's test")
                integration, _ = integrate(
                    taken, used + 1, [f"{path}: already a component's test; choose another "
                                      "path" for path in clashes])
            assembled: list[tuple[str, list[dict[str, str]], str]] = [
                (tid, files, check.id) for tid, files, check in shards]
            amended = {t["id"]: dict(t) for t in tasks}
            for covered, files, check in integration:
                # The check runs where the covered tasks are all in: at the one that depends
                # on every other covered task. When none does, the deepest covered task
                # takes the check and gains the others as dependencies (it has the largest
                # dependency closure, so no cycle can arise).
                closures = {tid: closure(graph, tid) for tid in covered}
                holder = next((tid for tid in covered
                               if all(o == tid or o in closures[tid] for o in covered)), "")
                if not holder:
                    holder = max(covered, key=lambda tid: (len(closures[tid]), tid))
                    extra = [o for o in covered if o != holder
                             and o not in amended[holder]["depends_on"]]
                    amended[holder]["depends_on"] = [*amended[holder]["depends_on"], *extra]
                assembled.append((holder, files, check.id))
            joined_boundary = {**boundary, "tasks": [amended[t["id"]] for t in tasks]}
            plan = orch_generic.assemble(joined_boundary, assembled, inputs.existing,
                                         existing_edits=True)
            problems = self._pack_errors(plan, inputs.base, inputs.spec)
        except PlanError as error:
            return error
        except Conflict:
            raise  # recovery/receipt conflicts are not model-output lint failures
        except (ValueError, TypeError, KeyError, SyntaxError) as error:
            return Draft(None, len(seen), [*seen, [str(error)]])
        return Draft(None if problems else plan, len(rejected),
                     [*rejected, problems] if problems else rejected)

    def _call_planning(
        self, run_id: str, route: Route, prompt: str, schema: dict[str, object],
        call_id: str, *, purpose: str, task_id: str = "", attempt: int = 0,
        sample: str = "", base: str = "", prefix: str = "",
    ) -> NativeResult:
        """A planning call that repeats itself after a transient failure.

        Each repeat is its own journal phase (sample suffix ~retryN), so a resumed planning
        session replays every settled physical call in order and never guesses which one
        was billed. The last result is returned whatever it is; callers classify it.
        """
        result = NativeResult("cancelled", {})
        for retry in range(len(TRANSIENT_RETRY_DELAYS) + 1):
            result = self._call_model(
                run_id, route, prompt, schema,
                call_id if not retry else f"{call_id}-r{retry}",
                purpose=purpose, task_id=task_id, attempt=attempt,
                sample=sample if not retry else f"{sample}~retry{retry}",
                scope="planning", base=base, prefix=prefix,
            )
            if (retry == len(TRANSIENT_RETRY_DELAYS)
                    or not transient(result.outcome, result.message, result.usage)):
                return result
            code = result.usage.get("turn_error") or result.message[:80]
            self.notify(f"[plan] {purpose} {task_id or '-'} {attempt}: {result.outcome} "
                        f"({code}); repeat {retry + 1}/{len(TRANSIENT_RETRY_DELAYS)}")
            if self.cancel.wait(TRANSIENT_RETRY_DELAYS[retry]):
                return result
        return result

    def _ask_planner(
        self, call_id: str, prompt: str, schema: dict[str, object], *, run_id: str,
        purpose: str, attempt: int, sample: str = "", base: str = "", scope: str = "planning",
    ) -> dict[str, object] | PlanError:
        route = self._claude_route(run_id, planning=True)
        call = f"{call_id}-{uuid.uuid4().hex[:12]}"
        result = (self._call_planning(
            run_id, route, prompt, schema, call, purpose=purpose, attempt=attempt,
            sample=sample, base=base,
        ) if scope == "planning" else self._call_model(
            run_id, route, prompt, schema, call, purpose=purpose, attempt=attempt,
            sample=sample, scope=scope, base=base,
        ))
        if result.outcome != "ok":
            return PlanError(f"planner returned {result.outcome}: {result.message}")
        return result.data

    def _engine_key(self) -> dict[str, str]:
        return {"engine": "generic"}

    def _test_schema(self) -> dict[str, object]:
        return orch_generic.COMPONENT_TEST_SCHEMA

    def _suite_problems(self, run_id: str, base: str, boundary: dict[str, object]) -> list[str]:
        """A component's declared test command must at least run here (on the stubs it may
        pass or fail; `python` for `python3`, a missing runner, a wrong directory may not)."""
        suites = [c for c in orch_generic.components(boundary) if c["test_command"].strip()]
        if not suites:
            return []
        files = {f["path"]: f["content"]
                 for f in cast(list[dict[str, str]], boundary.get("files", []))}
        problems = []
        with self.worktree(run_id, "red", base) as path:
            apply_edits(path, list(files), [
                {"path": name, "search": "", "replace": content}
                for name, content in files.items()])
            for comp in suites:
                result = orch_verify.run_command(path, comp["test_command"],
                                                 orch_verify.MIN_TIMEOUT)
                if result.code in orch_verify.NOT_RUNNABLE:
                    problems.append(
                        f"component {comp['id'] or 'root'}: its test_command could not run "
                        f"(exit {result.code}): {result.output[-400:].strip()}. Use the tools "
                        "installed here, from the repository root (`cd <dir> && ...`).")
        return problems

    def _red_problem(self, run_id: str, base: str, boundary: dict[str, object],
                     files: list[dict[str, str]], check: "orch_verify.Check") -> str:
        """A designed check must run and fail on the stubs; "" when it does."""
        tree = {f["path"]: f["content"]
                for f in [*cast(list[dict[str, str]], boundary["files"]), *files]}
        with self.worktree(run_id, "red", base) as path:
            apply_edits(path, list(tree), [
                {"path": name, "search": "", "replace": content}
                for name, content in tree.items()])
            usable, problem, output = orch_verify.red(path, check)
            self.red_output[check.id] = output
            if usable:  # stubs only, no builder code yet: a trusted cache seed
                orch_verify.keep_seed(path, orch_verify.seed_path(self.scratch(run_id), check))
        return "" if usable else problem

    def _pack_errors(
        self, plan: dict[str, object], base: str, spec: str = "", *, only_task: str = "",
    ) -> list[str]:
        """Tasks whose owned files and frozen tests cannot fit one DSF prompt."""
        context = RunContext.of_plan(plan)
        tasks = [task for task in tasks_from(plan) if not only_task or task.id == only_task]
        sources = self.repo_view.read(base, [p for task in tasks for p in task.files])
        errors: list[str] = []
        for task in tasks:
            owned = {p: context.files.get(p, sources.get(p, "")) for p in task.files}
            try:
                context_pack(task, context.requirements, "", context.interfaces_for(task),
                             owned, context.tests_for(task),
                             DSF_BUDGET,
                             original_spec=spec,
                             dependency_contracts=context.dependency_contracts(task, base))
            except ContextTooLarge as error:
                errors.append(
                    f"{error}; reduce per-task owned files or repetitive test setup. "
                    "The original specification and required behaviours must remain complete."
                )
        return errors

    def _checkpoint(
        self, run_id: str, spec: str, base_sha: str, plan: dict[str, object],
        draft: "Draft",
    ) -> Run:
        """Commit stubs and frozen tests on a fresh integration branch and tag it."""
        hashed = plan_hash(plan)
        branch = f"orch/{run_id}"
        integration = self.integration(run_id)
        files = plan_files(plan)
        with self.git_lock:
            exists = bool(self.git(self.repo, "branch", "--list", branch))
            if not exists:
                self.git(self.repo, "worktree", "add", "-b", branch, str(integration), base_sha)
            elif run_id not in self._durable_planning:
                raise PlanError("Planning branch already exists; preserve it for inspection")
            elif not integration.exists():
                self.git(self.repo, "worktree", "add", str(integration), branch)
        orch_verify.link_ignored(self.repo, integration)
        if integration.is_symlink() or any(
            (integration / path).is_symlink()
            or not (integration / path).resolve().is_relative_to(integration.resolve())
            for path in files
        ):
            raise PlanError("Unsafe path in planning checkpoint; preserve it for inspection")
        if self.git(integration, "symbolic-ref", "--short", "HEAD") != branch:
            raise PlanError("Planning worktree changed branches; preserve it for inspection")
        head = self.git(integration, "rev-parse", "HEAD")
        if head == base_sha:
            dirty = set(self.git(integration, "diff", "--name-only", "HEAD").splitlines())
            dirty.update(self.git(integration, "ls-files", "--others",
                                  "--exclude-standard").splitlines())
            if dirty - set(files) or any(
                (integration / path).is_symlink()
                or ((integration / path).exists()
                    and (not (integration / path).is_file()
                         or (integration / path).read_text() != source))
                for path, source in files.items()
            ):
                raise PlanError("Planning worktree has edits outside the saved plan; preserve them")
            apply_edits(integration, list(files), [
                {"path": name, "search": "", "replace": content} for name, content in files.items()
            ])
            self.git(integration, "add", "-f", "--", *sorted(files))
            summary = " ".join(str(plan.get("summary", "")).split())[:60]
            self.git(
                integration, "commit", "-m",
                f"orch plan {run_id}: {summary}\n\nOrch-Run: {run_id}\nOrch-Plan: {hashed}\n",
                author=f"orch {self._claude_route(run_id, planning=True).label}",
            )
        else:
            parents = self.git(self.repo, "rev-list", "--parents", "-n", "1", head).split()[1:]
            identity = parse_trailers(self.git(self.repo, "show", "-s", "--format=%B", head))
            changed = set(self.git(self.repo, "diff", "--name-only", base_sha, head).splitlines())
            if (parents != [base_sha] or identity.get("Orch-Run") != run_id
                    or identity.get("Orch-Plan") != hashed or changed != set(files)
                    or self.repo_view.read(head, files) != files
                    or self.git(integration, "status", "--porcelain")):
                raise PlanError("Existing checkpoint differs from the saved plan; preserve it")
        checkpoint = self.git(integration, "rev-parse", "HEAD")
        tag = f"orch/{run_id}/plan"
        if self.git(self.repo, "tag", "--list", tag):
            if self.git(self.repo, "rev-parse", f"{tag}^{{commit}}") != checkpoint:
                raise PlanError("Planning tag changed; preserve it for inspection")
        else:
            self.git(self.repo, "tag", tag, checkpoint)
        baseline, baseline_note = self._baseline(run_id, base_sha)
        run = Run(
            id=run_id,
            repo=str(self.repo),
            base=base_sha,
            branch=branch,
            spec=spec,
            plan=plan,
            plan_hash=hashed,
            repo_map=self.repo_view.listing(checkpoint, BUILDER_MAP_BYTES),
            created_at=self.clock(),
            checkpoint=checkpoint,
            baseline=baseline,
            verified_head=checkpoint,
            environment_root="", environment_fingerprint="",
        )
        tasks = tasks_from(plan)
        self.state.create_run(run, tasks)
        self.state.record(run_id, "plan.baseline", {"tests": len(baseline), "note": baseline_note})
        self.notify(f"[plan] {run_id}: {len(tasks)} task(s), plan {hashed}, branch {branch}")
        return run

    def _baseline(self, run_id: str, base: str) -> tuple[list[str], str]:
        """Checks that must stay green; none until the planner declares project checks."""
        return [], "no pre-existing checks declared"

    def scratch(self, run_id: str) -> Path:
        path = self.run_dir(run_id) / "tmp"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # --- execution ------------------------------------------------------
    @contextmanager
    def exclusive(self, run_id: str) -> Iterator[None]:
        lock = self.run_dir(run_id) / "lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(f"run {run_id} is already executing") from None
            try:
                yield
            finally:
                self.release_worktrees(run_id)

    def execute(
        self, run_id: str, jobs: int | None = None,
        verify_jobs: int | None = None,
        rpm: float = 0, max_cents: int = 0,
    ) -> Run:
        """Run (or resume) every task.

        `jobs` caps concurrent model calls (AIMD starts there), `verify_jobs` concurrent
        sandbox runs (default: CPU count), `rpm` builder calls started per minute across
        the run, `max_cents` this run's model spend (a kill switch on top of the DSH
        budget).
        """
        with self.exclusive(run_id):
            self.state.run(run_id)
            jobs = jobs if jobs is not None else 4
            verify_jobs = verify_jobs if verify_jobs is not None else 0
            for task_id in self.state.recover(run_id):
                status = self.state.task(run_id, task_id).status
                self.notify(f"[run] {task_id} was interrupted; recovered as {status}")
            active = [t for t in self.state.tasks(run_id) if t.status != "REPLANNED"]
            graph_errors = dependency_errors({t.id: t.depends_on for t in active},
                                            {t.id: t.interface_depends_on for t in active})
            if graph_errors:
                return self.state.update_run(run_id, "run.graph_invalid", {
                    "errors": graph_errors,
                }, status="parked", message="Invalid dependency graph: " + graph_errors[0])
            self._restore(run_id)
            self.stop_reason = self.gate_failure = ""
            self.cancel.clear()
            self.generating.clear()
            self.slot_freed.clear()
            self.max_jobs = self.limit = max(1, jobs)
            self.throttled = 0
            self.cooldown_until = 0.0
            self.max_verify_jobs = verify_jobs or default_verify_jobs()
            self.verify_slots = threading.BoundedSemaphore(self.max_verify_jobs)
            self.bucket = TokenBucket(rpm, self.clock, self.sleep) if rpm > 0 else None
            self.max_cents = max(0, max_cents)
            self.spent = self.state.spent(run_id)
            self.metrics = {}
            self.state.update_run(
                run_id, "run.started",
                {"jobs": jobs, "verify_jobs": self.max_verify_jobs, "rpm": rpm,
                 "max_cents": max_cents},
                status="running",
            )
            while True:
                self._drain(run_id)
                tasks = self.state.tasks(run_id)
                if self.stop_reason or not all(t.status in FINISHED for t in tasks):
                    break
                if self._final_gate(run_id):
                    break
            if self.bucket is not None:
                self.metrics["paced_seconds"] = round(self.bucket.waited)
            self.state.record(run_id, "run.metrics", dict(self.metrics))
            return self._finish(run_id)

    def _spend(self, usage: dict[str, object]) -> None:
        cents = usage.get("settled_cents", 0)
        if type(cents) is int:
            with self.flow:
                self.spent += cents

    def _restore(self, run_id: str) -> None:
        """Undo whatever a killed process left on the integration branch past the last
        verified HEAD (an ungated cherry-pick, half a revert), and drop stale worktrees."""
        run = self.state.run(run_id)
        integration = self.integration(run_id)
        with self.git_lock:
            self.pools = {key: pool for key, pool in self.pools.items() if key[0] != run_id}
            for operation in ("cherry-pick", "revert"):
                try:
                    self.git(integration, operation, "--abort")
                except RuntimeError:
                    pass  # nothing was in progress
            head = self.git(integration, "rev-parse", "HEAD")
            dirty = self.git(integration, "status", "--porcelain")
            if run.verified_head and (head != run.verified_head or dirty):
                self.git(integration, "reset", "--hard", run.verified_head)
                self.state.record(
                    run_id, "run.integration_restored", {"from": head, "to": run.verified_head}
                )
                self.notify(f"[run] integration branch restored to {run.verified_head[:10]}")
            work = self.run_dir(run_id) / "work"
            for stale in sorted(work.iterdir()) if work.is_dir() else []:
                try:
                    self.git(self.repo, "worktree", "remove", "--force", str(stale))
                except RuntimeError:
                    shutil.rmtree(stale, ignore_errors=True)
            self.git(self.repo, "worktree", "prune")

    def _drain(self, run_id: str) -> None:
        """Dispatch READY tasks within the AIMD limit until nothing more can run.

        Only tasks still generating count against the limit: a task waiting for its
        sandbox runs or the merge train no longer holds a model-call slot."""
        futures: dict[Future[None], str] = {}
        workers = self.max_jobs + self.max_verify_jobs
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while True:
                try:
                    self._promote(run_id)
                    ready = self._ready(run_id, set(futures.values()))
                    room = min(self._room(len(self.generating)), workers - len(futures))
                    if (self.limit == self.max_jobs
                            and room == max(0, self.max_jobs - len(self.generating))):
                        self._count("ready_backlog_peak", max(0, len(ready) - room), peak=True)
                    for task in ready:
                        if room <= 0:
                            break
                        started = self.state.transition(
                            run_id, task.id, "RUNNING", "task.started",
                            {"limit": self.limit},
                            attempt=task.attempts + 1, attempts=task.attempts + 1,
                        )
                        with self.flow:
                            self.generating.add(task.id)
                            busy = len(self.generating)
                        self._count("peak_generating", busy, peak=True)
                        futures[pool.submit(self._attempt, run_id, started)] = task.id
                        room -= 1
                    if not futures:
                        if self.stop_reason or not ready:
                            return
                        self.sleep(min(0.1, max(0.05, self.cooldown_until - self.clock())))
                        continue
                    done: set[Future[None]] = set()
                    while not done and not self.slot_freed.is_set():
                        done, _ = wait(futures, timeout=0.05, return_when=FIRST_COMPLETED)
                    self.slot_freed.clear()
                    for future in done:
                        futures.pop(future)
                        future.result()
                except KeyboardInterrupt:
                    self.cancel.set()
                    self.stop_reason = self.stop_reason or "interrupted by user"
                except Exception as error:
                    # Stop admissions before draining workers. Durable state is reconciled on
                    # resume; do not loop on a broken store or wait for another model answer.
                    self.stop_reason = (self.stop_reason
                                        or f"scheduler fault: {type(error).__name__}")
                    self.cancel.set()
                    break

    def _room(self, busy: int) -> int:
        """New dispatches allowed now: AIMD limit, cooldown, this run's
        spend cap and the remaining DSH budget."""
        if self.cancel.is_set():
            self.stop_reason = self.stop_reason or "interrupted by user"
        if self.max_cents and self.spent >= self.max_cents and not self.stop_reason:
            self.stop_reason = f"run spend cap ¥{self.max_cents / 100:.2f} reached"
        if self.stop_reason or self.clock() < self.cooldown_until:
            return 0
        return max(0, self.limit - busy)

    def _throttle(self, label: str) -> None:
        with self.aimd_lock:
            self.limit = max(1, self.limit // 2)
            self.throttled += 1
            delay = min(60.0, 2.0**self.throttled)
            self.cooldown_until = max(self.cooldown_until, self.clock() + delay)
            if self.throttled > MAX_THROTTLES:
                self.stop_reason = self.stop_reason or f"{label} rate limited repeatedly"
        self.notify(f"[run] {label} rate limited; concurrency {self.limit}, cooling {delay:.0f}s")

    def _congest(self) -> None:
        with self.aimd_lock:
            self.limit = max(1, self.limit // 2)

    def _relax(self) -> None:
        with self.aimd_lock:
            self.limit = min(self.max_jobs, self.limit + 1)
            self.throttled = 0

    def _final_gate(self, run_id: str) -> bool:
        """Pre-existing tests and every merged task's tests must pass together at the
        end (merge gates ran only the tests a change could affect); a regression is
        bisected to one task commit, reverted and rebuilt.

        Returns True when the run may finish, False when tasks were reopened.
        """
        run = self.state.run(run_id)
        done = [task for task in self.state.tasks(run_id) if task.status == "DONE"]
        merged = [test for task in done for test in task.tests]
        if not run.baseline and not merged:
            return True
        integration = self.integration(run_id)
        head = self.git(integration, "rev-parse", "HEAD")

        def green(revision: str, tests: list[str]) -> tuple[bool, str]:
            return self._checks_at(run_id, "final", revision, tests)

        passed, report = green(head, [*run.baseline, *merged])
        self.state.record(
            run_id, "run.final_gate", {"passed": passed, "head": head,
                                       "tests": len(run.baseline) + len(merged)},
        )
        if passed:
            # Each component's own test command, as a person would run it: the delivered
            # repository must be green on its own terms, not only on orch's checks.
            suites = [orch_verify.Check(c["test_command"], ()).id
                      for c in orch_generic.components(run.plan) if c["test_command"].strip()]
            if suites:
                ok, suite_report = green(head, suites)
                # a suite that could not start says nothing about the code
                statuses = [line for line in suite_report.split("\n\n", 1)[0].splitlines()
                            if line.startswith(("FAIL", "TIMEOUT"))]
                unrunnable = bool(statuses) and all(
                    any(f"(exit {code})" in line for code in orch_verify.NOT_RUNNABLE)
                    for line in statuses)
                self.state.record(run_id, "run.component_suites",
                                  {"passed": ok, "suites": len(suites),
                                   "unrunnable": unrunnable})
                if not ok and not unrunnable:
                    owner = self._suite_owner(run_id, suite_report)
                    if owner and run.regressions < MAX_REGRESSIONS:
                        # A test that passes under its own check command but not under the
                        # component's (it needs a variable the check sets, a file another
                        # check writes) is this task's test problem: judgment, not a park.
                        evidence = ("the component's own test command fails on the finished "
                                    f"result, and the failure names {owner}'s tests; they "
                                    "pass under their own check command, so the tests must "
                                    "also hold under the component's test command:\n")
                        self.state.reopen(
                            run_id, owner, [], head, {"suite": True}, level=2,
                            pending_candidate="", failed_candidate="",
                            last_error=(evidence + suite_report)[-3000:],
                            last_signature=signature("suite", suite_report))
                        self.notify("[run] final gate: a component's suite fails on the "
                                    f"result; {owner} to the supervisor")
                        return False
                    self.gate_failure = ("a component's own test suite fails on the result: "
                                         + suite_report.strip()[-400:])
            return True
        if run.regressions >= MAX_REGRESSIONS:
            self.gate_failure = f"tests still fail after {run.regressions} rounds: {report[-200:]}"
            return True
        commits = self.git(
            self.repo, "rev-list", "--reverse", f"{run.checkpoint}..{head}"
        ).splitlines()
        if run.baseline and not green(head, run.baseline)[0]:
            if not green(run.checkpoint, run.baseline)[0]:
                self.gate_failure = (
                    "the plan's own files break pre-existing tests (checkpoint is red): "
                    + report.strip()[-200:]
                )
                return True
            tests, good = run.baseline, -1  # checkpoint is green: stubs touch no old code
        else:
            failing = orch_verify.failed(report, merged)
            start = self._proven(run_id, done, commits).get(failing[0], "") if failing else ""
            if start not in commits:
                self.gate_failure = f"merged tests fail at the end: {report.strip()[-200:]}"
                return True
            tests, good = [failing[0]], commits.index(start)
            # Bisect only between two confirmed ends: green where the test was last
            # proven, red at HEAD with the test alone. Otherwise the failure depends on
            # other tests or is not deterministic, and no single commit is to blame.
            if green(head, tests)[0] or not green(commits[good], tests)[0]:
                self.gate_failure = (
                    f"{failing[0]} fails only together with other tests or not every time "
                    f"(test interference); fix the tests, then resume: {report.strip()[-200:]}"
                )
                return True
        bad = len(commits) - 1
        while bad - good > 1:
            middle = (good + bad) // 2
            if green(commits[middle], tests)[0]:
                good = middle
            else:
                bad = middle
        return self._revert_culprit(run_id, commits, bad, head, report)

    def _suite_owner(self, run_id: str, report: str) -> str:
        """The finished task whose test files a component-suite report names most; empty
        when the output names none of them (then no task is to blame)."""
        body = report.split("\n\n", 1)[1] if "\n\n" in report else report
        best, score = "", 0
        for task in self.state.tasks(run_id):
            if task.status != "DONE":
                continue
            names = {Path(path).name for path in orch_verify.files_of(list(task.tests))}
            hits = sum(body.count(name) for name in names if name)
            if hits > score:
                best, score = task.id, hits
        return best

    def _proven(
        self, run_id: str, done: list[OrchTask], commits: list[str]
    ) -> dict[str, str]:
        """check id -> the latest commit whose gate proved it green: its owner's merge."""
        seen: dict[str, list[str]] = {}
        for task in done:
            for test in task.tests:
                seen.setdefault(test, []).append(task.commit)
        return {
            test: max(known, key=commits.index)
            for test, candidates in seen.items()
            if (known := [c for c in candidates if c in commits])
        }

    def _revert_culprit(
        self, run_id: str, commits: list[str], bad: int, head: str, report: str
    ) -> bool:
        """Revert the bisected task commit and its merged dependents; reopen them."""
        integration = self.integration(run_id)
        culprit = parse_trailers(self.git(self.repo, "show", "-s", "--format=%B", commits[bad]))
        tasks = {task.id: task for task in self.state.tasks(run_id)}
        first = culprit.get("Orch-Task", "")
        if first not in tasks or tasks[first].status != "DONE":
            self.gate_failure = f"regression at {commits[bad][:10]} is not a task merge"
            return True
        reopen = [first]
        for task in sorted(tasks.values(), key=lambda t: commits.index(t.commit)
                           if t.commit in commits else -1):
            if task.status == "DONE" and set(task.depends_on) & set(reopen):
                reopen.append(task.id)
        with self.merge_lock, self.git_lock:
            for task_id in sorted(reopen, key=lambda t: -commits.index(tasks[t].commit)):
                try:
                    self.git(integration, "revert", "--no-edit", tasks[task_id].commit)
                except RuntimeError as error:
                    self.git(integration, "revert", "--abort")
                    self.git(integration, "reset", "--hard", head)
                    self.gate_failure = f"revert of {task_id} conflicts: {error}"
                    return True
        evidence = f"regression found by the final gate (bisected to {commits[bad][:10]}):\n"
        self.state.reopen(
            run_id, first, reopen[1:], self.git(integration, "rev-parse", "HEAD"),
            {"commit": commits[bad]},
            level=1, last_error=(evidence + report)[-3000:],
            last_signature=signature("regression", report),
        )
        self.notify(f"[run] final gate: {first} broke tests; reverted {reopen}")
        return False

    def _promote(self, run_id: str) -> None:
        tasks = self.state.tasks(run_id)
        done = {task.id for task in tasks if task.status == "DONE"}
        unavailable = {task.id for task in tasks if task.status in {"PARKED", "REPLANNED"}}
        for task in tasks:
            required = set(task.depends_on)
            if not task.pending_candidate and self.early_interface_start:
                required -= set(task.interface_depends_on)
            if (task.status == "PLANNED" and required <= done
                    and not set(task.depends_on) & unavailable):
                early = sorted(set(task.depends_on) - done)
                self.state.transition(run_id, task.id, "READY", "task.ready", {
                    "interface_dependencies": early,
                })

    def _ready(self, run_id: str, busy: set[str]) -> list[OrchTask]:
        """READY tasks, longest remaining dependency chain first."""
        tasks = self.state.tasks(run_id)
        depth = chain_depths({task.id: task.depends_on for task in tasks})

        ready = [task for task in tasks if task.status == "READY" and task.id not in busy]
        return sorted(ready, key=lambda task: (-depth[task.id], task.id))

    def _finish(self, run_id: str) -> Run:
        tasks = self.state.tasks(run_id)
        if self.gate_failure:
            return self.state.update_run(
                run_id, "run.parked", {"reason": self.gate_failure},
                status="parked", message=self.gate_failure,
            )
        if self.stop_reason:
            return self.state.update_run(
                run_id, "run.paused", {"reason": self.stop_reason},
                status="paused", message=self.stop_reason,
            )
        if all(task.status in FINISHED for task in tasks):
            head = self.git(self.integration(run_id), "rev-parse", "HEAD")
            self.git(self.repo, "tag", "-f", f"orch/{run_id}/done", head)
            return self.state.update_run(
                run_id, "run.done", {"head": head}, status="done",
                message=f"all {sum(t.status == 'DONE' for t in tasks)} task(s) merged on "
                f"orch/{run_id}",
            )
        parked = [task.id for task in tasks if task.status == "PARKED"]
        done = {task.id for task in tasks if task.status == "DONE"}
        blocked = {task.id: [dep for dep in task.depends_on
                            if dep not in done]
                   for task in tasks if task.status == "PLANNED"}
        stranded = [task.id for task in tasks if task.status in IN_FLIGHT]
        return self.state.update_run(
            run_id, "run.parked", {"parked": parked, "blocked": blocked, "stranded": stranded},
            status="parked",
            message=f"{len(parked)} task(s) parked, {len(blocked)} blocked on dependencies, "
                    f"{len(stranded)} interrupted; "
                    "see `orch status`",
        )

    def _attempt(self, run_id: str, task: OrchTask) -> None:
        try:
            self._attempt_once(run_id, task)
        finally:
            self._generated(task.id)

    def _generated(self, task_id: str) -> None:
        with self.flow:
            if task_id in self.generating:
                self.generating.discard(task_id)
                self.slot_freed.set()

    def _attempt_once(self, run_id: str, task: OrchTask) -> None:
        number = task.attempts
        try:
            try:
                self._step(run_id, task)
            except Waiting as waiting:
                self._wait(run_id, task.id, number, waiting)
            except StaleInputs as changed:
                diff = self.git(self.repo, "diff", changed.candidate + "^", changed.candidate)
                self.state.refresh_inputs(run_id, task.id, number, changed.candidate,
                                          changed.revision, changed.changed, diff)
                self.notify(f"[run] {task.id} refreshes changed dependency contracts; "
                            "other tasks keep their artifacts")
            except AttemptFailed as failure:
                if self.cancel.is_set():
                    raise Deferred("interrupted while verifying") from None
                self._failed(run_id, task.id, number, task.level, failure)
        except Deferred as reason:
            if self.cancel.is_set():
                self.stop_reason = self.stop_reason or "interrupted by user"
            self._settle(run_id, task.id, number, "READY", "task.deferred", str(reason),
                         attempts=number - 1)
        except Exception as error:  # noqa: BLE001 - an infrastructure fault stays in this task
            if self.cancel.is_set() and not isinstance(error, Parked):
                # Ctrl-C: whatever broke while stopping is not the task's fault.
                self.stop_reason = self.stop_reason or "interrupted by user"
                self._settle(run_id, task.id, number, "READY", "task.deferred",
                             f"interrupted: {type(error).__name__}", attempts=number - 1)
                return
            detail = str(error) if isinstance(error, Parked) else (
                f"{type(error).__name__}: {error}"[:300]
            )
            self._settle(run_id, task.id, number, "PARKED", "task.parked", detail, reason=detail)
            self.notify(f"[run] {task.id} PARKED: {detail}")

    def _wait(self, run_id: str, task_id: str, number: int, waiting: Waiting) -> None:
        """Back to PLANNED behind the tasks its code imports; the attempt is not counted."""
        self.state.wait_for(run_id, task_id, number, waiting.owners, waiting.detail,
                            validate=dependency_errors)
        self.notify(f"[run] {task_id} {waiting.detail}; waits for {', '.join(waiting.owners)}")

    def _settle(
        self, run_id: str, task_id: str, number: int, status: str, kind: str, why: str,
        **changes: Any,
    ) -> None:
        """Last-resort transition from an exception handler; never crashes the run."""
        try:
            self.state.transition(
                run_id, task_id, status, kind, {"reason": why}, attempt=number, **changes
            )
        except Conflict as error:
            self.state.record(
                run_id, "task.handler_conflict", {"wanted": status, "error": str(error)},
                task_id, number,
            )

    def _step(self, run_id: str, task: OrchTask) -> None:
        """One attempt of one task: cheap first, cheap once more with its failure, then the
        supervisor decides (see orch_supervisor); level 3 is a strong-model attempt."""
        if task.level == 2 and not task.pending_candidate:
            self._supervise(run_id, task, "")
            return
        route = self._claude_route(run_id) if task.level == 3 else self._builder_route(task)
        try:
            self._build(run_id, task, route)
        except Signal as signal:
            # BLOCKED or TEST_DISPUTE: a planning problem the builder cannot solve alone.
            self._supervise(run_id, task, f"{signal.status}: {signal.reason}",
                            failure=signal.failure)

    def _claude(
        self, run_id: str, task: OrchTask, purpose: str, prompt: str, schema: dict[str, object],
    ) -> dict[str, object]:
        """One strong-model exception call; provider stops defer, other failures fail the rung."""
        route = self._claude_route(run_id)
        prefix = digest(prompt.shared)[:12] if isinstance(prompt, ContextPrompt) else ""
        self.state.record(
            run_id, "attempt.called", {"route": route.label, "purpose": purpose,
                                       "prefix": prefix, "bytes": len(prompt.encode())},
            task.id, task.attempts,
        )
        self.notify(f"[run] {task.id} a{task.attempts} -> {route.label} ({purpose})")
        call_id = f"orch-{run_id}-{task.id}-{purpose}-{uuid.uuid4().hex[:12]}"
        result = self._call_model(
            run_id, route, prompt, schema, call_id, purpose=purpose,
            task_id=task.id, attempt=task.attempts,
            scope="harden" if purpose == "harden" else "execution",
        )
        self.state.record(
            run_id, "attempt.result",
            {"route": route.label, "outcome": result.outcome, "usage": result.usage,
             "call_id": call_id},
            task.id, task.attempts,
        )
        self._stop_on(route, result)
        if result.outcome != "ok":
            raise AttemptFailed("provider", f"{purpose} {result.outcome}: {result.message}")
        return result.data

    def _stop_on(self, route: Route, result: NativeResult) -> None:
        """Provider-side outcomes defer the task; only rate limits keep the run going."""
        if unresolved(result.outcome, result.usage):
            self.stop_reason = self.stop_reason or f"{route.label} requires reconciliation"
            raise Parked("unresolved provider call; inspect `orch reconcile` before retry")
        if result.outcome not in STOP:
            return
        if result.outcome == "rate_limited":
            self._throttle(route.label)
        else:
            self.stop_reason = self.stop_reason or f"{route.label} {result.outcome}"
        raise Deferred(f"{route.label} {result.outcome}: {result.message[:160]}")

    def _keep_supervision(self, run_id: str, task: OrchTask, count: int, prompt: str,
                          answer: dict[str, object]) -> str:
        """The exact evidence a supervisor read and the answer it gave, kept beside the run
        (the call journal keeps only hashes). What the decision led to is in the events;
        scripts/export_decisions.py joins the two into decision -> outcome records."""
        folder = self.run_dir(run_id) / "supervision"
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{task.id}-s{count}-a{task.attempts}.json"
        shared = prompt.shared if isinstance(prompt, ContextPrompt) else ""
        (folder / name).write_text(dumps({
            "run": run_id, "task": task.id, "supervision": count, "attempt": task.attempts,
            "route": self._claude_route(run_id).label,
            "prompt": {"shared": shared, "dynamic": str(prompt)[len(shared):]},
            "answer": answer,
        }))
        return name

    def _revised_commands(
        self, run: Run, task: OrchTask, command_edits: list[object],
    ) -> tuple[list[str], list[dict[str, str]] | None]:
        """This task's check ids after the supervisor's search/replace edits to their
        commands, and the plan's components with the same edits applied to this task's
        component test command (None when the components did not change)."""
        tests = list(task.tests)
        if not command_edits:
            return tests, None
        declared = isinstance(run.plan.get("components"), list) and bool(run.plan["components"])
        entries = {str(t["id"]): t for t in cast(list[dict[str, object]], run.plan["tasks"])}
        mine = orch_generic.component_of(run.plan, entries.get(task.id, task.entry()))["id"]
        comps = [dict(c) for c in orch_generic.components(run.plan)] if declared else []
        changed = False
        for edit in command_edits:
            if (not isinstance(edit, dict) or set(edit) != {"search", "replace"}
                    or not all(isinstance(v, str) for v in edit.values()) or not edit["search"]):
                raise AttemptFailed("supervisor", "malformed command edit")
            search, replace = edit["search"], edit["replace"]
            hit = False
            revised = []
            for test in tests:
                check = orch_verify.parse(test)
                if check is not None and search in check.command:
                    hit = True
                    check = orch_verify.Check(check.command.replace(search, replace), check.files)
                    revised.append(check.id)
                else:
                    revised.append(test)
            tests = revised
            for comp in comps:
                if comp["id"] == mine and search in comp["test_command"]:
                    comp["test_command"] = comp["test_command"].replace(search, replace)
                    hit = changed = True
            if not hit:
                raise AttemptFailed("supervisor",
                                    f"command edit matches no check command: {search[:80]}")
        return tests, comps if changed else None

    def _fix_tests(
        self, run: Run, task: OrchTask, paths: list[str], edits: list[object],
        explanation: str, command_edits: list[object] | None = None,
    ) -> None:
        """Apply the supervisor's test revision (file edits, check command edits, or both)
        as a new test commit, gated like a merge: every check whose files changed (this
        task's or another's) must still run and fail on the stubs, and every merged task's
        checks must still pass."""
        with self.integration_change(run.id) as (integration, _):
            run = self.state.run(run.id)
            merged = [t for other in self.state.tasks(run.id) if other.status == "DONE"
                      for t in other.tests]
            changed_paths: list[str] = []
            if edits:
                try:
                    changed_paths = apply_edits(integration, paths, edits)
                except EditError as error:
                    raise AttemptFailed("supervisor",
                                        f"test revision unusable: {error}") from None
                # Only files whose bytes changed: an edit whose search and replace decode to
                # the same text (JSON "\u2028" is the character itself, not six characters)
                # used to reach `git commit` with nothing staged and park the task.
                before = plan_files(run.plan)
                changed_paths = [path for path in changed_paths
                                 if (integration / path).read_text() != before.get(path)]
            tests, components = self._revised_commands(run, task, command_edits or [])
            if edits and not changed_paths and tests == task.tests and components is None:
                raise AttemptFailed("supervisor", (
                    "the test revision changed nothing: every search text equals its replace "
                    "text once the JSON is decoded. To write a backslash escape such as "
                    "\\u2028 into a file, escape the backslash in JSON (\\\\u2028)."))
            files = {path: (integration / path).read_text() if path in paths else content
                     for path, content in plan_files(run.plan).items()}
            entries = {t.id: t.entry() for t in self.state.tasks(run.id)
                       if t.status != "REPLANNED"}
            entries[task.id]["tests"] = tests
            plan = {
                **run.plan,
                "files": [{"path": path, "content": content} for path, content in files.items()],
                "tasks": list(entries.values()),
                **({"components": components} if components is not None else {}),
            }
            errors = orch_generic.plan_errors(plan) if changed_paths or tests != task.tests \
                else []
            if errors:
                raise AttemptFailed("supervisor", f"test revision fails lint: {errors[:3]}")
            # The checks to re-prove red: this task's, and any other task's whose files the
            # revision touched. Stubs are the plan's non-test files, as at planning time.
            others = [t for other in self.state.tasks(run.id)
                      if other.id != task.id and other.status != "REPLANNED"
                      for t in other.tests
                      if set(orch_verify.files_of([t])) & set(changed_paths)]
            all_tests = set(orch_verify.files_of(
                [t for entry in entries.values()
                 for t in cast(list[str], entry["tests"])]))
            stubs: dict[str, object] = {"files": [{"path": p, "content": c}
                                                  for p, c in files.items() if p not in all_tests]}
            for test in dict.fromkeys([*tests, *others]):
                check = orch_verify.parse(test)
                if check is None:
                    continue
                problem = self._red_problem(
                    run.id, run.checkpoint, stubs,
                    [{"path": p, "content": files[p]} for p in check.files], check)
                if problem:
                    raise AttemptFailed("supervisor", f"revised tests on the stubs: {problem}")
            if components is not None:
                own = orch_generic.component_of(plan, entries[task.id])
                problems = self._suite_problems(run.id, run.checkpoint,
                                                {**plan, "components": [own]})
                if problems:
                    raise AttemptFailed("supervisor", f"revised test command: {problems[0]}")
            hashed = plan_hash(plan)
            if changed_paths:
                self.git(integration, "add", "-f", "--", *changed_paths)
                message = commit_message(
                    run.id, hashed, task, f"a{task.attempts}t", self._claude_route(run.id).label,
                    title=f"orch tests {task.id}: {explanation}",
                )
                self.git(integration, "commit", "-m", message,
                         author=f"orch {self._claude_route(run.id).label}")
            head = self.git(integration, "rev-parse", "HEAD")
            if merged and changed_paths:
                passed, report = self._checks_at(run.id, "gate", head, merged)
                if not passed:
                    # Judgment, not the end: the revision is rolled back (integration_change)
                    # and the supervisor decides again with this report in hand.
                    raise AttemptFailed("supervisor", "the test revision breaks merged "
                                        f"tasks' checks: {report[-1500:]}")
            current = self.state.task(run.id, task.id)
            saved = current.pending_candidate or current.failed_candidate
            level = 0
            if saved:
                try:
                    identity = self._candidate_identity(run, current, saved)
                    level = 3 if identity["Orch-Model"] == self._claude_route(run.id).label else 0
                except (Parked, RuntimeError) as error:
                    self.state.record(run.id, "task.candidate_discarded", {
                        "commit": saved, "reason": str(error),
                    }, task.id, task.attempts)
                    saved = ""
            self.state.revise_tests(
                run.id, task.id, plan, head, expected_plan=run.plan_hash,
                attempt=task.attempts, candidate=saved, level=level, tests=tests,
            )
        self.notify(f"[run] {task.id} tests revised; "
                    + ("reverify saved code" if saved else "no eligible saved code"))

    def _replan(self, run_id: str, task: OrchTask, entries: object, summary: str) -> None:
        """Replace the task (and the tasks that depend on it) by the supervisor's tasks."""
        tasks = self.state.tasks(run_id)
        run = self.state.run(run_id)
        chosen = subtree(tasks, task.id, [])
        if not isinstance(entries, list) or not entries:
            raise AttemptFailed("supervisor", "split returned no tasks")
        errors = self._replan_errors(run, tasks, chosen, entries)
        if errors:
            raise AttemptFailed("supervisor", "split rejected: " + "; ".join(errors[:3]))
        replacements = [
            replace(new, generation=task.generation + 1) for new in tasks_from({"tasks": entries})
        ]
        try:
            with self.merge_lock:
                # Parallel supervisors can choose the same ids from an earlier snapshot.
                current = self.state.tasks(run_id)
                if {new.id for new in replacements} & {other.id for other in current}:
                    first = next_task_id(current)
                    renamed = {new.id: f"T-{first + i:03d}" for i, new in enumerate(replacements)}
                    replacements = [replace(
                        new, id=renamed[new.id],
                        depends_on=[renamed.get(dep, dep) for dep in new.depends_on],
                        interface_depends_on=[renamed.get(dep, dep)
                                              for dep in new.interface_depends_on],
                    ) for new in replacements]
                self.state.replan(run_id, task.id, chosen, replacements, summary[:300],
                                  validate=orch_generic.plan_errors)
        except Conflict as error:
            raise AttemptFailed("supervisor", f"split raced with execution: {error}") from None
        self.notify(f"[run] {', '.join(chosen)} split into {[t.id for t in replacements]}")

    def _replan_errors(
        self, run: Run, tasks: list[OrchTask], chosen: list[str], entries: list[Any],
    ) -> list[str]:
        """Mechanical reasons a replacement cannot stand in for the chosen tasks: plan
        structure (ownership, dependencies, checks), the same checks, fresh ids."""
        kept = [t.entry() for t in tasks if t.status != "REPLANNED" and t.id not in chosen]
        plan = {**run.plan, "tasks": [*kept, *entries]}
        errors = orch_generic.plan_errors(plan)
        wanted = {test for t in tasks if t.id in chosen for test in t.tests}
        given = {test for e in entries if isinstance(e, dict) for test in e.get("tests", [])}
        taken = {t.id for t in tasks}
        errors += ["replacement tests differ from the replaced tasks"] if wanted != given else []
        errors += [
            f"id {e.get('id')} reused" for e in entries
            if isinstance(e, dict) and e.get("id") in taken
        ]
        return errors

    def _supervise(self, run_id: str, task: OrchTask, signal: str, *,
                   failure: AttemptFailed | None = None) -> None:
        """Judgment after cheap attempts failed: evidence in, one action out."""
        if task.supervisions >= MAX_SUPERVISIONS:
            raise Parked(f"{MAX_SUPERVISIONS} supervisor decisions did not resolve it: "
                         + (task.last_error or signal)[-200:])
        run = self.state.run(run_id)
        tasks = self.state.tasks(run_id)
        context = RunContext.of(run, tasks)
        with self.git_lock:
            head = self.git(self.integration(run_id), "rev-parse", "HEAD")
        candidate = task.pending_candidate or task.failed_candidate
        owned: dict[str, str] = {}
        for path in task.files:
            for revision in (candidate, head):
                try:
                    owned[path] = Workspace.git(self.repo, "show", f"{revision}:{path}",
                                                strip=False) if revision else ""
                    break
                except subprocess.CalledProcessError:
                    continue
        attempts = self._attempt_history(run_id, task)
        prompt = supervise_prompt(
            spec=run.spec, task=task, requirements=context.requirements,
            tests=context.tests_for(task), owned=owned, attempts=attempts,
            failure=failure.detail if failure is not None else task.last_error,
            diff=failure.diff if failure is not None else task.last_diff, signal=signal,
            all_tasks=tasks, chosen=subtree(tasks, task.id, []), first_id=next_task_id(tasks))
        investigations: list[tuple[str, str]] = []
        while True:
            data = self._claude(run_id, task, "supervise", prompt, SUPERVISOR_SCHEMA)
            action = str(data.get("action", ""))
            if action != "investigate" or len(investigations) >= MAX_INVESTIGATIONS:
                break
            command = str(data.get("command", "")).strip()
            if not command:
                break
            # Evidence on demand: the command runs in the sandbox on the latest candidate's
            # tree (read-only to everything but its own scratch); only its output comes back.
            with self.worktree(run_id, "gate", candidate or head) as tree:
                result = orch_verify.run_command(tree, command, orch_verify.MIN_TIMEOUT)
            output = f"exit {result.code}\n{result.output}"
            investigations.append((command, output))
            self.state.record(run_id, "task.investigated", {
                "command": command[:500], "exit": result.code, "output": output[-4000:],
            }, task.id, task.attempts)
            self.notify(f"[run] {task.id} supervisor investigates: {command[:120]}")
            prompt = supervise_prompt(
                spec=run.spec, task=task, requirements=context.requirements,
                tests=context.tests_for(task), owned=owned, attempts=attempts,
                failure=failure.detail if failure is not None else task.last_error,
                diff=failure.diff if failure is not None else task.last_diff, signal=signal,
                all_tasks=tasks, chosen=subtree(tasks, task.id, []),
                first_id=next_task_id(tasks), investigations=investigations)
        if action == "investigate":  # out of investigations: nothing was decided
            raise AttemptFailed("supervisor", "the supervisor kept investigating without "
                                "deciding")
        explanation = str(data.get("explanation", ""))[:2000]
        guidance = str(data.get("guidance", "")).strip()
        count = task.supervisions + 1
        record = self._keep_supervision(run_id, task, count, prompt, data)
        self.state.record(run_id, "task.supervised", {
            "action": action, "explanation": explanation, "guidance": guidance[:2000],
            "supervision": count, "record": record,
        }, task.id, task.attempts)
        self.notify(f"[run] {task.id} supervisor: {action} - {explanation[:160]}")
        if action in {"retry", "escalate"}:
            note = (f"{task.guidance}\n- {guidance}" if task.guidance else f"- {guidance}") \
                if guidance else task.guidance
            self.state.transition(
                run_id, task.id, "READY", "task.guided", {"action": action, "status": "READY"},
                attempt=task.attempts, level=3 if action == "escalate" else 1,
                supervisions=count, guidance=note, pending_candidate="")
            return
        if action == "revise_contract":
            contract = str(data.get("contract", "")).strip()
            if not contract:
                raise AttemptFailed("supervisor", "revise_contract without a contract")
            self.state.transition(
                run_id, task.id, "READY", "task.contract_revised",
                {"contract_bytes": len(contract), "status": "READY"}, attempt=task.attempts,
                level=1, supervisions=count, contract=contract, pending_candidate="",
                guidance=(f"{task.guidance}\n- {guidance}" if task.guidance and guidance
                          else guidance or task.guidance))
            return
        if action == "revise_tests":
            edits = data.get("test_edits")
            commands = data.get("command_edits")
            # Any task's test file: a broken test file of another task can break this task's
            # checks (one build, one package). Builders never edit tests; the supervisor
            # revises them for the designers, and every revised task is re-checked below.
            paths = sorted(orch_verify.files_of(
                [t for other in tasks if other.status != "REPLANNED" for t in other.tests]))
            edits = edits if isinstance(edits, list) else []
            commands = commands if isinstance(commands, list) else []
            if not edits and not commands:
                raise AttemptFailed("supervisor", "revise_tests without edits")
            self.state.transition(run_id, task.id, task.status, "task.supervised_tests",
                                  {"status": task.status}, attempt=task.attempts,
                                  supervisions=count)
            self._fix_tests(run, task, paths, edits, explanation[:120], commands)
            return
        if action == "split":
            self.state.transition(run_id, task.id, task.status, "task.supervised_split",
                                  {"status": task.status}, attempt=task.attempts,
                                  supervisions=count)
            self._replan(run_id, task, data.get("tasks"), explanation)
            return
        self.state.transition(run_id, task.id, task.status, "task.supervised_stop",
                              {"status": task.status}, attempt=task.attempts, supervisions=count)
        if action == "environment":
            raise Parked("environment: " + explanation[:300])
        raise Parked("supervisor: " + (explanation[:300] or "no automatic action left"))

    def _attempt_history(self, run_id: str, task: OrchTask) -> list[dict[str, object]]:
        """What each attempt did and how it ended, for the supervisor."""
        items: dict[int, dict[str, object]] = {}
        for event in self.state.events(run_id, task.id):
            match = re.search(r"/a(\d+)$", event.span)
            if not match:
                continue
            n = int(match.group(1))
            item = items.setdefault(n, {"label": f"a{n}", "route": "", "rung": "",
                                        "outcome": "", "category": "", "summary": ""})
            if event.kind == "attempt.called" and not item["route"]:
                item["route"] = event.data.get("route", "")
                item["rung"] = {0: "first try", 1: "retry", 3: "strong model"}.get(
                    cast(int, event.data.get("level", 0)), str(event.data.get("purpose", "")))
            elif event.kind == "attempt.summary":
                item["summary"] = event.data.get("summary", "")
            elif event.kind in {"task.failed", "task.parked"}:
                item["outcome"] = "failed"
                item["category"] = event.data.get("category", "")
            elif event.kind == "task.done":
                item["outcome"] = "passed"
        return [items[n] for n in sorted(items)]

    def _build(self, run_id: str, task: OrchTask, route: Route) -> None:
        run = self.state.run(run_id)
        context = RunContext.of(run, self.state.tasks(run_id))
        with self.git_lock:
            base = self.git(self.integration(run.id), "rev-parse", "HEAD")
        label = f"a{task.attempts}"
        with self.worktree(run.id, f"{task.id}-{label}", base) as path:
            made: Candidate | AttemptFailed | Deferred | Signal
            if task.pending_candidate:
                made = self._reuse_candidate(run, task, route, base, path)
            else:
                made = self._generate(run, context, task, route, base, path, label, "")
            self._generated(task.id)  # from here on the task only waits for sandbox runs
            if not isinstance(made, Candidate):
                raise made
            candidate = made
            self.state.transition(run.id, task.id, "RUNNING", "task.candidate", {
                "commit": candidate.commit,
            }, attempt=task.attempts, pending_candidate=candidate.commit)
            self.state.transition(
                run.id, task.id, "VERIFYING", "task.verifying",
                {"candidates": [candidate.label]}, attempt=task.attempts,
            )
            error = self._check(run, context, task, base, candidate, route)
            if error is not None:
                raise error
            self.state.transition(
                run.id, task.id, "MERGING", "task.merging",
                {"passing": [candidate.label]}, attempt=task.attempts,
            )
            self.state.transition(run.id, task.id, "MERGING", "task.candidate", {
                "commit": candidate.commit,
            }, attempt=task.attempts, pending_candidate=candidate.commit)
            self._merge(run, task, candidate, route)

    def _candidate_identity(self, run: Run, task: OrchTask, commit: str) -> dict[str, str]:
        """One provenance gate for both selecting and replaying saved code."""
        if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
            raise Parked("Saved candidate is not an immutable commit ID")
        trailers = parse_trailers(self.git(self.repo, "show", "-s", "--format=%B", commit))
        expected = {"Orch-Run": run.id, "Orch-Task": task.id,
                    "Orch-Contract": contract_hash(task)}
        parents = self.git(self.repo, "rev-list", "--parents", "-n", "1", commit).split()
        if (any(key not in trailers for key in REQUIRED_TRAILERS)
                or any(trailers.get(key) != value for key, value in expected.items())
                or trailers.get("Orch-Model") not in {DSF.label, self._claude_route(run.id).label,
                                                      HUMAN.label,
                                                      self._builder_route(task).label}
                or len(parents) != 2):
            raise Parked("Saved candidate no longer matches task contract or route")
        changed = set(self.git(self.repo, "diff", "--name-only", parents[1], commit).splitlines())
        if changed - set(task.files):  # an unchanged tree is fine: the checks decide
            raise Parked("Saved candidate changes files outside its implementation ownership")
        if task.interface_depends_on or "Orch-Inputs" in trailers:
            current = self.state.run(run.id)
            context = RunContext.of(current, self.state.tasks(run.id))
            contracts = context.dependency_contracts(task, current.base)
            expected_inputs = digest(dumps(contracts))
            if trailers.get("Orch-Inputs") != expected_inputs:
                observed = [event.data.get("dependency_contracts", {})
                            for event in self.state.events(run.id, task.id)
                            if event.kind == "attempt.called"
                            and event.data.get("sample") == trailers["Orch-Attempt"]]
                previous = observed[-1] if observed else {}
                previous = previous if isinstance(previous, dict) else {}
                versions = {tid: cast(dict[str, object], value)["version"]
                            for tid, value in contracts.items()}
                changed_dependencies = sorted(tid for tid in {*previous, *versions}
                                              if previous.get(tid) != versions.get(tid))
                raise StaleInputs(commit, expected_inputs, changed_dependencies or sorted(versions))
        return trailers

    def _reuse_candidate(
        self, run: Run, task: OrchTask, route: Route, base: str, path: Path,
    ) -> "Candidate":
        """Replay an immutable code artifact; dependencies travel by commit, never by chat."""
        original = task.pending_candidate
        trailers = self._candidate_identity(run, task, original)
        if trailers["Orch-Model"] != route.label:
            raise Parked("Saved candidate no longer matches task contract or route")
        try:
            with self.git_lock:
                self.git(path, "cherry-pick", "--allow-empty", original)
        except RuntimeError as error:
            self.git(path, "cherry-pick", "--abort")
            raise AttemptFailed("rebase", str(error)) from None
        commit = self.git(path, "rev-parse", "HEAD")
        label = trailers["Orch-Attempt"]
        self.git(self.repo, "update-ref", f"refs/orch/{run.id}/{task.id}/{label}", commit)
        self.state.record(run.id, "task.candidate_reused", {
            "source": original, "commit": commit, "base": base,
        }, task.id, task.attempts)
        self._count("candidate_reuses")
        return Candidate(commit, self.git(path, "diff", base, commit), label, path)

    def _builder_pack(
        self, run: Run, context: "RunContext", task: OrchTask, route: Route,
        base: str, path: Path, hint: str = "", feedback: str = "",
    ) -> Pack:
        """Facts and evidence for one attempt. Context the task asked for (and the files of
        the tasks it builds on) is answered again at `base`; when it does not fit, the
        oldest requests are dropped first and the task may ask again."""
        selectors = list(dict.fromkeys([*self._dependency_files(context, task, base),
                                        *task.context_selectors]))
        owned = self.repo_view.read(base, task.files)
        arguments: tuple[Any, Any, Any, Any, Any, Any, Any, Any, Any] = (
            task, context.requirements, run.repo_map,
            context.interfaces_for(task), owned,
            context.tests_for(task),
            (DSF_BUDGET if route == DSF else CLAUDE_BUDGET),
            (task.last_diff, feedback or task.last_error),
            "\n".join(part for part in (
                ("HUMAN GUIDANCE for this task (authoritative over earlier attempts):\n"
                 + task.human_hint) if task.human_hint else "",
                ("SUPERVISOR GUIDANCE after earlier attempts failed:\n" + task.guidance)
                if task.guidance else "", hint) if part),
        )
        contracts = context.dependency_contracts(task, run.base)
        while True:
            requested: dict[str, str] = {}
            for selector in selectors:
                try:
                    requested[selector] = self.repo_view.answer(base, [json.loads(selector)])
                except (ValueError, TypeError):
                    continue
            try:
                return replace(context_pack(*arguments, requested=requested,
                                            original_spec=run.spec,
                                            language_note=self._language_note(),
                                            dependency_contracts=contracts),
                               requested=tuple(selectors))
            except ContextTooLarge:
                if not selectors:
                    raise
                selectors = selectors[1:]

    def _dependency_files(self, context: "RunContext", task: OrchTask, base: str) -> list[str]:
        """Source files owned by the tasks this task builds on, as read selectors (at most
        DEPENDENCY_READS); none when there are more, so a broad fan-in stays on demand."""
        paths = sorted({
            path for tid in task.depends_on if tid in context.tasks
            for path in context.tasks[tid].files
            if path not in task.files
        })
        present = set(self.repo_view.tree(base))
        paths = [path for path in paths if path in present]
        return [dumps({"read": path}) for path in paths] if len(paths) <= DEPENDENCY_READS \
            else []

    def _generate(
        self, run: Run, context: "RunContext", task: OrchTask, route: Route, base: str,
        path: Path, label: str, hint: str,
    ) -> "Candidate | AttemptFailed | Deferred | Signal":
        try:
            context_feedback = ""
            repairs = 0
            call_route = route
            # A task that owns no files (a cross-component agreement check) has nothing to
            # build: its frozen checks alone decide, on the merged dependencies. Run
            # r7313527a parked such a task after five rungs of builders correctly
            # returning no edits.
            status: object = "DONE"
            answer: dict[str, object] = {}
            if not task.files:
                self.notify(f"[run] {task.id} owns no files; its checks decide")
            while task.files:
                if self.cancel.is_set() or self.stop_reason:
                    raise Deferred(self.stop_reason or "interrupted during context retrieval")
                if self.max_cents and self.spent >= self.max_cents:
                    raise Deferred("run spend cap reached during context retrieval")
                current = self.state.task(run.id, task.id)
                try:
                    pack = self._builder_pack(
                        run, context, current, route, base, path, hint, context_feedback,
                    )
                except ContextTooLarge as error:
                    raise AttemptFailed("context", str(error)) from None
                except ValueError as error:
                    raise Parked(str(error)) from None
                prompt = ContextPrompt(pack.shared, pack.task + (
                    (pack.retry or "\nImplement the task.\n") if route.provider == "claude"
                    else pack.retry))
                prefix = digest(prompt.shared)[:12]
                self.state.record(
                    run.id, "attempt.called",
                    {"route": route.label, "prefix": prefix, "level": task.level,
                     "sample": label,
                     "bytes": len(prompt.encode()),
                     "context_requests": current.context_requests,
                     "dependency_contracts": {
                         tid: cast(dict[str, object], contract)["version"]
                         for tid, contract
                         in context.dependency_contracts(task, run.base).items()}},
                    task.id, task.attempts,
                )
                self.notify(f"[run] {task.id} {label} -> {route.label} (level {task.level})")
                if route == DSF and self.bucket is not None and not self.bucket.take(
                    lambda: self.cancel.is_set() or bool(self.stop_reason)
                ):
                    raise Deferred("interrupted while waiting for the request rate")
                call_id = f"orch-{run.id}-{task.id}-{label}-{uuid.uuid4().hex[:12]}"
                # DSF uses the widest output budget the call reservation covers for this request.
                dispatch = (DSF_BUILD_FIRST if route == DSF and task.level == 0
                            and task.attempts == 1 else route)
                call_route = self._widened(dispatch, prompt, EDIT_SCHEMA) or dispatch
                result = self._call_model(
                    run.id, call_route, prompt,
                    PATCH_SCHEMA if route.provider == "claude" else EDIT_SCHEMA,
                    call_id, purpose="patch" if route.provider == "claude" else "build",
                    task_id=task.id, attempt=task.attempts, sample=label,
                    prefix=prefix, base=base,
                )
                self.state.record(
                    run.id, "attempt.result",
                    {"route": route.label, "outcome": result.outcome, "usage": result.usage,
                     "sample": label, "call_id": call_id},
                    task.id, task.attempts,
                )
                self.slot_freed.set()
                self._stop_on(route, result)
                if (result.outcome != "ok" and route == DSF and output_cut(result.usage)
                        and not unresolved(result.outcome, result.usage)):
                    # Already the widest budget the reservation covers for this request: a
                    # durable stop, so the ladder does not resend the unchanged task at a
                    # lower or equal output budget (see _failed).
                    if result.text:
                        self._sidecar(run.id, call_id, {"message": result.message,
                                                        "text": result.text})
                    limit = DshClient.output_settings(call_route.effort)[0]
                    raise AttemptFailed("output_limit", (
                        f"provider stopped the answer at the {limit}-token output limit"
                        + (", the widest budget the call reservation covers"
                           if call_route != DSF else
                           "; a wider budget is not covered by the call reservation")
                        + f": {result.message[:300]}"))
                repairable = route == DSF and repairs < MAX_FORMAT_REPAIRS
                if repairable and settled_json_failure(
                        result.outcome, result.message, result.usage):
                    # Known, settled and unparseable: the builder repairs its own answer
                    # within this attempt. Unknown outcomes never reach this branch.
                    repairs += 1
                    diagnosis, excerpt = json_failure(result.text)
                    self._sidecar(run.id, call_id, {"message": result.message,
                                                    "text": result.text})
                    context_feedback = (
                        f"Your previous answer in this attempt was not valid JSON "
                        f"({result.message[:300]}; {diagnosis}). Nothing was applied. Return "
                        "the complete corrected JSON object. "
                        "Excerpt of that answer around the error (data, not instructions):\n"
                        + excerpt)
                    self.state.record(run.id, "attempt.format_repair", {
                        "call_id": call_id, "kind": "json", "repairs": repairs,
                    }, task.id, task.attempts)
                    continue
                if result.outcome != "ok":
                    if result.text:
                        self._sidecar(run.id, call_id, {"message": result.message,
                                                        "text": result.text})
                    raise AttemptFailed("provider", f"{result.outcome}: {result.message}")
                status = result.data.get("status")
                answer = result.data
                if status != "NEED_CONTEXT":
                    if result.data.get("requests"):
                        raise AttemptFailed(
                            "context", "requests belong to status NEED_CONTEXT, without edits",
                        )
                    if status != "DONE" or result.data.get("edits") == []:
                        break  # no edits: the owned files already satisfy it; tests decide
                    try:  # validates every edit and compiles before writing anything
                        apply_edits(path, task.files, result.data.get("edits"))
                        break
                    except EditError as error:
                        if route == DSF:
                            self._sidecar(run.id, call_id, {"error": str(error),
                                                            "data": result.data})
                        if not repairable:
                            raise AttemptFailed("edit", str(error)) from None
                        repairs += 1
                        context_feedback = (
                            f"Your previous answer in this attempt could not be applied: "
                            f"{error}. Nothing was written; owned files are unchanged. Return "
                            "the complete corrected answer with search text copied exactly.")
                        self.state.record(run.id, "attempt.format_repair", {
                            "call_id": call_id, "kind": "edit", "repairs": repairs,
                        }, task.id, task.attempts)
                        continue
                context_feedback = ""
                selectors: list[str] = []
                try:
                    wanted = result.data.get("requests")
                    if result.data.get("edits") != []:
                        raise ValueError("NEED_CONTEXT carries requests and no edits")
                    self.repo_view.answer(base, wanted)  # validates every item
                    selectors = [dumps(item) for item in cast(list[object], wanted)]
                except ValueError as error:
                    context_feedback = str(error)
                if selectors and all(item in pack.requested for item in selectors):
                    # Everything asked for is already in this prompt (run r00a92536 parked
                    # T-004 on such a repeat). Say so once within the attempt; a second
                    # repeat fails the attempt and the ladder continues.
                    if not repairable:
                        # "edit", not "context": nothing is oversized, the answer was unusable.
                        raise AttemptFailed("edit", (
                            "NEED_CONTEXT repeated context already supplied: " + dumps(selectors)))
                    repairs += 1
                    context_feedback = (
                        "Everything you asked for is already shown above under CONTEXT YOU "
                        "ASKED FOR. Do not request it again; return DONE with your edits, or "
                        "BLOCKED/TEST_DISPUTE with a reason.")
                    self.state.record(run.id, "attempt.format_repair", {
                        "call_id": call_id, "kind": "context", "repairs": repairs,
                    }, task.id, task.attempts)
                    continue
                try:
                    self.state.request_context(
                        run.id, task.id, selectors, base, context_feedback, call_id=call_id,
                        observed_requests=current.context_requests,
                    )
                except Conflict as error:
                    # A spent context budget is this attempt's failure, not the task's end:
                    # the cheap retry and then the supervisor see it like any other failure
                    # (run r5f4fdad4 parked a Meridian task here, bypassing both).
                    raise AttemptFailed("edit", (
                        f"{error}; what was asked for so far is shown under CONTEXT YOU ASKED "
                        "FOR. Build from it: return DONE with edits, or BLOCKED with what is "
                        "missing.")) from None
            if status in {"BLOCKED", "TEST_DISPUTE"}:
                raise Signal(str(status), str(answer.get("reason", "")))
            if status != "DONE":
                raise AttemptFailed("edit", "Expected DONE, NEED_CONTEXT, BLOCKED or TEST_DISPUTE")
            if answer.get("summary") or answer.get("lesson"):
                self.state.record(run.id, "attempt.summary", {
                    "sample": label, "route": route.label,
                    "summary": str(answer.get("summary") or answer.get("lesson"))[:2000],
                }, task.id, task.attempts)
            # The sandbox's scratch tree and linked dependencies are never part of a change.
            self.git(path, "add", "-A", "--", ".",
                     *orch_verify.excludes(path))
            present = [owned for owned in task.files if (path / owned).exists()]
            if present:  # owned files may sit under a .gitignore pattern
                self.git(path, "add", "-f", "--", *present)
            # An unchanged tree is a candidate too: the frozen checks decide whether the
            # task was already satisfied.
            diff = self.git(path, "diff", "--cached", base)
            message = commit_message(
                run.id, run.plan_hash, task, label, route.label,
                inputs=(digest(dumps(context.dependency_contracts(task, run.base)))
                        if task.interface_depends_on else ""),
            )
            self.git(path, "commit", "--allow-empty", "-m", message, author=f"orch {route.label}")
            commit = self.git(path, "rev-parse", "HEAD")
            # Every attempt stays addressable as evidence after its worktree is removed.
            with self.git_lock:
                self.git(self.repo, "update-ref", f"refs/orch/{run.id}/{task.id}/{label}", commit)
            return Candidate(commit, diff, label, path)
        except (AttemptFailed, Deferred, Signal) as outcome:
            return outcome

    def _sidecar(self, run_id: str, call_id: str, payload: dict[str, object]) -> None:
        """Keep a settled answer that could not be used, for audit and exact offline replay."""
        folder = self.run_dir(run_id) / "responses"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{call_id}.json").write_text(dumps(payload))

    def _owners(self, run_id: str) -> tuple[dict[str, OrchTask], dict[str, str]]:
        tasks = {t.id: t for t in self.state.tasks(run_id) if t.status != "REPLANNED"}
        return tasks, {path: t.id for t in tasks.values() for path in t.files}

    def _check(
        self, run: Run, context: "RunContext", task: OrchTask, base: str,
        candidate: "Candidate", route: Route,
    ) -> "AttemptFailed | Signal | None":
        path, commit, diff = candidate.path, candidate.commit, candidate.diff
        changed = set(self.git(path, "diff", "--name-only", base, commit).splitlines())
        outside = sorted(changed - set(task.files))
        if outside:
            return AttemptFailed("scope", f"changed files outside ownership: {outside}", diff)
        current = self.repo_view.read(commit, task.files)
        missing = [owned for owned in task.files if owned in context.stubs and owned not in current]
        if missing:
            return AttemptFailed("interface", f"missing implementation files: {missing}", diff)
        if task.interface_depends_on:
            tasks, _ = self._owners(run.id)
            waiting = [tid for tid in task.depends_on
                       if tasks[tid].status != "DONE" or not tasks[tid].commit
                       or self.git(self.repo, "merge-base", tasks[tid].commit, commit)
                       != tasks[tid].commit]
            if waiting:
                return Waiting(waiting, "await dependency implementations in snapshot", diff)
        if self.git(path, "status", "--porcelain"):
            return AttemptFailed("scope", "worktree is not clean after commit", diff)
        passed, report = self.verify(path, self.scratch(run.id), commit, task.tests)
        if not passed:
            return AttemptFailed("test", report[-4000:], diff)
        return None

    def _merge(self, run: Run, task: OrchTask, candidate: "Candidate", route: Route) -> None:
        """Cherry-pick onto the integration branch and gate on every merged task's tests."""
        commit, diff = candidate.commit, candidate.diff
        message = self.git(self.repo, "show", "-s", "--format=%B", commit)
        missing = [key for key in REQUIRED_TRAILERS if key not in parse_trailers(message)]
        if missing:
            raise AttemptFailed("trailer", f"commit lacks trailers {missing}", diff)
        with self.integration_change(run.id) as (integration, before):
            self._candidate_identity(self.state.run(run.id),
                                     self.state.task(run.id, task.id), commit)
            done_tests = [
                (other.id, test)
                for other in self.state.tasks(run.id)
                if other.status == "DONE"
                for test in other.tests
            ]
            try:
                with self.git_lock:
                    self.git(integration, "cherry-pick", "--allow-empty", commit)
            except RuntimeError as error:
                with self.git_lock:
                    self.git(integration, "cherry-pick", "--abort")
                self._congest()
                if task.rebases + 1 > MAX_REBASES:
                    # Rebuild on the current integration branch rather than park: the cheap
                    # retry, then the supervisor, see the conflict like any failure.
                    raise AttemptFailed("merge", f"merge conflict after {MAX_REBASES} "
                                        f"rebases: {error}") from None
                self.state.transition(
                    run.id, task.id, "READY", "task.rebase", {"error": str(error)[:300]},
                    attempt=task.attempts, rebases=task.rebases + 1,
                )
                return
            merged = self.git(integration, "rev-parse", "HEAD")
            changed = self.git(integration, "diff", "--name-only", before, merged).splitlines()
            scope = self._gate_scope(run.id, task, changed)
            merged_tests = [test for owner, test in done_tests if owner in scope]
            affected = merged_tests  # the gate's scope is the dependency/component choice
            self.state.record(
                run.id, "task.gate",
                {"tests": len(affected) + len(task.tests), "skipped": len(done_tests)
                 - len(affected)},
                task.id, task.attempts,
            )
            passed, report = self._checks_at(run.id, "gate", merged,
                                             [*affected, *task.tests])
            reopen: list[str] = []
            if not passed and task.reopened_by:
                reopen = self._ripple_targets(run.id, task, report, [*affected, *task.tests])
                passed = bool(reopen)
            if not passed:
                raise AttemptFailed("integration", report, diff)
            self.state.record(
                run.id, "attempt.outcome",
                {"route": route.label, "level": task.level, "passed": True},
                task.id, task.attempts,
            )
            self.state.transition(
                run.id, task.id, "DONE", "task.done",
                {"commit": merged, "candidate": candidate.label, "implicit": []},
                attempt=task.attempts, verified_head=merged, commit=merged, reason="",
                pending_candidate="", failed_candidate="", reopened_by="",
            )
            if task.reopened_by:
                self._ripple(run.id, task, reopen, report)
        self._relax()
        self.notify(f"[run] {task.id} DONE at {merged[:10]} ({candidate.label})")

    # --- a rebuilt DONE task ripples to its dependents --------------------------------
    def _descendants(self, run_id: str, task_id: str) -> set[str]:
        tasks = self.state.tasks(run_id)
        found: set[str] = set()
        frontier = {task_id}
        while frontier:
            frontier = {t.id for t in tasks if set(t.depends_on) & frontier} - found
            found |= frontier
        return found

    def _ripple_targets(self, run_id: str, task: OrchTask, report: str,
                        names: list[str]) -> list[str]:
        """Dependents to reopen instead of rejecting a rebuilt task's merge: only when every
        failing check belongs to a DONE task downstream of it, and none of its own fail."""
        failing = orch_verify.failed(report, names)
        owner = {test: other.id for other in self.state.tasks(run_id)
                 if other.status == "DONE" for test in other.tests}
        downstream = self._descendants(run_id, task.id)
        if not failing or any(test in task.tests or owner.get(test) not in downstream
                              for test in failing):
            return []
        return sorted({owner[test] for test in failing})

    def _ripple(self, run_id: str, task: OrchTask, reopen: list[str], report: str) -> None:
        """After a rebuilt task merged: dependents whose checks now fail are reopened with
        the failure as context; the others are recorded as unaffected (no model call)."""
        for other in self.state.tasks(run_id):
            if other.id not in self._descendants(run_id, task.id) or other.status != "DONE":
                continue
            if other.id in reopen:
                note = (f"Upstream {task.id} was rebuilt and this task's frozen checks now fail "
                        "on the merged code; adapt this task's own files to it.")
                self.state.transition(
                    run_id, other.id, "READY", "task.ripple",
                    {"from": task.id, "affected": True, "status": "READY"},
                    attempt=other.attempts, reopened_by=task.id, level=0,
                    rebases=0, last_error=report[-4000:], reason="",
                    human_hint=(other.human_hint + "\n- " if other.human_hint else "- ") + note,
                )
                self.notify(f"[run] {other.id} reopened: upstream {task.id} changed")
            else:
                self.state.record(run_id, "task.ripple", {"from": task.id, "affected": False},
                                  other.id)

    # --- a person takes over a task ---------------------------------------
    def manual_path(self, run_id: str, task_id: str) -> Path:
        return self.run_dir(run_id) / "manual" / task_id

    def take(self, run_id: str, task_id: str) -> Path:
        """Check out a task's best code for a person to finish, at the integration HEAD.

        The task is parked so `orch run` will not rebuild it meanwhile; its owned files hold
        the latest candidate when one exists, else the frozen stubs.
        """
        with self.exclusive(run_id):
            task = self.state.task(run_id, task_id)
            if task.status in {"DONE", "REPLANNED"}:
                raise PlanError(f"{task_id} is {task.status}; nothing to take over")
            path = self.manual_path(run_id, task_id)
            if path.exists():
                return path
            head = self.git(self.integration(run_id), "rev-parse", "HEAD")
            path.parent.mkdir(parents=True, exist_ok=True)
            with self.git_lock:
                self.git(self.repo, "worktree", "add", "--detach", str(path), head)
            orch_verify.link_ignored(self.repo, path)
            candidate = task.pending_candidate or task.failed_candidate
            if candidate:
                for name in task.files:
                    try:
                        content = Workspace.git(self.repo, "show", f"{candidate}:{name}",
                                                strip=False)
                    except subprocess.CalledProcessError:
                        continue  # a file the candidate did not create
                    (path / name).parent.mkdir(parents=True, exist_ok=True)
                    (path / name).write_text(content)
            if task.status != "PARKED":
                self.state.transition(run_id, task_id, "PARKED", "task.parked",
                                      {"reason": "taken over by a person"},
                                      attempt=task.attempts, reason="taken over by a person")
            self.state.record(run_id, "task.manual_taken",
                              {"base": head, "candidate": candidate, "path": str(path)},
                              task_id)
            return path

    def submit(self, run_id: str, task_id: str) -> tuple[bool, str]:
        """Verify a person's edits with the task's frozen checks and merge them like any
        candidate (the merge gate reruns every merged task's checks)."""
        with self.exclusive(run_id):
            run = self.state.run(run_id)
            task = self.state.task(run_id, task_id)
            path = self.manual_path(run_id, task_id)
            taken = [e for e in self.state.events(run_id, task_id) if e.kind == "task.manual_taken"]
            if not path.exists() or not taken:
                return False, f"{task_id} is not taken over; run `orch take {task_id}` first"
            base = str(taken[-1].data["base"])
            excludes = orch_verify.excludes(path)
            self.git(path, "add", "-A", "--", ".", *excludes)
            changed = set(self.git(path, "diff", "--cached", "--name-only", base).splitlines())
            outside = sorted(changed - set(task.files))
            if outside:
                self.git(path, "reset", "-q")
                return False, (f"edits outside {task_id}'s files {sorted(task.files)}: {outside}"
                               "; frozen tests and other tasks' files cannot change here")
            if not changed:
                return False, "no changes to submit"
            context = RunContext.of(run, self.state.tasks(run_id))
            inputs = (digest(dumps(context.dependency_contracts(task, run.base)))
                      if task.interface_depends_on else "")
            self.git(path, "commit", "-q", "-m", commit_message(
                run.id, run.plan_hash, task, f"a{task.attempts}h", HUMAN.label,
                title=f"orch {task.id}: finished by a person", inputs=inputs),
                author=HUMAN.label)
            commit = self.git(path, "rev-parse", "HEAD")
            # The same states as any candidate, so the run log and live view stay complete.
            for status, kind in (("READY", "task.manual_submit"), ("RUNNING", "task.started"),
                                 ("VERIFYING", "task.verifying")):
                self.state.transition(run_id, task_id, status, kind,
                                      {"candidate": commit, "by": HUMAN.label},
                                      attempt=task.attempts)

            def back(reason: str) -> None:
                self.state.transition(run_id, task_id, "PARKED", "task.parked",
                                      {"reason": reason}, attempt=task.attempts, reason=reason)

            passed, report = self.verify(path, self.scratch(run_id), commit, list(task.tests))
            if not passed:
                self.git(path, "reset", "-q", "--soft", "HEAD~1")
                back("a person's edit fails the frozen checks")
                return False, report
            diff = self.git(path, "diff", base, commit)
            self.state.transition(run_id, task_id, "MERGING", "task.merging",
                                  {"candidate": commit}, attempt=task.attempts)
            try:
                self._merge(run, self.state.task(run_id, task_id),
                            Candidate(commit, diff, "human", path), HUMAN)
            except (AttemptFailed, Parked) as failure:
                self.git(path, "reset", "-q", "--soft", "HEAD~1")
                back("a person's edit fails the merge gate")
                detail = failure.detail if isinstance(failure, AttemptFailed) else str(failure)
                return False, f"merge gate: {detail[-3000:]}"
            done = self.state.task(run_id, task_id).status == "DONE"
            if not done:  # the merge must be redone on a moved integration branch
                self.state.transition(run_id, task_id, "PARKED", "task.parked",
                                      {"reason": "rebase needed"}, attempt=task.attempts,
                                      reason="submit again: the integration branch moved")
            if done:
                with self.git_lock:
                    self.git(self.repo, "worktree", "remove", "--force", str(path))
            return done, ("merged; continue with `orch run`" if done else
                          "the integration branch moved and the merge must be redone: submit "
                          "again")

    def _failed(
        self, run_id: str, task_id: str, number: int, level: int, failure: AttemptFailed
    ) -> None:
        task = self.state.task(run_id, task_id)
        mark = signature(failure.category, failure.detail)
        diff_mark = digest(failure.diff)[:12] if failure.diff else ""
        repeated = mark == task.last_signature or (bool(diff_mark)
                                                   and diff_mark == task.last_diff_hash)
        route = self._claude_route(run_id) if level == 3 else self._builder_route(task)
        self.state.record(
            run_id, "attempt.outcome", {"route": route.label, "level": level, "passed": False},
            task_id, number,
        )
        evidence: dict[str, Any] = {
            "pending_candidate": "",
            "failed_candidate": (task.pending_candidate if failure.sampleable
                                 and task.pending_candidate else task.failed_candidate),
            "last_signature": mark,
            "last_diff_hash": diff_mark,
            "last_error": failure.detail[-3000:],
            "last_diff": failure.diff[-4000:],
        }
        data = {"category": failure.category, "signature": mark, "level": level,
                "sampleable": failure.sampleable, "candidate": task.pending_candidate,
                # what the studio shows for this attempt (docs/STUDIO.md)
                "report": failure.detail[-8000:], "diff": failure.diff[-8000:]}
        # One cheap retry with the failure in hand, unless the attempt was already a retry,
        # repeated an earlier failure, or came from the supervisor; then judgment. An answer
        # cut at the widest output budget cannot be bought again as it is: the supervisor
        # decides (a split into smaller tasks, a stronger model, a person).
        cheap = level == 0 and not repeated and failure.category not in {
            "context", "supervisor", "output_limit"}
        upcoming = 1 if cheap else 2
        self.state.transition(
            run_id, task_id, "READY", "task.failed", {**data, "next": upcoming},
            attempt=number, level=upcoming, **evidence,
        )
        self.notify(f"[run] {task_id} a{number} failed ({failure.category}); "
                    + ("retrying with the failure" if cheap else "to the supervisor"))


def combined(reports: list[str]) -> str:
    """Reports of single-check runs as one: every status line first (failed() and the
    ripple read them), then each failure's output."""
    heads, bodies = [], []
    for report in reports:
        head, _, body = report.partition("\n\n")
        heads.append(head)
        if body.strip() and any(line.startswith(("FAIL", "TIMEOUT"))
                                for line in head.splitlines()):
            bodies.append(body.strip())
    return "\n".join(heads) + ("\n\n" + "\n\n".join(bodies) if bodies else "")


PROJECT_TEST_SHARDS = 4
PROJECT_SHARD_MIN_FILES = 24  # below this a suite runs as one process
# pytest's default norecursedirs: explicit file arguments bypass them, so skip them here.
_NORECURSE = re.compile(r"(^|/)(\.[^/]*|_darcs|build|CVS|dist|node_modules|venv|[^/]*\.egg|"
                        r"\{arch\})/")


def default_verify_jobs() -> int:
    return max(2, os.cpu_count() or 2)


def module_key(task: OrchTask) -> str:
    """The directory of a task's first owned file: the unit of the circuit breaker."""
    return str(PurePosixPath(task.files[0]).parent) if task.files else "."


class Signal(Exception):
    """A builder's BLOCKED or TEST_DISPUTE answer: a planning problem, not a coding one."""

    def __init__(
        self, status: str, reason: str, failure: AttemptFailed | None = None,
    ) -> None:
        super().__init__(f"{status}: {reason[:300]}")
        self.status = status
        self.reason = reason
        self.failure = failure


@dataclass(frozen=True)
class PlanInputs:
    """What every plan sample of one run sees (the stable part of the planner prompt)."""

    spec: str
    base: str
    existing: frozenset[str]
    repo_map: str
    coupling: str


@dataclass
class Draft:
    """One plan sample: the plan (None if it never passed lint)."""

    plan: dict[str, object] | None
    repairs: int
    errors: list[list[str]]  # lint errors of each rejected attempt


@dataclass(frozen=True)
class Candidate:
    commit: str
    diff: str
    label: str
    path: Path


@dataclass(frozen=True)
class RunContext:
    """Inputs every attempt of a run derives from its (possibly revised) plan."""

    files: dict[str, str]
    stubs: dict[str, str]
    modules: dict[str, str]
    requirements: dict[str, str]
    tasks: dict[str, OrchTask]

    @classmethod
    def of(cls, run: Run, tasks: Iterable[OrchTask] | None = None) -> "RunContext":
        context = cls.of_plan(run.plan)
        return (replace(context, tasks={task.id: task for task in tasks
                                        if task.status != "REPLANNED"})
                if tasks is not None else context)

    @classmethod
    def of_plan(cls, plan: dict[str, object]) -> "RunContext":
        files = plan_files(plan)
        checked = set(orch_verify.files_of(
            [test for task in tasks_from(plan) for test in task.tests]))
        return cls(
            files=files,
            stubs={p: c for p, c in files.items() if p not in checked},
            modules={p: p for p in files if p in checked},
            requirements={
                str(item["id"]): str(item["text"])
                for item in cast(list[dict[str, str]], plan["requirements"])
            },
            tasks={task.id: task for task in tasks_from(plan)},
        )

    def interfaces_for(self, task: OrchTask) -> dict[str, str]:
        """Frozen stubs of this task and of every task it builds on, whole."""
        graph = {tid: other.depends_on for tid, other in self.tasks.items()}
        graph[task.id] = task.depends_on  # includes dependencies discovered during execution
        paths = set(task.files)
        for tid in closure(graph, task.id):
            if tid in self.tasks:
                paths.update(self.tasks[tid].files)
        return {path: self.stubs[path] for path in sorted(paths) if path in self.stubs}

    def dependency_contracts(self, task: OrchTask, baseline: str) -> dict[str, object]:
        """Version the promises used to build early, independent of implementation commits.

        Existing interfaces come from the immutable repository baseline; newly declared
        interfaces and behavioral requirements come from the frozen plan. No test results
        or generated implementation bodies are promoted to a public contract.
        """
        if not task.interface_depends_on:
            return {}
        graph = {tid: other.depends_on for tid, other in self.tasks.items()}
        graph[task.id] = task.depends_on
        contracts: dict[str, object] = {}
        for tid in sorted(closure(graph, task.id)):
            other = self.tasks[tid]
            requirements = {rid: self.requirements[rid] for rid in other.reqs}
            published = {"baseline": baseline, "goal": other.goal, "contract": other.contract,
                         "requirements": requirements, "files": sorted(other.files),
                         "interfaces": {path: self.stubs[path] for path in sorted(other.files)
                                        if path in self.stubs}}
            contracts[tid] = {"version": digest(dumps(published)), "contract": other.contract,
                              "requirements": requirements}
        return contracts

    def tests_for(self, task: OrchTask) -> dict[str, str]:
        """Frozen test files that this task's checks run."""
        return {path: self.files[path] for path in orch_verify.files_of(list(task.tests))
                if path in self.files}
