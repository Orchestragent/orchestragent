"""Deterministic scheduler: plan checkpoint, parallel DAG execution, verify, merge.

LLMs only answer leaf calls (Claude plans and handles exceptions, DSF builds). Every
decision in between is code, and a task is done only when its frozen tests pass on
the integration branch. The failure ladder lives in orch_ladder.
"""

import fcntl
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

from orchestragent import orch_generic, orch_verify
from orchestragent.dsh import EXTENDED, DshClient, json_failure
from orchestragent.envelope import digest
from orchestragent.jev import Decision
from orchestragent.live_workspace import PROJECT_TEST_TIMEOUT, LiveWorkspace
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
    common_failures,
    context_pack,
    contract_hash,
    diagnose_failure,
    failed_in,
    failed_tests,
    implementation_errors,
    interface_errors,
    parse_trailers,
    signature,
)
from orchestragent.orch_cache import MAX_WAIT_SECONDS, CachePolicy
from orchestragent.orch_context import owned_sources, planning_sources, requested_sources
from orchestragent.orch_environment import PytestEnvironment
from orchestragent.orch_excerpt import excerpt
from orchestragent.orch_graph import replacement_frontier
from orchestragent.orch_index import (
    Coupling,
    Index,
    RepoIndex,
    assertions,
    dependency_code,
    method_bodies,
    python_module,
)
from orchestragent.orch_journal import CallJournal
from orchestragent.orch_ladder import (
    CHEAT_FLAG,
    CHEAT_QUESTION,
    EXCEPTION_SCHEMA,
    HINTS,
    MAX_GENERATION,
    PARK,
    REPLAN_SCHEMA,
    cheat_probability,
    jev_state,
    next_level,
    next_task_id,
    pick,
    replan_prompt,
    review_prompt,
    samples,
    subtree,
    thicken_prompt,
    triage_options,
    triage_question,
)
from orchestragent.orch_models import Choice as ModelChoice
from orchestragent.orch_models import choose as choose_model
from orchestragent.orch_modules import (
    MODULES_SCHEMA,
    id_range,
    merge,
    module_lint,
    modules_prompt,
    submodule_lint,
    submodule_prompt,
)
from orchestragent.orch_mutate import (
    HARDEN_SCHEMA,
    MAX_SURVIVORS,
    PLANNER_LESSONS,
    WEAK_TESTS,
    Mutant,
    Report,
    harden_prompt,
    mutants,
    select,
)
from orchestragent.orch_pace import TokenBucket
from orchestragent.orch_plan import (
    PLAN_HINTS,
    PLAN_SCHEMA,
    REQ_TEST,
    chain_depths,
    closure,
    coupled_splits,
    coupling_text,
    dependency_errors,
    lint,
    lint_lessons,
    plan_files,
    plan_prompt,
    plan_score,
    tasks_from,
    unittest_ids,
)
from orchestragent.orch_planning import PlanningSession, PlanningStore
from orchestragent.orch_policy import Policy, PolicyStore
from orchestragent.orch_review import Review, ReviewStore, review_version
from orchestragent.orch_rules import (
    PROXY_RHO,
    PROXY_RUNS,
    Rule,
    arm,
    layer,
    lesson,
    planner_verdict,
    spearman,
    verdict,
)
from orchestragent.orch_state import FINISHED, IN_FLIGHT, OrchState, OrchTask, Run, plan_hash
from orchestragent.orch_thin import (
    BOUNDARY_REVIEW_SCHEMA,
    COVERAGE_SCHEMA,
    TEST_SCHEMA,
    BoundaryStop,
    assemble,
    boundary_errors,
    boundary_fingerprint,
    boundary_prompt,
    boundary_review_prompt,
    canonical_boundary,
    citing_tasks,
    coverage_prompt,
    fill_requirements,
    integration_targets,
    review_verdict,
    revise_draft,
    shard_path,
    shard_source,
    source_context,
    spec_requirements,
    test_edit_prompt,
    test_prompt,
    test_shard,
)
from orchestragent.orch_usage import (
    Call,
    output_cut,
    settled_json_failure,
    transient,
    unresolved,
)
from orchestragent.platform import sandbox_available
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
# The same model and reasoning effort with the wider output budget. Each DSF test-design
# and build call uses it whenever the call reservation covers that exact request (see
# _widened); the limit is a ceiling, not a target length. Larger requests keep DSF.
# Only the pinned DSH route has the extended output tier; other builders keep their route.
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
CLAUDE_PATCH = (Route("claude", "claude-opus-5-5", "medium") if PLANNER.provider == "claude"
                else PLANNER)
SONNET_PLAN = Route("claude", "claude-sonnet-5-5", "medium")
# Outcomes that say nothing about the task: stop dispatching, keep the task READY.
STOP = frozenset(
    {"auth_required", "quota_exhausted", "rate_limited", "waiting_approval", "policy_blocked",
     "cancelled", "timeout"}
)
MAX_REBASES = 3
MAX_REGRESSIONS = 2  # final-gate bisect rounds before the run gives up
MAX_TEST_REVIEWS = 3  # a successful test correction may expose another independent error
MAX_REPLAN_REVISIONS = 1  # a rejected replacement subtree gets one corrected proposal
# A reviewer checks each linted test shard against its requirements' verbatim text and the
# designer adds tests for stated behaviour nothing asserts (see coverage_prompt).
# The `reviewer` role; "off" disables the review.
_REVIEWER = str(choose_model("reviewer"))
COVERAGE_REVIEW = _REVIEWER != "off"
MAX_TEST_REVIEWS_PER_SHARD = 2
# Who reviews frozen test shards: the planner (Opus, default), Sonnet, or DSF itself.
# Opus reviewed 22 shards for US$3.19 on rollout (f6cdf35); the cheaper reviewers are for
# measured comparison.
REVIEW_MODEL = _REVIEWER if COVERAGE_REVIEW else "planner"
DEFAULT_TEST_JOBS = 16  # concurrent DSF test-design shards unless a frozen policy says otherwise
MAX_FORMAT_REPAIRS = 1  # DSF self-repairs of a settled unparseable/unappliable answer per attempt
# Repeats of one planning call after a transient transport/provider failure (run r073174b7
# lost the whole run to one DSF STREAM_CLOSED), and the pause before each repeat.
TRANSIENT_RETRY_DELAYS = (2.0, 8.0)
BREAKER = 3  # parked tasks in one module that open its circuit breaker
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


Decider = Callable[[object, dict[str, object], str], Decision]
JEV = "vercel/typesafe-ai/jev"
JEV_MODES = ("on", "shadow", "off")


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
        decide: Decider | None = None,
        jev_mode: str = "off",
        policy: Policy | None = None,
        environment: PytestEnvironment | None = None,
        strategy: str = "opus-dsf",
    ) -> None:
        if strategy not in {"opus-dsf", "sonnet-dsf"}:
            raise ValueError("Strategy must be opus-dsf or sonnet-dsf")
        if strategy == "sonnet-dsf" and jev_mode != "off":
            raise ValueError("sonnet-dsf does not support Jev modes")
        self.strategy = self.strategy_override = strategy
        self._planning_strategies: dict[str, str] = {}
        self._durable_planning: set[str] = set()
        if jev_mode not in JEV_MODES:
            raise ValueError(f"jev mode must be one of {JEV_MODES}")
        self.repo = repo.resolve()
        self.call = call
        # Pure Python projects keep the tuned unittest/pytest pipeline; every other project
        # (any language, or several) runs the checks its own test designers declared.
        self.generic = orch_verify.engine(self.repo) == "generic"
        self.runner = orch_verify.run if self.generic else runner
        self.notify = notify
        self.clock = clock
        self.sleep = sleep
        # Jev answers choice questions; "shadow" asks and records but never acts.
        self.decide = decide
        self.jev_mode = jev_mode if decide is not None else "off"
        self.home = orch_home(self.repo)
        self.home.mkdir(parents=True, exist_ok=True)
        self.state = OrchState(self.home / "state.sqlite", clock)
        self.planning = PlanningStore(self.state.store)
        self.reviews = ReviewStore(self.state)
        self.policies = PolicyStore(self.state)
        if policy is not None:
            self.policies.save(policy)
            policy = self.policies.get(policy.id)
        self.policy_override = policy
        self.policy = policy or self.policies.current()
        self.environment_override = environment
        self.environment = environment
        self.environment_text = ""
        self.index = Index(self.repo, self.home / "index.sqlite")
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
        # Rules: the active layer is a per-run snapshot; trials map attempts to A/B arms.
        self.rules_text = ""
        self.rule_ids: list[str] = []
        self.trials: dict[tuple[str, str, int], tuple[str, bool]] = {}
        self.trial_lock = threading.Lock()
        # Wide runs: generation (model calls) and verification (sandbox runs) are
        # separate pools; cache hints never serialize unknown cold-start traffic.
        self.flow = threading.Lock()
        self.generating: set[str] = set()
        self.slot_freed = threading.Event()  # a task left generation: dispatch may resume
        self.verifying = 0
        self.max_verify_jobs = default_verify_jobs()
        self.verify_slots = threading.BoundedSemaphore(self.max_verify_jobs)
        self.early_interface_start = EARLY_INTERFACE_START
        self.cache_policy = CachePolicy(clock)
        self.cache_mode = "auto"
        self.ready_cache_keys: dict[str, tuple[str, str, str]] = {}
        self.cache_wait_started: dict[str, float] = {}
        self.bucket: TokenBucket | None = None
        self.max_cents = 0
        self.spent = 0
        self.pools: dict[tuple[str, str], list[Path]] = {}
        self.metrics: dict[str, int] = {}
        self.timing = threading.local()

    def _run_strategy(self, run_id: str) -> str:
        with self.git_lock:
            pending = self._planning_strategies.get(run_id)
        try:
            strategy = self.state.run(run_id).strategy
        except KeyError:
            strategy = pending or self.strategy_override
        if pending is not None and pending != strategy:
            raise ValueError("Planning strategy conflicts with saved run; no fallback")
        if strategy not in {"opus-dsf", "sonnet-dsf"}:
            raise ValueError("Unsupported saved orchestration strategy")
        return strategy

    def _claude_route(self, run_id: str, *, planning: bool = False) -> Route:
        if self._run_strategy(run_id) == "sonnet-dsf":
            return SONNET_PLAN
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
        """Every source file of the project's language at `revision`, in one git batch."""
        if self.generic:  # every tracked text file of a reasonable size, in any language
            sizes = self.git(self.repo, "ls-tree", "-r", "-l", revision).splitlines()
            paths = [line.split("\t", 1)[1] for line in sizes
                     if line.split()[3].isdigit() and int(line.split()[3]) <= GENERIC_SOURCE_BYTES]
            return self.index.read(revision, paths)
        return self.index.read(revision, suffix=".py")

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
        if self.generic:
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
                if selected == LiveWorkspace.run_unittests and self.environment is not None:
                    selected = partial(LiveWorkspace.run_unittests, environment=self.environment)
                return selected(execution, scratch, revision, names, timeout=timeout)
            finally:
                self.timing.last = time.monotonic() - started
                with self.flow:
                    self.verifying -= 1
        finally:
            self.verify_slots.release()

    def _language_note(self) -> str:
        if not self.generic:
            return ""
        return ("Write each owned file in its own language. Stub bodies fail with 'not "
                "implemented'; replace every one in your owned files. " + orch_verify.SANDBOX_NOTE)

    def elapsed(self) -> float:
        """Seconds this thread's last sandbox run took (its wait for a slot excluded)."""
        return float(getattr(self.timing, "last", 0.0))

    def bind_environment(self, root: str = "", fingerprint: str = "") -> None:
        """Bind every verification/lookup entrypoint to the same saved runtime contract."""
        self.environment, self.environment_text = None, ""
        self.index.source_roots = (".",)
        if root or fingerprint:
            if not root or not fingerprint:
                raise ValueError("Incomplete pinned project environment")
            environment = PytestEnvironment.load(Path(root))
            if environment.fingerprint != fingerprint:
                raise ValueError("Pinned environment fingerprint changed")
            self.environment = environment
            self.environment_text = environment.prompt()
            self.index.source_roots = environment.source_roots

    # --- planning -------------------------------------------------------
    def _call_model(
        self, run_id: str, route: Route, prompt: str, schema: dict[str, object],
        call_id: str, *, purpose: str, task_id: str = "", attempt: int = 0,
        sample: str = "", scope: str = "execution", prefix: str = "",
        rules: list[str] | None = None, base: str = "",
        journal: CallJournal | None = None,
    ) -> NativeResult:
        if self._run_strategy(run_id) == "sonnet-dsf":
            dsf_routes = {DSF, DSF_EXTENDED, DSF_TEST, self._extended(DSF_TEST),
                          DSF_BUILD_FIRST, self._extended(DSF_BUILD_FIRST)}
            if (route not in dsf_routes | {SONNET_PLAN}
                    or purpose == "test-design" and route not in dsf_routes):
                raise ValueError("Disallowed route for sonnet-dsf; no fallback")
        if self.environment is not None:
            try:
                if PytestEnvironment.load(self.environment.root) != self.environment:
                    raise ValueError("fingerprint changed")
            except (OSError, ValueError):
                raise Deferred("Pinned environment changed before model dispatch") from None
            environment = self.environment_text
            prompt = (ContextPrompt(environment + prompt.shared, prompt.dynamic)
                      if isinstance(prompt, ContextPrompt) else environment + prompt)
            if prefix and not isinstance(prompt, ContextPrompt):
                prefix = digest(environment + prefix)[:12]
            if (route.provider == "dsh"
                    and len(DshClient.task_text(prompt, schema).encode()) > DshClient.input_limit):
                raise ContextTooLarge("Pinned environment and request exceed DSF input limit")
        payload: dict[str, object] = {"prompt": prompt, "schema": schema}
        if isinstance(prompt, ContextPrompt):
            payload["context_boundary_bytes"] = len(prompt.shared.encode())
            prefix = digest(prompt.shared)[:12]
        request = dumps(payload)
        receipt = Call(
            call_id, run_id, route.provider, route.model, purpose, self.clock(),
            task_id=task_id, attempt=attempt, sample=sample, effort=route.effort,
            scope=scope, prompt_hash=digest(request), prefix_hash=prefix,
            request_bytes=len(request.encode()), rules=list(rules or []), base_revision=base,
        )
        if scope == "planning" and run_id in self._durable_planning:
            journal = self.planning
        reviewing = journal is self.reviews
        if journal is not None:
            cached = journal.claim(receipt)
            if cached is not None:
                self.notify(f"[{scope}] reuse settled {purpose} {task_id or sample} a{attempt}")
                if reviewing:
                    self.state.record(run_id, "task.review_reused", {
                        "review": sample, "call_id": cached[1],
                    }, task_id, attempt)
                return cached[0]
        if scope == "execution" and (self.cancel.is_set() or self.stop_reason
                                     or self.max_cents and self.spent >= self.max_cents):
            raise Deferred(self.stop_reason or "execution stopped before model dispatch")
        if reviewing:
            if sum(c.sample == sample for c in self.state.usage.calls(run_id)) >= MAX_TEST_REVIEWS:
                raise Parked("Review provider retry limit reached; inspect settled call receipts")
            self.state.record(run_id, "attempt.called", {
                "route": route.label, "purpose": purpose, "prefix": prefix,
                "bytes": len(prompt.encode()), "review": sample,
            }, task_id, attempt)
        if prefix:
            self.cache_policy.start(call_id, (route.provider, route.model, prefix))
        raw_usage: dict[str, object] = {}
        result: NativeResult | None = None
        try:
            with self.state.usage.capture(
                receipt,
                (lambda db: journal.finish(db, receipt, result)) if journal is not None else None,
            ):
                result = self.call(route.provider, route.model, prompt, schema, call_id,
                                   self.cancel.is_set, effort=route.effort)
                receipt.outcome, receipt.usage = result.outcome, result.usage
                raw_usage = result.usage
                receipt.session_id = result.session_id
            if scope in {"execution", "harden"}:
                self._spend(receipt.usage)
        finally:
            if prefix:
                self.cache_policy.finish(call_id, raw_usage, receipt.outcome)
        assert result is not None
        if reviewing:
            self.state.record(run_id, "attempt.result", {
                "route": route.label, "outcome": result.outcome, "usage": result.usage,
                "call_id": call_id, "review": sample,
            }, task_id, attempt)
        return result

    def plan(
        self, spec: str, base: str = "HEAD", attempts: int = 3, samples: int = 1,
        modules: bool = False, distributed: bool = False, test_jobs: int | None = None,
        reviewed_boundary: bool = False,
        integration_checks: bool = True,
    ) -> Run:
        """Claude drafts `samples` plans in parallel (no discussion between them); lint
        gates each and a deterministic score picks one. Planner rules are settled first
        (this is their checkpoint boundary) and one candidate rule is on trial.

        `modules` plans hierarchically for large builds (orch_modules): module
        boundaries first, then every module's sub-plan in parallel, merged by code."""
        self.policy = self.policy_override or self.policies.current()
        tuning = self.policy.tuning if self.policy else None
        if test_jobs is None:
            # One wave for up to 16 shards: networkx runs at 4b12bef queued 12-16 shards in
            # waves of 4 (planning 468-617 s); DSF showed no per-call slowdown at 4-8 calls.
            test_jobs = tuning.test_jobs if tuning else DEFAULT_TEST_JOBS
        if self.strategy_override == "sonnet-dsf" and (modules or samples != 1):
            raise PlanError("sonnet-dsf does not support hierarchical or sampled planning")
        if distributed and (modules or samples != 1 or not 1 <= test_jobs <= 16):
            raise PlanError("distributed planning needs one boundary and 1..16 test jobs")
        if reviewed_boundary and not distributed:
            raise PlanError("reviewed boundary requires distributed planning")
        run_id = "r" + uuid.uuid4().hex[:8]
        with self.git_lock:
            self._planning_strategies[run_id] = self.strategy_override
        self.notify(f"[plan] run {run_id}; usage: orch cost --run {run_id}")
        try:
            with self.exclusive(run_id):
                if distributed:
                    base = self.git(self.repo, "rev-parse", "--verify", f"{base}^{{commit}}")
                    route = self._claude_route(run_id, planning=True)
                    environment = self.environment_override
                    self.planning.create(PlanningSession(
                        run_id, spec, base, self.strategy_override, route.label, route.effort,
                        attempts, test_jobs, str(environment.root) if environment else "",
                        environment.fingerprint if environment else "",
                        DSF.label, DSF.effort, reviewed_boundary, integration_checks,
                        self.policy.id if self.policy else "",
                        integration_scope="joins",
                    ))
                    self._durable_planning.add(run_id)
                return self._plan(run_id, spec, base, attempts, samples, modules,
                                  distributed, test_jobs, reviewed_boundary, integration_checks)
        finally:
            self.release_worktrees(run_id)  # probe and baseline worktrees
            with self.git_lock:
                self._planning_strategies.pop(run_id, None)
                self._durable_planning.discard(run_id)

    def resume_plan(self, run_id: str, *, ack_unknown_calls: list[str] | None = None) -> Run:
        """Resume distributed planning at pinned inputs, reusing settled phase responses."""
        session = self.planning.session(run_id)
        with self.exclusive(session.id):
            try:
                return self.state.run(session.id)
            except KeyError:
                pass
            self.cancel.clear()
            self.stop_reason = ""
            if self.policy_override and self.policy_override.id != session.policy_id:
                raise PlanError("Resume must use the pinned planning policy")
            self.policy = self.policies.get(session.policy_id) if session.policy_id else None
            self.planning.acknowledge(session.id, ack_unknown_calls or [])
            if self.environment_override is not None and (
                str(self.environment_override.root) != session.environment_root
                or self.environment_override.fingerprint != session.environment_fingerprint
            ):
                raise PlanError("Resume must use the pinned planning environment")
            original_environment = self.environment_override
            self.environment_override = (
                PytestEnvironment(Path(session.environment_root), session.environment_fingerprint)
                if session.environment_root else None)
            with self.git_lock:
                self._planning_strategies[session.id] = session.strategy
                self._durable_planning.add(session.id)
            try:
                route = self._claude_route(session.id, planning=True)
                if ((route.label, route.effort) != (session.planner, session.effort)
                        or (DSF.label, DSF.effort) != (session.designer, session.designer_effort)):
                    raise PlanError("Pinned planner route changed; no automatic model substitution")
                return self._plan(session.id, session.spec, session.base, session.attempts,
                                  1, False, True, session.test_jobs, session.reviewed_boundary,
                                  session.integration_checks)
            finally:
                self.environment_override = original_environment
                with self.git_lock:
                    self._planning_strategies.pop(session.id, None)
                    self._durable_planning.discard(session.id)

    def _plan(
        self, run_id: str, spec: str, base: str, attempts: int, samples: int, modules: bool,
        distributed: bool = False, test_jobs: int = 4,
        reviewed_boundary: bool = False,
        integration_checks: bool = True,
    ) -> Run:
        self.strategy = self._run_strategy(run_id)
        thin = self.strategy == "sonnet-dsf" or distributed
        if self.strategy == "sonnet-dsf" and self.policy is not None:
            raise PlanError("sonnet-dsf planning does not support frozen policy mode")
        base_sha = self.git(self.repo, "rev-parse", "--verify", f"{base}^{{commit}}")
        environment = self.environment_override
        self.bind_environment()
        project_report = ""
        # The project's own pytest baseline only proves the base revision is healthy, so it
        # runs beside planning instead of before it (NetworkX: ~105 s of 5446 tests that
        # used to delay the planner) and is checked before anything is committed.
        baseline_pool: ThreadPoolExecutor | None = None
        baseline: Future[tuple[bool, str]] | None = None
        if environment is not None:
            try:
                self.bind_environment(str(environment.root), environment.fingerprint)
                assert self.environment is not None
                environment = self.environment
            except Exception as error:
                raise PlanError(
                    f"project pytest baseline unavailable: {type(error).__name__}"
                ) from None
            baseline_pool = ThreadPoolExecutor(max_workers=1)
            baseline = baseline_pool.submit(self._project_tests, run_id, base_sha, environment)
        stopped_by_baseline = threading.Event()

        def baseline_error() -> PlanError | None:
            """Wait for the baseline; the PlanError a red or unusable baseline means."""
            if baseline is None:
                return None
            try:
                passed, report = baseline.result()
            except Exception as error:
                return PlanError(f"project pytest baseline unavailable: {type(error).__name__}")
            return None if passed else PlanError("project pytest baseline failed",
                                                 [report[-4000:]])

        def stop_planning(done: Future[tuple[bool, str]]) -> None:
            # A red base stops in-flight planning calls at once: nothing is built on it.
            failed = done.exception() is not None or not done.result()[0]
            if failed and not self.cancel.is_set():
                stopped_by_baseline.set()
                self.cancel.set()

        if baseline is not None:
            baseline.add_done_callback(stop_planning)
        try:
            self.policies.observe_spec(spec)
            notes = [] if thin else self._settle_planner_rules()
            rules_text, rule_ids, evicted = layer(
                [] if thin and self.policy is None else self._rules("planner"), "planner")
            for rule in evicted:
                self.state.save_rule(replace(rule, status="retired", note="evicted by rule budget"))
                notes.append(("rule.retired", {"rule": rule.id, "note": "budget"}))
            trial = None if thin else self._planner_trial(run_id)
            coupling = self.index.cochange(base_sha)
            inputs = PlanInputs(
                spec, base_sha,
                frozenset(self.git(self.repo, "ls-tree", "-r", "--name-only",
                                   base_sha).splitlines()),
                (self.index.at(base_sha).planning_map(spec) if distributed else
                 self.index.at(base_sha).repo_map(PLANNER_MAP_BYTES)), coupling_text(coupling),
                rules_text, trial[0].text if trial is not None and trial[1] else "",
                tuple([*rule_ids, *([trial[0].id] if trial is not None and trial[1] else [])]),
            )
            count = 1 if modules else max(1, min(samples, len(PLAN_HINTS)))
            if distributed:
                outcomes: list[Draft | PlanError] = [
                    self._draft_thin(run_id, inputs, attempts, distributed=True, jobs=test_jobs,
                                     reviewed_boundary=reviewed_boundary,
                                     integration_checks=integration_checks)]
            elif modules:
                outcomes = [self._draft_modules(run_id, inputs, attempts)]
            else:
                with ThreadPoolExecutor(max_workers=count) as pool:
                    outcomes = list(pool.map(
                        lambda i: self._draft(run_id, inputs, PLAN_HINTS[i] if count > 1 else "",
                                              f"s{i + 1}" if count > 1 else "", attempts),
                        range(count),
                    ))
            drafts = [item for item in outcomes if isinstance(item, Draft)]
            if not thin:
                for draft in drafts:
                    for errors in draft.errors:
                        for text in lint_lessons(errors):
                            self._learn("", text, f"{run_id} lint", "planner")
            clean = [draft for draft in drafts if draft.plan is not None]
            if not clean:
                rejected = [errors for draft in drafts for errors in draft.errors[-1:]]
                if rejected:
                    raise PlanError("plan failed lint", rejected[-1])
                raise next(item for item in outcomes if isinstance(item, PlanError))
            for draft in clean:
                self._score(run_id, base_sha, draft, coupling)
            best = max(clean, key=lambda draft: draft.score)
            assert best.plan is not None
            shown = [*rule_ids, *([trial[0].id] if trial is not None and trial[1] else [])]
            failure = baseline_error()
            if failure is not None:
                raise failure
            if baseline is not None:
                project_report = baseline.result()[1]
            run = self._checkpoint(run_id, spec, base_sha, best.plan, best, shown, environment)
        except Exception:
            failure = baseline_error()
            if failure is not None:
                raise failure from None  # the base, not the interrupted planning, is the cause
            raise
        finally:
            if baseline_pool is not None:
                baseline_pool.shutdown(wait=True)  # never leave the baseline worktree behind
            if stopped_by_baseline.is_set():
                self.cancel.clear()
        if distributed:
            self.state.record(run_id, "plan.distributed", {
                "map_bytes": len(inputs.repo_map.encode()), "test_jobs": test_jobs,
                "tasks": len(cast(list[object], best.plan["tasks"])),
                "reviewed_boundary": reviewed_boundary,
                "integration_checks": integration_checks,
            })
        if environment is not None:
            self.state.record(run_id, "plan.project_baseline", {
                "revision": base_sha, "environment_root": str(environment.root),
                "environment_fingerprint": environment.fingerprint, "passed": True,
                "report": project_report[-4000:],
            })
        for kind, data in notes:
            self.state.record(run_id, kind, data)
        if count > 1:
            self.state.record(run_id, "plan.samples", {
                "scores": [draft.score if draft.plan is not None else None for draft in drafts],
                "chosen": drafts.index(best) + 1,
            })
        passed = not (best.repairs or best.vacuous or best.splits)
        self.state.plan_outcome(
            run_id, trial[0].id if trial else "", bool(trial and trial[1]), passed, "proxy",
            {"score": best.score, "parts": best.parts, "repairs": best.repairs,
             "vacuous": best.vacuous, "splits": best.splits},
        )
        return run

    def _draft(
        self, run_id: str, inputs: "PlanInputs", hint: str, sample: str, attempts: int
    ) -> "Draft | PlanError":
        """One plan sample through up to `attempts` lint repairs."""
        if self._run_strategy(run_id) == "sonnet-dsf":
            if hint or sample:
                return PlanError("sonnet-dsf does not support sample/replay hints")
            return self._draft_thin(run_id, inputs, attempts)
        errors: list[str] = []
        seen: list[list[str]] = []
        label = f" {sample}" if sample else ""
        for attempt in range(1, attempts + 1):
            self.notify(f"[plan] asking {self._claude_route(run_id, planning=True).label}{label} "
                        f"(attempt {attempt})")
            answer = self._ask_planner(
                f"orch-{run_id}-plan-{sample + '-' if sample else ''}{attempt}",
                plan_prompt(inputs.spec, inputs.repo_map, errors, inputs.rules, inputs.trial,
                            inputs.coupling, hint),
                PLAN_SCHEMA,
                run_id=run_id, purpose="plan", attempt=attempt, sample=sample,
                base=inputs.base, scope="evolve" if sample in {"ew", "eo"} else "planning",
                rules=list(inputs.rule_ids),
            )
            if isinstance(answer, PlanError):
                return answer
            errors = lint(answer, inputs.existing) or self._pack_errors(
                answer, inputs.base, inputs.spec)
            if not errors:
                return Draft(answer, attempt - 1, seen)
            seen.append(errors)
            self.notify(f"[plan]{label} lint rejected {len(errors)} item(s)")
        return Draft(None, attempts, seen)

    def _draft_thin(
        self, run_id: str, inputs: "PlanInputs", attempts: int, *, distributed: bool = False,
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
        repo = (inputs.repo_map if distributed else
                self.index.at(inputs.base).repo_map(BUILDER_MAP_BYTES))
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
                                     existing_edits=distributed, generic=self.generic,
                                     coupling=inputs.coupling if reviewed_boundary else "",
                                     previous=reviewed_draft, rules=inputs.rules)
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
            if not self.generic:
                errors = boundary_errors(answer, inputs.existing, existing_edits=distributed)
            elif not distributed:
                return PlanError("projects outside the Python pipeline plan distributed")
            else:
                errors = orch_generic.boundary_errors(answer, inputs.existing,
                                                      existing_edits=distributed)
            if not errors:
                errors = self._pack_errors({**answer, **self._engine_key()}, inputs.base,
                                           inputs.spec)
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
                                           rules=inputs.rules, findings=review_feedback),
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
            if not errors and not distributed:
                request = DshClient.task_text(
                    test_prompt(inputs.spec, answer, repo, [], generic=self.generic),
                    self._test_schema())
                if len(request.encode()) > DshClient.input_limit:
                    errors = ["Boundary/spec/interfaces exceed the DSF test-design request limit"]
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
        if distributed:
            return self._draft_test_shards(run_id, inputs, boundary, attempts, jobs, seen,
                                           integration_checks=integration_checks)
        for number in range(1, attempts + 1):
            if self.cancel.is_set():
                return PlanError("thin test design cancelled")
            prompt = test_prompt(inputs.spec, boundary, repo, errors, generic=self.generic)
            request = DshClient.task_text(prompt, self._test_schema())
            if len(request.encode()) > DshClient.input_limit:
                return PlanError("DSF test-design request exceeds input limit; nothing dispatched")
            result = self._call_planning(
                run_id, DSF, prompt, self._test_schema(),
                f"orch-{run_id}-test-design-{number}-{uuid.uuid4().hex[:12]}",
                purpose="test-design", attempt=number, base=inputs.base,
            )
            if result.outcome != "ok":
                return PlanError(f"test-designer returned {result.outcome}")
            try:
                plan = assemble(boundary, result.data, inputs.existing)
                errors = self._pack_errors(plan, inputs.base, inputs.spec)
            except (ValueError, TypeError, KeyError, SyntaxError) as error:
                errors = [str(error)]
            if not errors:
                return Draft(plan, len(seen), seen)
            seen.append(errors)
        return Draft(None, len(seen), seen)

    @staticmethod
    def _extended(route: Route) -> Route:
        return replace(route, effort=route.effort + EXTENDED) if route.provider == "dsh" \
            else route

    def _widened(self, route: Route, prompt: str, schema: dict[str, object]) -> Route | None:
        """The extended-output route when the per-call reservation bounds this exact
        request's worst case: every request byte billed as an input token plus the full
        extended output."""
        if route not in {DSF, DSF_TEST, DSF_BUILD_FIRST} or route.provider != "dsh":
            return None
        wide = self._extended(route)
        prices = DSH_PRICE_FEN_PER_MTOK[route.model]
        request = len((self.environment_text + DshClient.task_text(prompt, schema)).encode())
        worst = (request * max(prices["input_tokens"], prices["cache_creation_input_tokens"])
                 + DshClient.output_settings(wide.effort)[0] * prices["output_tokens"])
        return wide if worst <= DSF_RESERVATION * 1_000_000 else None

    def _draft_test_shards(
        self, run_id: str, inputs: "PlanInputs", boundary: dict[str, object], attempts: int,
        jobs: int, seen: list[list[str]],
        *, integration_checks: bool = True,
    ) -> "Draft | PlanError":
        tasks = cast(list[dict[str, Any]], boundary["tasks"])
        graph = {task["id"]: task["depends_on"] for task in tasks}
        assignments = [(task, False) for task in tasks]
        if integration_checks:
            scope = self.planning.session(run_id).integration_scope
            selected = set(integration_targets(graph, scope)) | citing_tasks(boundary)
            assignments += [(task, True) for task in tasks if task["id"] in selected]
        shard_sources: dict[str, dict[str, str]] = {}
        checks: dict[tuple[str, bool], str] = {}  # generic engine: each shard's check id
        for task in tasks:
            related = closure(graph, task["id"]) | {task["id"]}
            paths = {path for other in tasks if other["id"] in related for path in other["files"]}
            # Read the pre-task revision once, before any builders exist. New modules also
            # receive their declared dependencies' old implementations; never candidate code.
            shard_sources[task["id"]] = planning_sources(self.index, inputs.base, paths,
                                                           any_file=self.generic)
        for task, integration in assignments:
            prompt = test_prompt(inputs.spec, boundary, inputs.repo_map, [],
                                 generic=self.generic, task_id=task["id"],
                                 sources=shard_sources[task["id"]], integration=integration)
            request = DshClient.task_text(prompt, self._test_schema())
            if len(request.encode()) > DshClient.input_limit:
                return PlanError("Distributed test-design request exceeds input limit")
        self.notify(f"[plan] {len(assignments)} test-design shards "
                    f"({len(assignments) - len(tasks)} independent integration), "
                    f"at most {jobs} concurrent calls")
        stopped = threading.Event()

        def design(item: tuple[dict[str, Any], bool]) -> tuple[
            list[dict[str, str]], list[list[str]],
        ]:
            task, integration = item
            purpose = "integration-test-design" if integration else "test-design"
            errors: list[str] = []
            rejected: list[list[str]] = []
            # A settled unparseable answer is a protocol slip: it gets MAX_FORMAT_REPAIRS of
            # its own, separate from the `attempts` lint revisions. Every physical call has
            # its own deterministic phase number, so resume replays settled answers.
            repairs = revisions = 0
            marks: set[str] = set()
            code = "lint_budget"
            tid = task["id"]
            path = shard_path(tid, integration)
            # A complete, well-formed draft whose Python does not compile is revised in
            # place with EDIT_SCHEMA edits, not regenerated; it still uses a lint revision.
            draft = ""
            # Up to two reviews: the first may add tests for missing behaviour; the second
            # checks the revised draft and acts only on wrong tests, so added tests are
            # validated before freezing and missing items cannot ping-pong.
            reviews = MAX_TEST_REVIEWS_PER_SHARD if not COVERAGE_REVIEW else 0
            # The last lint-passing draft a review asked to improve: if that improvement
            # fails, this draft is frozen instead of redesigning the shard (timeflow at
            # 5588d86 spent 144 s regenerating one after a failed review repair).
            fallback: list[dict[str, str]] | None = None
            for number in range(1, attempts + MAX_FORMAT_REPAIRS + 2
                                + MAX_TEST_REVIEWS_PER_SHARD):
                if revisions >= attempts:
                    break
                if self.cancel.is_set() or stopped.is_set():
                    raise PlanError("distributed test design cancelled")
                local, draft = draft, ""
                schema = EDIT_SCHEMA if local else self._test_schema()
                stage = purpose.removesuffix("-design") + "-repair" if local else purpose
                prompt = (test_edit_prompt(
                    test_prompt(inputs.spec, boundary, inputs.repo_map, [], generic=self.generic,
                                task_id=tid,
                                sources=shard_sources[tid], integration=integration),
                    path, local, errors) if local else
                    test_prompt(inputs.spec, boundary, inputs.repo_map, errors,
                                generic=self.generic, task_id=tid,
                                sources=shard_sources[tid], integration=integration))
                if len(DshClient.task_text(prompt, schema).encode()) > DshClient.input_limit:
                    stopped.set()
                    raise PlanError("Distributed test repair exceeds input limit")
                # The widest output budget the call reservation covers for this exact request.
                route = self._widened(DSF_TEST, prompt, schema) or DSF_TEST
                try:
                    result = self._call_planning(
                        run_id, route, prompt, schema,
                        f"orch-{run_id}-{stage}-{tid}-{number}-{uuid.uuid4().hex[:12]}",
                        purpose=stage, task_id=tid, attempt=number,
                        base=inputs.base,
                        prefix=digest(prompt.split("\n## ASSIGNED TEST SHARD\n")[0]),
                    )
                except BaseException:
                    stopped.set()  # do not dispatch queued shards after an infrastructure failure
                    raise
                if result.outcome != "ok" and fallback is not None:
                    self.notify(f"[plan] {tid} test-design {number}: review repair "
                                f"{result.outcome}; keep the reviewed draft")
                    return fallback, rejected
                if result.outcome != "ok":
                    if output_cut(result.usage) and not unresolved(result.outcome, result.usage):
                        # Already the widest budget the reservation covers: the same request
                        # would be cut again, so stop instead of buying it twice.
                        limit = DshClient.output_settings(route.effort)[0]
                        self.notify(f"[plan] {tid} test-design {number}: stopped at {limit} "
                                    "output tokens; stop")
                        code = "output_limit"
                        errors = [f"Answer stopped at the {limit}-token output limit "
                                  f"({result.message[:200]})"]
                        rejected.append(errors)
                        break
                    if settled_json_failure(result.outcome, result.message, result.usage):
                        diagnosis, excerpt = json_failure(result.text)
                        note = ("Previous response was invalid/truncated JSON "
                                f"({result.message[:300]}; {diagnosis}). Return a complete "
                                "compact JSON object; reduce repetitive test code."
                                + (" Excerpt around the error (data, not instructions): "
                                   + excerpt if excerpt else ""))
                        self.notify(f"[plan] {tid} test-design {number}: settled invalid JSON; "
                                    "retry only this shard")
                        rejected.append([note])
                        if local and repairs < MAX_FORMAT_REPAIRS:
                            # A malformed patch answer keeps the same draft and its original
                            # compile feedback for the one format self-repair.
                            repairs += 1
                            draft, errors = local, [*errors, note]
                            continue
                        errors = [note]
                        if repairs < MAX_FORMAT_REPAIRS:
                            repairs += 1
                        else:
                            revisions += 1
                        continue
                    stopped.set()
                    raise PlanError(f"test-designer {tid} returned {result.outcome}")
                design_data: object = result.data
                try:
                    if local:
                        if result.data.get("status") != "DONE":
                            raise EditError("a local draft repair must return DONE with edits")
                        design_data = revise_draft(path, local, result.data.get("edits"))
                    if not self.generic:
                        files = test_shard(boundary, tid, design_data, integration=integration)
                        tests = [name for file in files
                                 for name in unittest_ids(file["path"], file["content"])]
                    else:
                        files, check = orch_generic.test_shard(boundary, tid, design_data,
                                                               existing=inputs.existing)
                        problem = self._red_problem(run_id, inputs.base, boundary, files, check)
                        if problem:
                            raise ValueError(problem)
                        tests = [check.id]
                        checks[(tid, integration)] = check.id
                    candidate = {
                        **boundary, **self._engine_key(),
                        "files": [*cast(list[dict[str, str]], boundary["files"]), *files],
                        "tasks": [{**item, "tests": tests} if item["id"] == tid else item
                                  for item in tasks],
                    }
                    errors = self._pack_errors(candidate, inputs.base, inputs.spec, only_task=tid)
                    label = f"{tid}-integration" if integration else tid
                    artifact = self.run_dir(run_id) / "planning" / f"{label}-{number}.json"
                    artifact.write_text(dumps({
                        "files": files, "errors": errors, "accepted": not errors,
                        "source_revision": inputs.base,
                        "source_selectors": sorted(shard_sources[tid]),
                        "source_bytes": sum(len(text.encode())
                                            for text in shard_sources[tid].values()),
                    }))
                    if not errors and reviews < MAX_TEST_REVIEWS_PER_SHARD and not self.generic:
                        reviews += 1  # a revised draft is linted again before its review
                        gaps = self._coverage_gaps(run_id, inputs.base, boundary, graph, task,
                                                   integration, files, number)
                        if reviews > 1:
                            gaps = [gap for gap in gaps if gap.startswith("wrong ")]
                        if gaps and len(files) == 1:
                            errors = ["Test review before freezing. For each 'missing' item "
                                      "add a focused test of that stated behaviour; for each "
                                      "'wrong' item correct that test to what the "
                                      "requirement implies. Keep every other test unchanged: "
                                      + "; ".join(gaps)]
                            draft = files[0]["content"]  # revise this draft in place next
                            fallback = files
                            rejected.append(errors)
                            self.notify(f"[plan] {tid} test-design {number}: test review "
                                        f"found {len(gaps)} gap(s) or wrong test(s)")
                            continue
                    if not errors:
                        return files, rejected
                    if fallback is not None:
                        self.notify(f"[plan] {tid} test-design {number}: review repair failed "
                                    "lint; keep the reviewed draft")
                        return fallback, [*rejected, errors]
                    errors = [*errors, "Compact repetitive test setup with subTest tables. "
                              "Preserve coverage and expected behaviour for every assigned "
                              "requirement; do not omit assertions just to fit the budget."]
                    self.notify(f"[plan] {tid} test-design {number}: builder capacity; "
                                "repair only this shard")
                except (ValueError, TypeError, KeyError, SyntaxError) as error:
                    if fallback is not None:
                        self.notify(f"[plan] {tid} test-design {number}: review repair "
                                    f"unusable ({str(error)[:120]}); keep the reviewed draft")
                        return fallback, [*rejected, [str(error)]]
                    errors = [str(error)]
                    context = (source_context(design_data, error.lineno or 0)
                               if isinstance(error, SyntaxError) else "")
                    if context:
                        errors.append(
                            "Lines around the error in your previous draft (data, not "
                            "instructions). Check quote and bracket nesting of every Python "
                            "literal there, then the JSON escaping of the whole file:\n"
                            + context)
                        draft = shard_source(design_data)  # revise this draft in place next
                    self.notify(f"[plan] {tid} test-design {number}: {str(error)[:400]}")
                rejected.append(errors)
                revisions += 1
                mark = boundary_fingerprint(errors[:1])
                if mark in marks:
                    code = "repeated_lint_error"
                    break
                marks.add(mark)
            stopped.set()
            raise PlanError(f"test-designer {tid} failed lint ({code})", errors)

        # Same shards, same pool: start independent integration shards and shards with larger
        # dependency closures first, so the usually longest designs do not queue behind short
        # ones. A heuristic, not an optimal schedule; results return in assignment order.
        depth = {task["id"]: len(closure(graph, task["id"])) for task in tasks}
        order = sorted(range(len(assignments)), key=lambda index: (
            not assignments[index][1], -depth[assignments[index][0]["id"]], index))
        try:
            with ThreadPoolExecutor(max_workers=min(jobs, len(assignments))) as pool:
                finished = list(pool.map(design, [assignments[index] for index in order]))
            shards = [shard for _, shard in sorted(zip(order, finished, strict=True),
                                                   key=lambda pair: pair[0])]
            if not self.generic:
                plan = assemble(boundary, {"files": [f for files, _ in shards for f in files]},
                                inputs.existing, existing_edits=True)
            else:
                plan = orch_generic.assemble(boundary, [
                    (task["id"], files, checks[(task["id"], integration)])
                    for (task, integration), (files, _) in zip(assignments, shards, strict=True)
                ], inputs.existing, existing_edits=True)
            problems = self._pack_errors(plan, inputs.base, inputs.spec)
        except PlanError as error:
            return error
        except Conflict:
            raise  # recovery/receipt conflicts are not model-output lint failures
        except (ValueError, TypeError, KeyError, SyntaxError) as error:
            return Draft(None, len(seen), [*seen, [str(error)]])
        rejected = [*seen, *(errors for _, failures in shards for errors in failures)]
        return Draft(None if problems else plan, len(rejected),
                     [*rejected, problems] if problems else rejected)

    def _call_planning(
        self, run_id: str, route: Route, prompt: str, schema: dict[str, object],
        call_id: str, *, purpose: str, task_id: str = "", attempt: int = 0,
        sample: str = "", base: str = "", prefix: str = "", rules: list[str] | None = None,
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
                scope="planning", base=base, prefix=prefix, rules=rules,
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

    def _coverage_gaps(
        self, run_id: str, base: str, boundary: dict[str, Any], graph: dict[str, Any],
        task: dict[str, Any], integration: bool, files: list[dict[str, str]], number: int,
    ) -> list[str]:
        """Stated behaviours of the shard's requirements that no test asserts ([] if none,
        or if the review itself fails: it never blocks planning)."""
        texts = {item["id"]: item["text"] for item in boundary.get("requirements", [])
                 if isinstance(item, dict) and isinstance(item.get("text"), str)}
        owners = (closure(graph, task["id"]) | {task["id"]}) if integration else {task["id"]}
        reqs = sorted({req for item in boundary["tasks"] if item["id"] in owners
                       for req in item["reqs"]})
        assigned = {req: texts[req] for req in reqs if req in texts}
        if not assigned or len(files) != 1:
            return []
        prompt = coverage_prompt(assigned, files[0]["path"], files[0]["content"], integration)
        purpose = "integration-coverage-review" if integration else "coverage-review"
        route = {"planner": self._claude_route(run_id, planning=True),
                 "opus-low": replace(CLAUDE_PLAN, effort="low"), "sonnet": SONNET_PLAN,
                 "sonnet-high": replace(SONNET_PLAN, effort="high"),
                 "dsf": DSF_EXTENDED}[REVIEW_MODEL]
        result = self._call_planning(
            run_id, route, prompt, COVERAGE_SCHEMA,
            f"orch-{run_id}-{purpose}-{task['id']}-{number}-{uuid.uuid4().hex[:12]}",
            purpose=purpose, task_id=task["id"], attempt=number, base=base,
        )
        if result.outcome != "ok":
            return []
        missing, wrong = result.data.get("missing"), result.data.get("wrong")
        gaps = [f"missing {item['req']}: {item['clause']}"[:300]
                for item in (missing if isinstance(missing, list) else [])[:8]
                if isinstance(item, dict) and isinstance(item.get("req"), str)
                and isinstance(item.get("clause"), str)]
        gaps += [f"wrong {item['test']}: {item['problem']}"[:300]
                 for item in (wrong if isinstance(wrong, list) else [])[:8]
                 if isinstance(item, dict) and isinstance(item.get("test"), str)
                 and isinstance(item.get("problem"), str)]
        return gaps

    def _ask_planner(
        self, call_id: str, prompt: str, schema: dict[str, object], *, run_id: str,
        purpose: str, attempt: int, sample: str = "", base: str = "", scope: str = "planning",
        rules: list[str] | None = None,
    ) -> dict[str, object] | PlanError:
        route = self._claude_route(run_id, planning=True)
        call = f"{call_id}-{uuid.uuid4().hex[:12]}"
        result = (self._call_planning(
            run_id, route, prompt, schema, call, purpose=purpose, attempt=attempt,
            sample=sample, base=base, rules=rules,
        ) if scope == "planning" else self._call_model(
            run_id, route, prompt, schema, call, purpose=purpose, attempt=attempt,
            sample=sample, scope=scope, base=base, rules=rules,
        ))
        if result.outcome != "ok":
            return PlanError(f"planner returned {result.outcome}: {result.message}")
        return result.data

    def _draft_modules(
        self, run_id: str, inputs: "PlanInputs", attempts: int
    ) -> "Draft | PlanError":
        """Module boundaries, then every module's sub-plan in parallel, then one merge."""
        active = {rule.id for rule in self._rules("planner") if rule.status == "active"}
        rule_ids = [rule_id for rule_id in inputs.rule_ids if rule_id in active]
        seen: list[list[str]] = []
        errors: list[str] = []
        top: dict[str, object] = {}
        for attempt in range(1, attempts + 1):
            self.notify(f"[plan] asking {self._claude_route(run_id, planning=True).label} "
                        f"for module boundaries ({attempt})")
            answer = self._ask_planner(
                f"orch-{run_id}-modules-{attempt}",
                modules_prompt(inputs.spec, inputs.repo_map, errors, inputs.rules),
                MODULES_SCHEMA,
                run_id=run_id, purpose="modules", attempt=attempt, base=inputs.base,
                rules=rule_ids,
            )
            if isinstance(answer, PlanError):
                return answer
            top, errors = answer, module_lint(answer, inputs.existing)
            if not errors:
                break
            seen.append(errors)
        if errors:
            return Draft(None, attempts, seen)
        listed = cast(list[dict[str, object]], top["modules"])
        self.notify(f"[plan] {len(listed)} module(s); planning each in parallel")

        def plan_module(position: int) -> tuple[dict[str, object] | None, int, list[list[str]]]:
            module = listed[position]
            first, last = id_range(position, len(listed))
            problems: list[str] = []
            rejected: list[list[str]] = []
            for attempt in range(1, attempts + 1):
                sub = self._ask_planner(
                    f"orch-{run_id}-module-{module['name']}-{attempt}",
                    submodule_prompt(top, module, first, last, problems, inputs.rules,
                                     original_spec=inputs.spec),
                    PLAN_SCHEMA,
                    run_id=run_id, purpose="module", attempt=attempt,
                    sample=str(module["name"]), base=inputs.base,
                    rules=rule_ids,
                )
                if isinstance(sub, PlanError):
                    raise sub
                problems = submodule_lint(top, module, first, last, sub) or lint(
                    sub, inputs.existing
                )
                if not problems:
                    return sub, attempt - 1, rejected
                rejected.append([f"{module['name']}: {item}" for item in problems])
            return None, attempts, rejected

        try:
            with ThreadPoolExecutor(max_workers=min(4, len(listed))) as pool:
                planned = list(pool.map(plan_module, range(len(listed))))
        except PlanError as error:
            return error
        repairs = len(seen) + sum(fixes for _, fixes, _ in planned)
        seen += [errors for _, _, rejected in planned for errors in rejected]
        if any(sub is None for sub, _, _ in planned):
            return Draft(None, repairs, seen)
        merged = merge(top, [sub for sub, _, _ in planned if sub is not None])
        problems = lint(merged, inputs.existing) or self._pack_errors(
            merged, inputs.base, inputs.spec)
        if problems:
            return Draft(None, repairs, [*seen, problems])
        return Draft(merged, repairs, seen)

    def _score(self, run_id: str, base: str, draft: "Draft", coupling: list[Coupling]) -> None:
        assert draft.plan is not None
        draft.vacuous, draft.probe_note = self._probe_plan(run_id, base, draft.plan)
        draft.splits = coupled_splits(draft.plan, coupling)
        draft.score, draft.parts = plan_score(
            draft.plan, len(draft.vacuous), len(draft.splits), draft.repairs
        )

    def _engine_key(self) -> dict[str, str]:
        return {"engine": "generic"} if self.generic else {}

    def _test_schema(self) -> dict[str, object]:
        return orch_generic.GENERIC_TEST_SCHEMA if self.generic else TEST_SCHEMA

    def _red_problem(self, run_id: str, base: str, boundary: dict[str, object],
                     files: list[dict[str, str]], check: "orch_verify.Check") -> str:
        """A designed check must run and fail on the stubs; "" when it does."""
        tree = {f["path"]: f["content"]
                for f in [*cast(list[dict[str, str]], boundary["files"]), *files]}
        with self.worktree(run_id, "red", base) as path:
            apply_edits(path, list(tree), [
                {"path": name, "search": "", "replace": content}
                for name, content in tree.items()])
            usable, problem = orch_verify.red(path, check)
        return "" if usable else problem

    def _probe_plan(
        self, run_id: str, base: str, plan: dict[str, object]
    ) -> tuple[list[str], str]:
        """Tasks whose frozen tests already pass on the stubs (they prove nothing).

        One sandbox run per task answers that directly: its tests either all pass on
        the stubs (vacuous) or the run fails. Naming individual failures is not needed,
        and a long failing report (cut to its tail, or counting a setUp and a cleanup
        error for one test) used to force a bisection down to single tests: Linux run
        r6f34ed35 spent 42 seconds of its critical path here on 51 tests."""
        tasks = tasks_from(plan)
        with self.worktree(run_id, "probe", base) as path:
            files = plan_files(plan)
            apply_edits(path, list(files), [
                {"path": name, "search": "", "replace": content} for name, content in files.items()
            ])
            scratch = self.scratch(run_id)

            def vacuous(task: OrchTask) -> bool:
                return not task.tests or self.verify(path, scratch, base, list(task.tests))[0]

            try:
                with ThreadPoolExecutor(max_workers=max(1, self.max_verify_jobs)) as pool:
                    flags = list(pool.map(vacuous, tasks))
            except Exception as error:  # noqa: BLE001 - probing is advisory
                return [], str(error)[:200]
        return [task.id for task, flag in zip(tasks, flags, strict=True) if flag], ""

    def _settle_planner_rules(self) -> list[tuple[str, dict[str, object]]]:
        if self.policy is not None:
            return []
        trusted, rho, runs = self.proxy_trust()
        notes: list[tuple[str, dict[str, object]]] = []
        for rule in self.state.rules("planner"):
            status, note = planner_verdict(rule, trusted)
            if status != rule.status:
                self.state.save_rule(replace(rule, status=status, note=note))
                notes.append((f"rule.{status}", {"rule": rule.id, "note": note,
                                                  "proxy_trusted": trusted, "rho": rho}))
                self.notify(f"[rules] planner {rule.id} {status}: {note}")
        return notes

    def proxy_trust(self) -> tuple[bool, float | None, int]:
        """(proxy trusted, Spearman rho of proxy score vs. real success, runs compared).

        Until PROXY_RUNS runs have both, the proxy is trusted provisionally."""
        history = self.state.planning_history()
        if len(history) < PROXY_RUNS:
            return True, None, len(history)
        rho = spearman([score for _, score, _ in history],
                       [1 - blocked for _, _, blocked in history])
        return rho >= PROXY_RHO, round(rho, 3), len(history)

    def evolve(self, rule_id: str = "", replays: int = 3, attempts: int = 2) -> list[str]:
        """Offline replay for a planner candidate: re-plan recent specs at their bases
        with and without it (paired) and judge each draft on the proxy only (lint
        repairs, vacuous tests, split coupling). Nothing is built; status changes wait
        for the next plan, the planner rules' checkpoint boundary."""
        if self.strategy_override == "sonnet-dsf":
            raise ValueError("sonnet-dsf does not support evolve")
        candidates = [
            rule for rule in self.state.rules("planner")
            if rule.status == "candidate" and (not rule_id or rule.id == rule_id)
        ]
        if rule_id and not candidates:
            raise KeyError(f"{rule_id} is not a planner candidate")
        history = self.state.runs()[:replays]
        if any(run.strategy == "sonnet-dsf" for run in history):
            raise ValueError("sonnet-dsf replay/evolve is not supported")
        if not candidates or not history:
            return ["nothing to replay: " + ("no planner candidates" if not candidates
                                             else "no past runs")]
        rule = min(candidates, key=lambda r: (r.proxy_with_total + r.proxy_without_total,
                                              int(r.id[1:])))
        rules_text, active_ids, _ = layer(self.state.rules("planner"), "planner")
        lines = [f"replaying {len(history)} spec(s) with and without {rule.id}: {rule.text}"]
        for past in history:
            try:
                # Probes use the past run's work dir: never while that run executes.
                with self.exclusive(past.id):
                    row = self._replay(past, rule, rules_text, attempts, active_ids)
            except RuntimeError as error:
                lines.append(f"  {past.id}: skipped ({error})")
                continue
            if isinstance(row, PlanError):
                return [*lines, f"  {past.id}: stopped: {row}"]
            lines.append(f"  {past.id}: {row}")
        judged = next(r for r in self.state.rules("planner") if r.id == rule.id)
        trusted, _, _ = self.proxy_trust()
        status, note = planner_verdict(judged, trusted)
        decision = ("snapshot and held-out evaluation required; active policy stays frozen"
                    if self.policy is not None else
                    (f"becomes {status} at the next plan ({note})" if status != judged.status
                     else "not decided yet"))
        lines.append(
            f"{judged.id} proxy with {judged.proxy_with_passed}/{judged.proxy_with_total}, "
            f"without {judged.proxy_without_passed}/{judged.proxy_without_total}: "
            + decision
            + ("" if trusted else " [proxy untrusted: judged on real outcomes]")
        )
        return lines

    def _replay(
        self, past: Run, rule: Rule, rules_text: str, attempts: int, active_ids: list[str],
    ) -> str | PlanError:
        """One paired replay of a past spec: with and without the candidate rule."""
        coupling = self.index.cochange(past.base)
        existing = frozenset(
            self.git(self.repo, "ls-tree", "-r", "--name-only", past.base).splitlines()
        )
        planner_map = self.index.at(past.base).repo_map(PLANNER_MAP_BYTES)
        row = []
        for shown in (True, False):
            inputs = PlanInputs(past.spec, past.base, existing, planner_map,
                                coupling_text(coupling), rules_text, rule.text if shown else "",
                                tuple([*active_ids, *([rule.id] if shown else [])]))
            outcome = self._draft(past.id, inputs, "", "ew" if shown else "eo", attempts)
            if isinstance(outcome, PlanError):
                return outcome
            if outcome.plan is not None:
                self._score(past.id, past.base, outcome, coupling)
            passed = outcome.plan is not None and not (
                outcome.repairs or outcome.vacuous or outcome.splits
            )
            self.state.plan_outcome(
                past.id, rule.id, shown, passed, "replay",
                {"score": outcome.score, "repairs": outcome.repairs,
                 "vacuous": outcome.vacuous, "splits": outcome.splits},
            )
            row.append(f"{'with' if shown else 'without'} {'ok' if passed else 'fail'} "
                       f"{outcome.score:.2f}")
        return ", ".join(row)

    def _planner_trial(self, run_id: str) -> tuple[Rule, bool] | None:
        if self.policy is not None:
            return None
        candidates = [r for r in self.state.rules("planner") if r.status == "candidate"]
        if not candidates:
            return None
        rule = min(candidates, key=lambda r: (
            r.proxy_with_total + r.proxy_without_total + r.with_total + r.without_total,
            int(r.id[1:]),
        ))
        return rule, arm(rule.id, run_id, 0)

    def _learn(
        self, run_id: str, text: str, source: str, kind: str, task_id: str = ""
    ) -> None:
        """Turn a lesson into a candidate rule of `kind` (duplicates are ignored)."""
        if run_id and self._run_strategy(run_id) == "sonnet-dsf":
            return
        found = self.state.add_rule(text, source, kind)
        if found is None:
            return
        if run_id:
            self.state.record(run_id, "rule.candidate", {"rule": found.id, "kind": kind},
                              task_id)
        self.notify(f"[rules] new {kind} candidate {found.id}: {found.text}")

    def _pack_errors(
        self, plan: dict[str, object], base: str, spec: str = "", *, only_task: str = "",
    ) -> list[str]:
        """Tasks whose owned files and frozen tests cannot fit one DSF prompt."""
        context = RunContext.of_plan(plan)
        sources = self.sources(base)
        tasks = tasks_from(plan)
        index = RepoIndex.from_sources({**sources, **context.files}, self.index.source_roots)
        controls = self._project_controls(
            [*context.files, *(p for task in tasks for p in task.files)])
        errors = [f"project pytest collection control is frozen: {path}" for path in controls]
        for task in tasks:
            if only_task and task.id != only_task:
                continue
            owned = {p: context.files.get(p, sources.get(p, "")) for p in task.files}
            owned = owned_sources(index, owned, task.tests)
            try:
                context_pack(task, context.requirements, "", context.interfaces_for(task, index),
                             owned, context.tests_for(task, sliced=True),
                             DSF_BUDGET - len(self.environment_text.encode()),
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
        draft: "Draft", rules: list[str], environment: PytestEnvironment | None = None,
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
        if self.generic:
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
                f"orch plan {run_id}: {summary}\n\nOrch-Run: {run_id}\nOrch-Plan: {hashed}\n"
                f"Orch-Rules: {', '.join(rules) or 'none'}\n",
                author=f"orch {self._claude_route(run_id, planning=True).label}",
            )
        else:
            parents = self.git(self.repo, "rev-list", "--parents", "-n", "1", head).split()[1:]
            identity = parse_trailers(self.git(self.repo, "show", "-s", "--format=%B", head))
            changed = set(self.git(self.repo, "diff", "--name-only", base_sha, head).splitlines())
            if (parents != [base_sha] or identity.get("Orch-Run") != run_id
                    or identity.get("Orch-Plan") != hashed or changed != set(files)
                    or self.index.read(head, files) != files
                    or self.git(integration, "status", "--porcelain")):
                raise PlanError("Existing checkpoint differs from the saved plan; preserve it")
        checkpoint = self.git(integration, "rev-parse", "HEAD")
        tag = f"orch/{run_id}/plan"
        if self.git(self.repo, "tag", "--list", tag):
            if self.git(self.repo, "rev-parse", f"{tag}^{{commit}}") != checkpoint:
                raise PlanError("Planning tag changed; preserve it for inspection")
        else:
            self.git(self.repo, "tag", tag, checkpoint)
        indexed = self.index.snapshot(checkpoint, f"{run_id}/plan")
        baseline, baseline_note = self._baseline(run_id, base_sha)
        run = Run(
            id=run_id,
            repo=str(self.repo),
            base=base_sha,
            branch=branch,
            spec=spec,
            plan=plan,
            plan_hash=hashed,
            repo_map=self.index.at(checkpoint).repo_map(BUILDER_MAP_BYTES),
            created_at=self.clock(),
            checkpoint=checkpoint,
            baseline=baseline,
            verified_head=checkpoint,
            policy_id=self.policy.id if self.policy else "",
            environment_root=str(environment.root) if environment else "",
            environment_fingerprint=environment.fingerprint if environment else "",
            strategy=self._run_strategy(run_id),
        )
        tasks = tasks_from(plan)
        self.state.create_run(run, tasks)
        self.state.record(run_id, "plan.baseline", {"tests": len(baseline), "note": baseline_note})
        self.state.record(run_id, "index.snapshot", {"revision": checkpoint, "files": indexed})
        if draft.probe_note:
            self.state.record(run_id, "plan.probe_skipped", {"error": draft.probe_note})
        for task in tasks:
            if task.id in draft.vacuous:
                self.state.record(run_id, "plan.vacuous_tests", {"tests": task.tests}, task.id)
                self.notify(f"[plan] warning: {task.id} tests already pass on the stubs")
        self.notify(f"[plan] {run_id}: {len(tasks)} task(s), plan {hashed}, branch {branch}")
        return run

    def _baseline(self, run_id: str, base: str) -> tuple[list[str], str]:
        """Pre-existing unittest ids that pass at the base; the final gate keeps them green."""
        if self.generic:  # project-wide checks come from the plan's own test designers
            return [], "no pre-existing checks declared"
        ids: list[str] = []
        for path, source in sorted(self.sources(base).items()):
            if LiveWorkspace.is_test_file(path):
                try:
                    ids += unittest_ids(path, source)
                except SyntaxError:
                    continue
        if not ids:
            return [], "no pre-existing tests"
        with self.worktree(run_id, "baseline", base) as tree:
            try:
                passed, _ = self.verify(tree, self.scratch(run_id), base, ids)
            except Exception as error:  # noqa: BLE001 - the gate is skipped, not the run
                return [], f"baseline not runnable: {str(error)[:120]}"
        return (ids, "green") if passed else ([], "pre-existing tests already fail; gate off")

    def _project_controls(self, paths: Iterable[str]) -> list[str]:
        """Project collection hooks stay frozen throughout generation and repairs."""
        if self.environment is None:
            return []
        return sorted({path for path in paths
                       if PurePosixPath(path).name.casefold() == "conftest.py"})

    def _project_tests(
        self, run_id: str, revision: str, environment: PytestEnvironment,
    ) -> tuple[bool, str]:
        """All project pytest tests, at pinned bytes, through the existing verification pool.

        A large suite runs as size-balanced shards of test files in parallel sandboxes
        (NetworkX: ~5400 tests, ~105 s in one process, on the critical path at the end of
        every run). Only an all-green sharded run is accepted as green; any red shard is
        confirmed by the original single full run, whose verdict stands.
        """
        groups = project_test_shards(
            self.git(self.repo, "ls-tree", "-r", "-l", revision),
            min(PROJECT_TEST_SHARDS, self.max_verify_jobs))
        if len(groups) < 2:
            return self._project_tests_once(run_id, revision, environment, None, "project")
        with ThreadPoolExecutor(max_workers=len(groups)) as pool:
            results = list(pool.map(
                lambda item: self._project_tests_once(
                    run_id, revision, environment, item[1], f"project-{item[0]}"),
                enumerate(groups)))
        if all(passed for passed, _ in results):
            return True, "\n".join(f"[shard {index + 1}/{len(groups)}] {report.strip()[-1500:]}"
                                   for index, (_, report) in enumerate(results))
        return self._project_tests_once(run_id, revision, environment, None, "project")

    def _project_tests_once(
        self, run_id: str, revision: str, environment: PytestEnvironment,
        names: list[str] | None, label: str,
    ) -> tuple[bool, str]:
        with self.worktree(run_id, label, revision) as tree:
            return self.verify(tree, self.scratch(run_id), revision, names,
                               runner=partial(LiveWorkspace.run_pytest, environment=environment),
                               timeout=PROJECT_TEST_TIMEOUT)

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
        self, run_id: str, jobs: int | None = None, mutants: int = 0,
        verify_jobs: int | None = None,
        rpm: float = 0, max_cents: int = 0, cache_mode: str | None = None,
    ) -> Run:
        """Run (or resume) every task.

        `jobs` caps concurrent model calls (AIMD starts there), `verify_jobs` concurrent
        sandbox runs (default: CPU count), `rpm` builder calls started per minute across
        the run, `max_cents` this run's model spend (a kill switch on top of the DSH
        budget). `mutants` > 0 then scores each merged task's tests with that many AST
        mutants (advisory: it never fails the run).
        """
        if cache_mode not in {None, "auto", "off"}:
            raise ValueError("cache_mode must be auto or off")
        with self.exclusive(run_id):
            saved = self.state.run(run_id)
            if (saved.strategy not in {"opus-dsf", "sonnet-dsf"}
                    or (saved.strategy == "sonnet-dsf"
                        and (saved.policy_id or self.jev_mode != "off"))):
                return self.state.update_run(run_id, "run.strategy_invalid", {}, status="paused",
                                             message="Unsupported saved strategy configuration")
            self.strategy = saved.strategy
            self.policy = self.policies.get(saved.policy_id) if saved.policy_id else None
            tuning = self.policy.tuning if self.policy else None
            # An explicit CLI value remains an execution override, recorded below.
            jobs = jobs if jobs is not None else tuning.jobs if tuning else 4
            verify_jobs = (verify_jobs if verify_jobs is not None else
                           tuning.verify_jobs if tuning else 0)
            cache_mode = cache_mode or (tuning.cache_mode if tuning else "auto")
            self.cache_mode = cache_mode
            # Resume follows the persisted checkpoint, including legacy environment-off runs.
            self.bind_environment()
            if saved.environment_root or saved.environment_fingerprint:
                stage = "environment validation"
                try:
                    if (not sandbox_available() or not saved.environment_root
                            or not saved.environment_fingerprint):
                        raise ValueError("Missing or unsupported pinned project environment")
                    self.bind_environment(saved.environment_root, saved.environment_fingerprint)
                    assert self.environment is not None
                    stage = "collection contract validation"
                    stored_tasks = [*tasks_from(saved.plan), *self.state.tasks(run_id)]
                    paths = [*plan_files(saved.plan),
                             *(p for task in stored_tasks for p in task.files)]
                    if self._project_controls(paths):
                        raise ValueError("Project collection control is frozen")
                    stage = "baseline evidence recovery"
                    identity = {"revision": saved.base, "environment_root": saved.environment_root,
                                "environment_fingerprint": saved.environment_fingerprint}
                    if not any(event.kind == "plan.project_baseline"
                               and event.data.get("passed") is True
                               and all(event.data.get(key) == value
                                       for key, value in identity.items())
                               for event in self.state.events(run_id)):
                        passed, report = self._project_tests(run_id, saved.base, self.environment)
                        self.state.record(run_id, "plan.project_baseline", {
                            **identity, "passed": passed, "report": report[-4000:],
                            "recovered": True,
                        })
                        if not passed:
                            raise ValueError("Pinned project baseline could not be re-proven")
                except Exception as error:
                    return self.state.update_run(run_id, "run.environment_invalid", {
                        "environment_root": saved.environment_root,
                        "environment_fingerprint": saved.environment_fingerprint,
                        "error": type(error).__name__, "stage": stage,
                    }, status="paused",
                        message="Pinned project pytest environment/contract is invalid")
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
            self.cache_policy = CachePolicy(self.clock)
            self.cache_wait_started.clear()
            for receipt in self.state.usage.calls(limit=128):
                if (receipt.prefix_hash and receipt.finished_at is not None
                        and receipt.usage_valid is True):
                    self.cache_policy.observe(
                        (receipt.provider, receipt.model, receipt.prefix_hash),
                        receipt.started_at, receipt.finished_at, receipt.usage, receipt.outcome,
                    )
            self.metrics = {}
            self.state.update_run(
                run_id, "run.started",
                {"jobs": jobs, "verify_jobs": self.max_verify_jobs, "rpm": rpm,
                 "max_cents": max_cents, "cache_mode": cache_mode,
                 "policy_id": saved.policy_id},
                status="running",
            )
            self._load_rules(run_id)
            while True:
                self._drain(run_id)
                tasks = self.state.tasks(run_id)
                if self.stop_reason or not all(t.status in FINISHED for t in tasks):
                    break
                if self._final_gate(run_id):
                    break
            if mutants and not (self.stop_reason or self.gate_failure):
                self.mutation(run_id, mutants, jobs=jobs)
            if self.bucket is not None:
                self.metrics["paced_seconds"] = round(self.bucket.waited)
            self.state.record(run_id, "run.metrics", dict(self.metrics))
            self.state.record(run_id, "run.cache", {
                **self.cache_policy.snapshot(), "mode": self.cache_mode,
                "affinity_dispatches": self.metrics.get("cache_affinity_dispatches", 0),
                "wait_checks": self.metrics.get("cache_wait_checks", 0),
                "waited_ms": self.metrics.get("cache_waited_ms", 0),
            })
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

    def _rules(self, kind: str) -> list[Rule]:
        return ([rule for rule in self.policy.rules if rule.kind == kind]
                if self.policy is not None else self.state.rules(kind))

    def _load_rules(self, run_id: str) -> None:
        """Checkpoint boundary: settle candidates, then freeze the active rule layer."""
        if self._run_strategy(run_id) == "sonnet-dsf":
            self.rules_text, self.rule_ids = "", []
            self.state.record(run_id, "run.rules", {"active": [], "strategy": "sonnet-dsf"})
            return
        if self.policy is not None:
            self.rules_text, self.rule_ids, _ = layer(self._rules("builder"))
            self.state.record(run_id, "run.rules", {"active": self.rule_ids,
                                                    "policy_id": self.policy.id})
            return
        for rule in self.state.rules("builder"):
            status, note = verdict(rule)
            if status != rule.status:
                self.state.save_rule(replace(rule, status=status, note=note))
                self.state.record(run_id, f"rule.{status}", {"rule": rule.id, "note": note})
                self.notify(f"[rules] {rule.id} {status}: {note}")
        self.rules_text, self.rule_ids, evicted = layer(self.state.rules("builder"))
        for rule in evicted:
            self.state.save_rule(replace(rule, status="retired", note="evicted by rule budget"))
            self.state.record(run_id, "rule.retired", {"rule": rule.id, "note": "budget"})
        self.state.record(run_id, "run.rules", {"active": self.rule_ids})

    def _trial(self, run_id: str, task: OrchTask) -> tuple[str, bool] | None:
        """The least-tested candidate rule and this attempt's arm, if any candidate exists."""
        if self.policy is not None or self._run_strategy(run_id) == "sonnet-dsf":
            return None
        candidates = [rule for rule in self.state.rules("builder") if rule.status == "candidate"]
        if not candidates:
            return None
        rule = min(candidates, key=lambda r: (r.with_total + r.without_total, int(r.id[1:])))
        chosen = (rule.id, arm(rule.id, task.id, task.attempts))
        with self.trial_lock:
            self.trials[(run_id, task.id, task.attempts)] = chosen
        return chosen

    def _observe(self, run_id: str, task_id: str, attempt: int, passed: bool) -> None:
        with self.trial_lock:
            trial = self.trials.pop((run_id, task_id, attempt), None)
        if trial is not None:
            self.state.rule_outcome(trial[0], trial[1], passed, run_id, task_id, attempt)

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
                    self._breaker(run_id)
                    ready = self._ready(run_id, set(futures.values()))
                    ready_ids = {task.id for task in ready}
                    self.cache_wait_started = {k: v for k, v in self.cache_wait_started.items()
                                               if k in ready_ids}
                    room = min(self._room(len(self.generating)), workers - len(futures))
                    if (self.limit == self.max_jobs
                            and room == max(0, self.max_jobs - len(self.generating))):
                        self._count("ready_backlog_peak", max(0, len(ready) - room), peak=True)
                    reserved = False
                    for task in ready:
                        if room <= int(reserved):
                            break
                        key = self.ready_cache_keys.get(task.id)
                        delay = self.cache_policy.delay(key) if key else 0.0
                        if delay > 0:
                            if task.id not in self.cache_wait_started:
                                self.cache_wait_started[task.id] = self.clock()
                                self.state.record(run_id, "task.cache_wait", {
                                    "prefix": key[2] if key else "",
                                    "recommended_seconds": delay, "cap_seconds": MAX_WAIT_SECONDS,
                                }, task.id, task.attempts + 1)
                            if self.clock() - self.cache_wait_started[task.id] < MAX_WAIT_SECONDS:
                                self._count("cache_wait_checks")
                                reserved = True  # keep one slot for the delayed critical task
                                continue
                        began = self.cache_wait_started.pop(task.id, None)
                        if began is not None:
                            self._count("cache_waited_ms", round(1000 * (self.clock() - began)))
                        affinity = self.cache_policy.affinity(key) if key else 0.0
                        if affinity > 0:
                            self._count("cache_affinity_dispatches")
                        started = self.state.transition(
                            run_id, task.id, "RUNNING", "task.started",
                            {"limit": self.limit, "cache_affinity": affinity,
                             "cache_prefix": key[2] if key else ""},
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
                        if self.cache_wait_started:
                            break  # reconsider cache deadlines even if a warming call stalls
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

    def _breaker(self, run_id: str) -> None:
        """A module with BREAKER parked tasks stops; the rest of the DAG continues."""
        tasks = self.state.tasks(run_id)
        parked: dict[str, int] = {}
        for task in tasks:
            if task.status == "PARKED" and not task.reason.startswith("breaker:"):
                parked[module_key(task)] = parked.get(module_key(task), 0) + 1
        for task in tasks:
            count = parked.get(module_key(task), 0)
            if task.status in {"PLANNED", "READY"} and count >= BREAKER:
                reason = f"breaker: module {module_key(task)} has {count} parked tasks"
                self.state.transition(run_id, task.id, "PARKED", "task.parked",
                                      {"reason": reason}, reason=reason)
                self.notify(f"[run] {task.id} PARKED: {reason}")

    def _final_gate(self, run_id: str) -> bool:
        """Pre-existing tests and every merged task's tests must pass together at the
        end (merge gates ran only the tests a change could affect); a regression is
        bisected to one task commit, reverted and rebuilt.

        Returns True when the run may finish, False when tasks were reopened.
        """
        run = self.state.run(run_id)
        done = [task for task in self.state.tasks(run_id) if task.status == "DONE"]
        merged = [test for task in done for test in task.tests]
        if not run.baseline and not merged and not run.environment_root:
            return True
        integration = self.integration(run_id)
        head = self.git(integration, "rev-parse", "HEAD")

        def green(revision: str, tests: list[str]) -> tuple[bool, str]:
            with self.worktree(run_id, "final", revision) as path:
                return self.verify(path, self.scratch(run_id), revision, tests)

        passed, report = green(head, [*run.baseline, *merged])
        self.state.record(
            run_id, "run.final_gate", {"passed": passed, "head": head,
                                       "tests": len(run.baseline) + len(merged)},
        )
        if passed:
            if run.environment_root:
                try:
                    controls = self._project_controls(self.git(
                        self.repo, "diff", "--name-only", run.base, head).splitlines())
                    if controls:
                        passed = False
                        report = "Frozen project collection control changed: " + ", ".join(controls)
                    else:
                        environment = PytestEnvironment.load(Path(run.environment_root))
                        if environment.fingerprint != run.environment_fingerprint:
                            raise ValueError("Pinned environment fingerprint changed")
                        passed, report = self._project_tests(run_id, head, environment)
                except Exception as error:
                    passed, report = False, f"{type(error).__name__} during project pytest gate"
                self.state.record(run_id, "run.project_final", {
                    "revision": head, "environment_root": run.environment_root,
                    "environment_fingerprint": run.environment_fingerprint,
                    "passed": passed, "report": report[-4000:],
                })
                if not passed:
                    self.gate_failure = "project pytest gate failed: " + report[-200:]
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
            failing = (failed_in(report, merged) if not self.generic
                       else orch_verify.failed(report, merged)) or []
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

    def _proven(
        self, run_id: str, done: list[OrchTask], commits: list[str]
    ) -> dict[str, str]:
        """test id -> the latest commit whose gate proved it green: its owner's merge, or
        the harden commit that added it (a hardened test does not exist before that)."""
        seen: dict[str, list[str]] = {}
        for task in done:
            for test in task.tests:
                seen.setdefault(test, []).append(task.commit)
        for event in self.state.events(run_id):
            if event.kind == "plan.tests_hardened":
                for test in cast(list[str], event.data["tests"]):
                    seen.setdefault(test, []).append(str(event.data["commit"]))
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
            level=1, used_levels=[0], last_error=(evidence + report)[-3000:],
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
        self.ready_cache_keys = {}
        if (self.cache_mode == "auto" and self.cache_policy.snapshot()["samples"]
                and not any(r.status == "candidate" for r in self._rules("builder"))):
            run = self.state.run(run_id)
            context = RunContext.of(run)
            for task in ready:
                if task.level >= 2:
                    continue
                try:
                    pack = self._builder_pack(run, context, task, DSF, run.verified_head,
                                              self.integration(run_id))
                except (ValueError, OSError):
                    continue  # no prediction is safer than affinity for the wrong prefix
                self.ready_cache_keys[task.id] = (
                    DSF.provider, DSF.model, digest(self.environment_text + pack.shared)[:12])
        return sorted(ready, key=lambda task: (
            -depth[task.id],
            -self.cache_policy.affinity(self.ready_cache_keys[task.id])
            if task.id in self.ready_cache_keys else 0,
            task.id,
        ))

    # --- mutation testing and harden -------------------------------------
    def mutation(
        self, run_id: str, cap: int, only: list[str] | None = None, jobs: int = 4
    ) -> list[Report]:
        """Score merged tasks' frozen tests with AST mutants of their code at the
        integration HEAD. Without `only`, tasks already scored at their commit are skipped."""
        run = self.state.run(run_id)
        self.bind_environment(run.environment_root, run.environment_fingerprint)
        head = self.git(self.integration(run_id), "rev-parse", "HEAD")
        tasks = [
            task for task in self.state.tasks(run_id)
            if task.status == "DONE"
            and (task.id in only if only is not None else task.mutated_at != task.commit)
        ]
        if not tasks:
            return []
        self.notify(f"[mutate] scoring {len(tasks)} task(s), up to {cap} mutants each")
        with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(tasks)))) as pool:
            found = list(pool.map(lambda task: self._mutate(run_id, task, head, cap), tasks))
        return [report for report in found if report is not None]

    def _mutants(self, task: OrchTask, revision: str) -> tuple[dict[str, str], list[Mutant]]:
        """The task's Python sources at `revision` and every mutant of them."""
        sources = self.index.read(revision, [p for p in task.files if p.endswith(".py")])
        return sources, [m for path, text in sources.items() for m in mutants(path, text)]

    def _kills(
        self, run_id: str, tree: Path, revision: str, sources: dict[str, str],
        chosen: list[Mutant], tests: list[str], limit: float,
    ) -> tuple[list[Mutant], int] | None:
        """(survivors, timeouts) of `chosen` against `tests`; None when interrupted."""
        survivors: list[Mutant] = []
        timeouts = 0
        for mutant in chosen:
            if self.cancel.is_set():
                return None
            target = tree / mutant.path
            target.write_text(mutant.source)
            try:
                passed, _ = self.verify(tree, self.scratch(run_id), revision, tests,
                                        timeout=limit)
            finally:
                target.write_text(sources[mutant.path])
            if passed:
                survivors.append(mutant)
            elif self.elapsed() >= limit * 0.95:
                timeouts += 1  # an infinite loop the tests would also have caught
        return survivors, timeouts

    def _mutate(self, run_id: str, task: OrchTask, head: str, cap: int) -> Report | None:
        sources, found = self._mutants(task, head)
        chosen = select(found, cap, task.id)
        with self.worktree(run_id, f"mutate-{task.id}", head) as tree:
            green, output = self.verify(tree, self.scratch(run_id), head, task.tests)
            if not green:
                self.state.record(run_id, "task.mutation_skipped",
                                  {"reason": output.strip()[-300:]}, task.id)
                return None
            limit = mutant_limit(self.elapsed())
            outcome = self._kills(run_id, tree, head, sources, chosen, task.tests, limit)
        if outcome is None:
            return None
        survivors, timeouts = outcome
        report = Report(task.id, len(chosen), len(chosen) - len(survivors), timeouts,
                        [m.brief() for m in survivors][:MAX_SURVIVORS])
        self.state.transition(
            run_id, task.id, "DONE", "task.mutation",
            {"total": report.total, "killed": report.killed, "timeouts": timeouts,
             "score": round(report.score, 3), "survivors": report.survivors,
             "gaps": report.gaps(), "commit": task.commit},
            mutation=round(report.score, 3), mutated_at=task.commit,
        )
        if report.total and report.score < WEAK_TESTS:
            touched = self.state.discount(run_id, task.id, f"mutation score {report.score:.2f}")
            # Weak tests are a planning gap: the planner wrote them (see orch evolve).
            self.state.record(run_id, "plan.weak_tests",
                              {"score": round(report.score, 3), "gaps": report.gaps(),
                               "rules": touched}, task.id)
            for operator in report.operators()[:2]:
                self._learn(run_id, PLANNER_LESSONS[operator], f"{run_id}/{task.id} mutation",
                            "planner", task.id)
        self.notify(f"[mutate] {task.id} {report.killed}/{report.total} killed"
                    + (f"; gaps: {'; '.join(report.gaps()[:2])}" if report.survivors else ""))
        return report

    def harden(self, run_id: str, only: list[str] | None = None, cap: int = 12) -> list[str]:
        """Claude adds tests that kill a task's surviving mutants; each new test must
        pass on the merged code and kill at least one survivor before it is frozen."""
        saved = self.state.run(run_id)
        if saved.environment_root or saved.environment_fingerprint:
            raise ValueError(
                "Project pytest runs cannot be hardened until project revalidation is supported"
            )
        lines: list[str] = []
        self._restore(run_id)  # a killed harden must not leave unvalidated tests behind
        for task in self.state.tasks(run_id):
            if task.status != "DONE" or (only is not None and task.id not in only):
                continue
            if task.mutated_at != task.commit:
                self._mutate(run_id, task, self.git(self.integration(run_id), "rev-parse",
                                                    "HEAD"), cap)
                task = self.state.task(run_id, task.id)
            scored = [e.data for e in self.state.events(run_id, task.id)
                      if e.kind == "task.mutation"]
            survivors = cast(list[dict[str, object]], scored[-1]["survivors"]) if scored else []
            if not survivors:
                lines.append(f"{task.id}: no surviving mutants")
                continue
            try:
                lines.append(self._harden(run_id, task, survivors, cap))
            except AttemptFailed as error:
                lines.append(f"{task.id}: harden rejected: {error.detail[:300]}")
            except Deferred as error:
                lines.append(f"{task.id}: stopped: {error}")
                break
        return lines

    def _harden(
        self, run_id: str, task: OrchTask, survivors: list[dict[str, object]], cap: int
    ) -> str:
        run = self.state.run(run_id)
        context = RunContext.of(run)
        tests = context.tests_for(task)
        integration = self.integration(run_id)
        with self.git_lock:
            head = self.git(integration, "rev-parse", "HEAD")
        code = self.index.read(head, task.files)
        data = self._claude(
            run_id, task, "harden",
            harden_prompt(task, context.requirements, tests, code, survivors,
                          original_spec=run.spec), HARDEN_SCHEMA,
        )
        paths = sorted(tests)
        with self.integration_change(run.id) as (integration, _):
            try:
                apply_edits(integration, paths, data.get("edits"))
            except EditError as error:
                raise AttemptFailed("harden", f"edits unusable: {error}") from None
            new, plan = self._hardened_plan(run, task, paths, tests)
            hashed = plan_hash(plan)
            self.git(integration, "add", "-f", "--", *paths)
            message = commit_message(
                run.id, hashed, task, f"h{len(new)}", self._claude_route(run.id).label,
                title=f"orch harden {task.id}: {len(new)} test(s) for surviving mutants",
            )
            self.git(integration, "commit", "-m", message,
                     author=f"orch {self._claude_route(run.id).label}")
            commit = self.git(integration, "rev-parse", "HEAD")
            killed = self._validate_hardening(run, task, commit, new, survivors)
            # The commit point: the plan and verified HEAD move together.
            self.state.update_run(
                run.id, "plan.tests_hardened",
                {"task": task.id, "commit": commit, "tests": new},
                plan=plan, plan_hash=hashed, verified_head=commit,
            )
            if run.status == "done":
                self.git(self.repo, "tag", "-f", f"orch/{run.id}/done", commit)
        self.state.transition(
            run.id, task.id, "DONE", "task.hardened",
            {"commit": commit, "tests": new, "killed": killed},
            tests=[*task.tests, *new], claude_attempts=task.claude_attempts + 1,
        )
        report = self._mutate(run.id, self.state.task(run.id, task.id), commit, cap)
        score = f"; mutation {task.mutation:.2f} -> {report.score:.2f}" if report else ""
        return f"{task.id}: {len(new)} new test(s) kill {killed} survivor(s){score}"

    def _hardened_plan(
        self, run: Run, task: OrchTask, paths: list[str], frozen: dict[str, str]
    ) -> tuple[list[str], dict[str, object]]:
        """The new test ids and the revised plan; refuses edits that weaken or rename."""
        integration = self.integration(run.id)
        edited = {path: (integration / path).read_text() for path in paths}
        new: list[str] = []
        for path in paths:
            before = set(unittest_ids(path, frozen[path]))
            after = unittest_ids(path, edited[path])
            if not before <= set(after):
                raise AttemptFailed("harden", f"{path}: existing tests were removed or renamed")
            kept = method_bodies(edited[path])
            changed = [n for n, body in method_bodies(frozen[path]).items() if kept.get(n) != body]
            if changed:
                raise AttemptFailed("harden", f"{path}: existing tests changed: {changed[:3]}")
            new += [test for test in after if test not in before]
        if not new:
            raise AttemptFailed("harden", "no new test methods")
        for test in new:
            match = REQ_TEST.fullmatch(test.rsplit(".", 1)[1])
            if match is None or f"R{match.group(1)}" not in task.reqs:
                raise AttemptFailed("harden", f"{test} is not named for one of {task.reqs}")
        entries = [
            {**t.entry(), "tests": [*t.tests, *new]} if t.id == task.id else t.entry()
            for t in self.state.tasks(run.id) if t.status != "REPLANNED"
        ]
        plan = {
            **run.plan,
            "files": [{"path": p, "content": edited.get(p, c)}
                      for p, c in plan_files(run.plan).items()],
            "tasks": entries,
        }
        errors = lint(plan, fixture_paths=set(edited))
        if errors:
            raise AttemptFailed("harden", f"revised plan fails lint: {errors[:3]}")
        return new, plan

    def _validate_hardening(
        self, run: Run, task: OrchTask, commit: str, new: list[str],
        survivors: list[dict[str, object]],
    ) -> int:
        """New tests pass on the merged code, keep every merged test green and kill at
        least one survivor. Returns how many survivors they kill."""
        merged = [t for other in self.state.tasks(run.id) if other.status == "DONE"
                  for t in other.tests]
        with self.worktree(run.id, "harden", commit) as tree:
            passed, report = self.verify(tree, self.scratch(run.id), commit, [*merged, *new])
            if not passed:
                raise AttemptFailed("harden", f"new tests fail on the merged code: {report[-600:]}")
            # Mutants run the new tests alone, so alone they must pass on the real code;
            # otherwise every mutant would count as killed.
            passed, report = self.verify(tree, self.scratch(run.id), commit, new)
            if not passed:
                raise AttemptFailed("harden", f"new tests fail when run alone: {report[-600:]}")
            limit = mutant_limit(self.elapsed())
            keys = {survivor["key"] for survivor in survivors}
            sources, found = self._mutants(task, commit)
            targets = [m for m in found if m.key in keys]
            outcome = self._kills(run.id, tree, commit, sources, targets, new, limit)
        if outcome is None:
            raise Deferred("interrupted while validating new tests")
        killed = len(targets) - len(outcome[0])
        if not killed:
            raise AttemptFailed("harden", "the new tests kill none of the surviving mutants")
        return killed

    def _finish(self, run_id: str) -> Run:
        tasks = self.state.tasks(run_id)
        if self.gate_failure:
            self._plan_outcome(run_id, tasks)
            return self.state.update_run(
                run_id, "run.parked", {"reason": self.gate_failure},
                status="parked", message=self.gate_failure,
            )
        if self.stop_reason:
            return self.state.update_run(
                run_id, "run.paused", {"reason": self.stop_reason},
                status="paused", message=self.stop_reason,
            )
        self._plan_outcome(run_id, tasks)
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

    def _plan_outcome(self, run_id: str, tasks: list[OrchTask]) -> None:
        """Once per run: how many planned tasks the plan itself failed (replanned, frozen
        tests corrected, or parked by a failed replan). This is the real outcome the
        planner proxy is checked against."""
        events = self.state.events(run_id)
        if any(event.kind == "plan.outcome" for event in events):
            return
        proxy = next((event.data for event in events if event.kind == "plan.proxy"), {})
        planned = [task for task in tasks if task.generation == 0]
        fixed = {event.task_id for event in events if event.kind == "task.tests_fixed"}
        failures = sorted(
            task.id for task in planned
            if task.status == "REPLANNED" or task.id in fixed
            or (task.status == "PARKED" and task.reason.startswith("replan"))
        )
        self.state.plan_outcome(
            run_id, str(proxy.get("rule", "")), bool(proxy.get("shown")), not failures,
            "outcome",
            {"blocked_rate": round(len(failures) / max(1, len(planned)), 3),
             "planned": len(planned), "failures": failures},
        )

    def _jev(
        self, run_id: str, task_id: str, attempt: int, purpose: str,
        state: object, questions: dict[str, object],
    ) -> dict[str, object] | None:
        """One Jev decision, or None; Jev trouble never blocks the build."""
        if self.jev_mode == "off" or self.decide is None:
            return None
        call_id = f"orch-{run_id}-{task_id}-jev-{purpose}-{uuid.uuid4().hex[:12]}"
        request = dumps({"state": state, "questions": questions})
        receipt = Call(
            call_id, run_id, "jev", "typesafe-ai/jev", purpose, self.clock(),
            task_id=task_id, attempt=attempt, prompt_hash=digest(request),
            request_bytes=len(request.encode()),
        )
        with self.state.usage.capture(receipt):
            result = self.decide(state, questions, call_id)
            receipt.outcome, receipt.usage = result.outcome, result.usage
        self._spend(receipt.usage)
        self.state.record(
            run_id, "jev.call",
            {"route": JEV, "purpose": purpose, "outcome": result.outcome, "usage": result.usage,
             "call_id": call_id},
            task_id, attempt,
        )
        if result.outcome == "ok":
            return result.answers
        if result.outcome in {"auth_required", "waiting_approval", "quota_exhausted"}:
            self.jev_mode = "off"
            self.notify(f"[jev] disabled for this run: {result.outcome} ({result.message})")
        return None

    def _triage(
        self, run_id: str, task: OrchTask, number: int, used: list[int],
        upcoming: int, failure: AttemptFailed,
    ) -> int:
        """Let Jev pick the next rung among those still open; deterministic below 0.6."""
        options = [level for level in triage_options(used, self._pass_rate())
                   if level != 2 or failure.sampleable]
        if upcoming == PARK or len(options) < 2:
            return upcoming
        answers = self._jev(
            run_id, task.id, number, "triage",
            jev_state(task, failure.category, failure.detail, failure.diff),
            triage_question(options),
        )
        if answers is None:
            return upcoming
        level, confidence, name = pick(answers.get("rung"), options)
        applied = self.jev_mode == "on" and level is not None
        self.state.record(
            run_id, "jev.triage",
            {"deterministic": upcoming, "choice": name, "confidence": confidence,
             "level": level, "applied": applied},
            task.id, number,
        )
        if applied and level is not None and level != upcoming:
            self.notify(f"[jev] {task.id}: {name} ({confidence:.2f}) instead of rung {upcoming}")
        return level if applied and level is not None else upcoming

    def _pass_rate(self) -> float:
        """Laplace-smoothed first-attempt DSF pass rate over this repository's history."""
        passed, total = self.state.outcomes(DSF.label, 0)
        return (passed + 1) / (total + 2)

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
        with self.trial_lock:
            self.trials.pop((run_id, task_id, number), None)  # no A/B evidence either way
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
        """One rung of the ladder for one task."""
        if task.pending_review:
            self._review(run_id, task, "")  # the snapshot, not a new builder, owns this operation
            return
        if not task.pending_candidate and task.level == 5:
            self._replan(run_id, task, f"ladder exhausted below replan: {task.last_signature}")
            return
        if not task.pending_candidate and task.level == 4:
            task = self._thicken(run_id, task)
        route = self._claude_route(run_id) if task.level == 3 else DSF
        count = samples(self._pass_rate()) if task.level == 2 else 1
        try:
            self._build(run_id, task, route, count)
        except Signal as signal:
            try:
                self._signal(run_id, task, signal)
            except AttemptFailed as verdict:
                if (verdict.category == "dispute" and signal.failure is not None
                        and not self.state.task(run_id, task.id).pending_review):
                    raise signal.failure from None
                raise

    def _claude(
        self, run_id: str, task: OrchTask, purpose: str, prompt: str, schema: dict[str, object],
        *, review_id: str = "",
    ) -> dict[str, object]:
        """One Claude exception call; provider stops defer, other failures fail the rung."""
        route = self._claude_route(run_id)
        prefix = digest(prompt.shared)[:12] if isinstance(prompt, ContextPrompt) else ""
        if not review_id:
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
            sample=review_id, journal=self.reviews if review_id else None,
        )
        if not review_id:
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

    def _thicken(self, run_id: str, task: OrchTask) -> OrchTask:
        context = RunContext.of(self.state.run(run_id))
        with self.git_lock:
            base = self.git(self.integration(run_id), "rev-parse", "HEAD")
        owned = {
            path: Workspace.git(self.repo, "show", f"{base}:{path}", strip=False)
            for path in task.files
            if path in self.git(self.repo, "ls-tree", "-r", "--name-only", base).splitlines()
        }
        data = self._claude(
            run_id, task, "thicken",
            thicken_prompt(task, context.requirements, context.tests_for(task), owned,
                           original_spec=self.state.run(run_id).spec),
            EXCEPTION_SCHEMA,
        )
        contract = str(data.get("contract", "")).strip()
        if (not contract or data.get("verdict") != "contract" or data.get("edits")):
            raise AttemptFailed("provider", "thicken must return only a clarified contract")
        self.notify(f"[run] {task.id} contract thickened")
        return self.state.transition(
            run_id, task.id, "RUNNING", "task.thickened", {"contract_bytes": len(contract)},
            attempt=task.attempts, contract=contract, claude_attempts=task.claude_attempts + 1,
        )

    def _signal(self, run_id: str, task: OrchTask, signal: "Signal") -> None:
        if signal.status == "BLOCKED":
            self._replan(run_id, task, f"BLOCKED: {signal.reason}")
            return
        if task.disputes:
            corrected = sum(event.kind == "task.tests_fixed"
                            for event in self.state.events(run_id, task.id))
            # A correction creates a new frozen version. A rejected dispute does not:
            # never ask the reviewer again about that unchanged version, even on resume.
            if task.disputes >= MAX_TEST_REVIEWS or corrected < task.disputes:
                raise AttemptFailed("dispute", signal.reason)
        self._review(run_id, task, signal.reason, failure=signal.failure)

    def _review(
        self, run_id: str, task: OrchTask, reason: str, *, failure: AttemptFailed | None = None,
    ) -> None:
        """TEST_DISPUTE: independent review against the original user specification."""
        run = self.state.run(run_id)
        context = RunContext.of(run)
        tests = context.tests_for(task)
        saved = self.reviews.pending(run_id, task.id)
        if saved is None:
            stale = sum(e.kind == "task.review_stale"
                        for e in self.state.events(run_id, task.id))
            if stale >= MAX_TEST_REVIEWS:
                raise Parked("Frozen review inputs keep changing; automatic refresh limit reached")
            prompt = cast(ContextPrompt, review_prompt(
                task, context.requirements, tests, reason, original_spec=run.spec))
            saved = self.reviews.begin(run, task, review_version(
                run.spec, task, context.requirements, tests), prompt, tests,
                {"category": failure.category, "detail": failure.detail, "diff": failure.diff}
                if failure is not None else None)
        if (explanation := self.reviews.applied(saved)) is not None:
            self.state.record(run_id, "task.review_applied_reused", {
                "review": saved.id,
            }, task.id, task.attempts)
            raise self._review_failure(saved, explanation)
        unknown = self.reviews.acknowledge_retry(saved)
        with self.merge_lock:
            if self._stale_review(saved, unknown):
                return
        data = self._claude(
            run_id, task, "review", saved.prompt(), EXCEPTION_SCHEMA, review_id=saved.id,
        )
        explanation = str(data.get("explanation", ""))[:2000]
        if (data.get("verdict") not in {"tests_correct", "tests_wrong"}
                or not explanation.strip() or data.get("contract")
                or (data.get("verdict") == "tests_correct" and data.get("edits"))):
            raise Parked("invalid test review; cannot resolve the specification dispute")
        if data.get("verdict") != "tests_wrong":
            with self.merge_lock:
                if self._stale_review(saved):
                    return
                self.reviews.close(saved, self.state.run(run_id).plan_hash, explanation)
            raise self._review_failure(saved, explanation)
        if self._fix_tests(run, task, sorted(saved.tests), data.get("edits"), explanation,
                           review=saved) and (taught := lesson(data.get("lesson"))):
            self._learn(run_id, taught, f"{run_id}/{task.id} tests_wrong", "planner", task.id)

    @staticmethod
    def _review_failure(saved: Review, explanation: str) -> AttemptFailed:
        return AttemptFailed(saved.failure.get("category", "dispute"),
                             saved.failure.get("detail", explanation),
                             saved.failure.get("diff", ""))

    def _stale_review(self, saved: Review, unknown: list[str] | None = None) -> bool:
        """Under merge_lock, discard only settled verdicts whose reviewed inputs changed."""
        if self.cancel.is_set():
            raise Deferred("interrupted before applying test review")
        run = self.state.run(saved.run_id)
        task = self.state.task(saved.run_id, saved.task_id)
        context = RunContext.of(run)
        current = review_version(run.spec, task, context.requirements, context.tests_for(task))
        if task.pending_review != saved.id:
            raise Conflict("Review no longer owns the pending operation")
        if current != saved.version:
            if unknown is None:
                unknown = self.reviews.acknowledge_retry(saved)
            if unknown:
                raise Parked("Stale review has unresolved provider calls; inspect orch reconcile")
            self.reviews.close(saved, run.plan_hash)
            return True
        if any((self.integration(run.id) / path).read_text() != content
               for path, content in saved.tests.items()):
            raise Parked("Frozen review files differ from their recorded inputs")
        return False

    def _fix_tests(
        self, run: Run, task: OrchTask, paths: list[str], edits: object, explanation: str,
        *, review: Review | None = None,
    ) -> bool:
        """Apply Claude's test correction as a new frozen-test commit, gated like a merge."""
        expected = {path: plan_files(run.plan)[path] for path in paths}
        with self.integration_change(run.id) as (integration, _):
            run = self.state.run(run.id)
            if review is not None and self._stale_review(review):
                return False
            merged = [t for other in self.state.tasks(run.id) if other.status == "DONE"
                      for t in other.tests]
            frozen = {path: (integration / path).read_text() for path in paths}
            if review is None and (frozen != expected or any(
                    plan_files(run.plan).get(path) != content for path, content in expected.items()
            )):
                raise AttemptFailed("dispute", "test correction inputs changed after review")
            try:
                changed_paths = apply_edits(integration, paths, edits)
            except EditError as error:
                raise AttemptFailed("dispute", f"test correction unusable: {error}") from None
            weakened = [
                f"{path}::{name}"
                for path in paths
                for after in [assertions((integration / path).read_text())]
                for name, count in assertions(frozen[path]).items()
                if after.get(name, 0) < count
            ]
            if weakened:
                raise AttemptFailed(
                    "dispute", f"test correction removes assertions from {weakened[:3]}"
                )
            plan = {
                **run.plan,
                "files": [
                    {"path": path, "content": (integration / path).read_text()
                     if path in paths else content}
                    for path, content in plan_files(run.plan).items()
                ],
                "tasks": [t.entry() for t in self.state.tasks(run.id)
                          if t.status != "REPLANNED"],
            }
            errors = lint(plan, fixture_paths=set(changed_paths))
            if errors:
                raise AttemptFailed("dispute", f"test correction fails lint: {errors[:3]}")
            hashed = plan_hash(plan)
            self.git(integration, "add", "-f", "--", *paths)
            message = commit_message(
                run.id, hashed, task, f"a{task.attempts}t", self._claude_route(run.id).label,
                title=f"orch tests {task.id}: {explanation}",
            )
            self.git(integration, "commit", "-m", message,
                     author=f"orch {self._claude_route(run.id).label}")
            head = self.git(integration, "rev-parse", "HEAD")
            if merged:
                with self.worktree(run.id, "gate", head) as gate:
                    passed, report = self.verify(gate, self.scratch(run.id), head, merged)
                if not passed:
                    raise Parked(f"test correction breaks merged tasks: {report[-300:]}")
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
                attempt=task.attempts, candidate=saved, level=level,
                expected_review=review.id if review else "",
                expected_entry=review.task_entry if review else task.entry(),
            )
        self.notify(f"[run] {task.id} frozen tests corrected; "
                    + ("reverify saved code" if saved else "no eligible saved code"))
        return True

    def _replan(self, run_id: str, task: OrchTask, reason: str) -> None:
        """Rung 5: Claude re-splits the failing subtree; lint decides as for the plan."""
        if task.generation >= MAX_GENERATION:
            raise Parked(f"replan limit reached; {reason[:200]}")
        run = self.state.run(run_id)
        tasks = self.state.tasks(run_id)
        owners = [t.id for t in tasks if t.id != task.id and any(f in reason for f in t.files)]
        chosen = subtree(tasks, task.id, owners)
        first = next_task_id(tasks)
        errors: list[str] = []
        previous: object = None
        for revision in range(MAX_REPLAN_REVISIONS + 1):
            data = self._claude(
                run_id, task, "replan",
                replan_prompt(run.plan, tasks, chosen, first, task, reason,
                              original_spec=run.spec, errors=errors, previous=previous),
                REPLAN_SCHEMA,
            )
            if (taught := lesson(data.get("lesson"))):
                self._learn(run_id, taught, f"{run_id}/{task.id} replan", "planner", task.id)
            entries = data.get("tasks")
            if not isinstance(entries, list) or not entries:
                raise Parked("replan returned no tasks")
            errors = self._replan_errors(run, tasks, chosen, entries)
            if not errors:
                break
            previous = entries
            self.state.record(run_id, "task.replan_rejected", {
                "revision": revision, "errors": errors[:8],
            }, task.id, task.attempts)
        if errors:
            raise Parked(f"replan rejected by lint: {'; '.join(errors[:3])}")
        replacements = [
            replace(new, generation=task.generation + 1) for new in tasks_from({"tasks": entries})
        ]
        try:
            with self.merge_lock:
                # Parallel planners can choose the same ids from an earlier snapshot.
                # Rename their local graph before committing against the latest state.
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
                self.state.replan(
                    run_id, task.id, chosen, replacements, str(data.get("summary", ""))[:300],
                    validate=partial(lint, fixture_paths=set()),
                )
        except Conflict as error:
            raise Parked(f"replan raced with execution: {error}") from None
        self.notify(f"[run] {', '.join(chosen)} replanned into {[t.id for t in replacements]}")

    def _replan_errors(
        self, run: Run, tasks: list[OrchTask], chosen: list[str], entries: list[Any],
    ) -> list[str]:
        """Every reason a proposed replacement subtree cannot replace the chosen tasks."""
        kept = [t.entry() for t in tasks if t.status != "REPLANNED" and t.id not in chosen]
        plan = {**run.plan, "tasks": [*kept, *entries]}
        errors = lint(plan, fixture_paths=set())  # code split preserves existing frozen tests
        if not errors:
            errors += replacement_frontier(
                {path for old in tasks if old.id in chosen for path in old.files},
                {entry["id"]: entry["depends_on"] for entry in entries},
                {entry["id"]: entry["files"] for entry in entries},
            )
        if not errors and run.environment_root:
            errors = self._pack_errors(plan, run.base, run.spec)
        wanted = {test for t in tasks if t.id in chosen for test in t.tests}
        given = {test for e in entries if isinstance(e, dict) for test in e.get("tests", [])}
        taken = {t.id for t in tasks}
        errors += ["replacement tests differ from the replaced tasks"] if wanted != given else []
        errors += [
            f"id {e.get('id')} reused" for e in entries
            if isinstance(e, dict) and e.get("id") in taken
        ]
        return errors

    def _build(self, run_id: str, task: OrchTask, route: Route, count: int) -> None:
        run = self.state.run(run_id)
        context = RunContext.of(run, self.state.tasks(run_id))
        with self.git_lock:
            base = self.git(self.integration(run.id), "rev-parse", "HEAD")
        if task.pending_candidate:
            count = 1
        labels = [
            f"a{task.attempts}" + (f"s{index + 1}" if count > 1 else "") for index in range(count)
        ]
        with ExitStack() as stack:
            paths = [
                stack.enter_context(self.worktree(run.id, f"{task.id}-{label}", base))
                for label in labels
            ]
            solo = count == 1 and route == DSF  # only single DSF attempts carry A/B trials
            jobs = [
                (path, label, HINTS[index % len(HINTS)] if count > 1 else "", solo)
                for index, (path, label) in enumerate(zip(paths, labels, strict=True))
            ]
            if task.pending_candidate:
                made: list[Candidate | AttemptFailed | Deferred | Signal] = [
                    self._reuse_candidate(run, task, route, base, paths[0])]
            else:
                with ThreadPoolExecutor(max_workers=count) as pool:
                    made = list(pool.map(
                        lambda job: self._generate(run, context, task, route, base, *job), jobs
                    ))
            self._generated(task.id)  # from here on the task only waits for sandbox runs
            candidates = [item for item in made if isinstance(item, Candidate)]
            if not candidates:
                for kind in (Deferred, Signal, AttemptFailed):
                    for item in made:
                        if isinstance(item, kind):
                            raise item
            self.state.transition(run.id, task.id, "RUNNING", "task.candidate", {
                "commit": candidates[0].commit,
            }, attempt=task.attempts, pending_candidate=candidates[0].commit)
            self.state.transition(
                run.id, task.id, "VERIFYING", "task.verifying",
                {"candidates": [c.label for c in candidates]}, attempt=task.attempts,
            )
            with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
                checked = list(pool.map(
                    lambda c: self._check(run, context, task, base, c, route), candidates
                ))
            passing = [c for c, error in zip(candidates, checked, strict=True) if error is None]
            if not passing:
                errors = [error for error in checked if error is not None]
                waiting = next(((c, e) for c, e in zip(candidates, checked, strict=True)
                                if isinstance(e, Waiting)), None)
                if waiting is not None:
                    self.state.transition(run.id, task.id, "VERIFYING", "task.candidate", {
                        "commit": waiting[0].commit,
                    }, attempt=task.attempts, pending_candidate=waiting[0].commit)
                    raise waiting[1]
                direct = next(((c, error) for c, error in zip(candidates, checked, strict=True)
                               if isinstance(error, Signal)), None)
                if direct is not None:
                    self.state.transition(run.id, task.id, "VERIFYING", "task.candidate", {
                        "commit": direct[0].commit,
                    }, attempt=task.attempts, pending_candidate=direct[0].commit)
                    raise direct[1]
                behavioral = [(c, error) for c, error in zip(candidates, checked, strict=True)
                              if isinstance(error, AttemptFailed) and error.category == "test"]
                if route == DSF and len({digest(c.diff) for c, _ in behavioral}) >= 2:
                    common = common_failures([error.detail for _, error in behavioral], task.tests)
                    if common:
                        best, sample_failure = min(behavioral, key=lambda item: (
                            len(failed_tests(item[1].detail)), item[0].label))
                        self.state.transition(run.id, task.id, "VERIFYING", "task.candidate", {
                            "commit": best.commit,
                        }, attempt=task.attempts, pending_candidate=best.commit)
                        self.state.record(run.id, "task.failure_agreement", {
                            "tests": common, "candidates": [c.commit for c, _ in behavioral],
                            "test_version": digest(dumps(context.tests_for(task))),
                        }, task.id, task.attempts)
                        raise Signal("TEST_DISPUTE",
                            "Different implementation candidates share these failures: "
                            + dumps(common) + ". Check fixture callbacks and expected values "
                            "against the original spec before further implementation edits. "
                            "Candidate agreement does not prove the tests wrong.\n"
                            + sample_failure.detail[-6000:], sample_failure)
                raise errors[0]
            self.state.transition(
                run.id, task.id, "MERGING", "task.merging",
                {"passing": [c.label for c in passing]}, attempt=task.attempts,
            )
            failure: AttemptFailed | None = None
            for candidate in passing:  # the rest are alternates if the merge gate rejects one
                self.state.transition(run.id, task.id, "MERGING", "task.candidate", {
                    "commit": candidate.commit,
                }, attempt=task.attempts, pending_candidate=candidate.commit)
                try:
                    self._merge(run, task, candidate, route)
                    return
                except AttemptFailed as error:
                    failure = error
            assert failure is not None
            raise failure

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
                or trailers.get("Orch-Model") not in {DSF.label, self._claude_route(run.id).label}
                or len(parents) != 2):
            raise Parked("Saved candidate no longer matches task contract or route")
        changed = set(self.git(self.repo, "diff", "--name-only", parents[1], commit).splitlines())
        if not changed or changed - set(task.files) or self._project_controls(list(changed)):
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
                self.git(path, "cherry-pick", original)
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
        base: str, path: Path, hint: str = "", extra_rule: str = "", feedback: str = "",
    ) -> Pack:
        # A task that builds on other tasks reads their files up front, next to anything
        # it asked for itself. Without them the integrating task of Linux runs r6f34ed35
        # and r8aadd9d7 guessed the dependencies' legacy ordering from signatures, failed
        # its first attempt (~150 s) and then asked for exactly these files.
        own = list(task.context_selectors)
        chosen = list(dict.fromkeys([*own, *self._dependency_files(context, task, base)]))
        try:
            requested = requested_sources(self.index, path, base, chosen,
                                          any_file=self.generic) if chosen else {}
        except ValueError:
            chosen = own
            requested = {}
        selectors = [item for item in chosen
                     if "::" not in item or item.split("::", 1)[0] not in chosen]
        if chosen == own and own:
            requested = requested_sources(self.index, path, base, selectors, any_file=self.generic)
        index = self.index.at(base)
        originals = self.index.read(base, task.files)
        owned = owned_sources(index, originals, task.tests, selectors)
        partial_owned = {name: source for name, source in owned.items()
                         if source != originals[name]}
        arguments = (
            task, context.requirements, run.repo_map,
            context.interfaces_for(task, index), owned,
            context.tests_for(task, sliced=True),
            (DSF_BUDGET if route == DSF else CLAUDE_BUDGET) - len(self.environment_text.encode()),
            (task.last_diff, feedback or task.last_error), hint, self.rules_text, extra_rule,
            self._dependencies(run.id, task, base),
        )
        contracts = context.dependency_contracts(task, run.base)
        try:
            return replace(context_pack(*arguments, requested=requested, original_spec=run.spec,
                                        language_note=self._language_note(),
                                        dependency_contracts=contracts),
                           partial_owned=partial_owned)
        except ContextTooLarge:
            if not selectors:
                raise
        # Keep requested definitions exact. Only full-file reads become explicit catalogs.
        for size in (8192, 2048, 512, 128):
            compact = requested_sources(self.index, path, base, chosen, catalog_bytes=size,
                                        any_file=self.generic)
            try:
                packed = context_pack(*arguments, requested=compact, original_spec=run.spec,
                                      language_note=self._language_note(),
                                      dependency_contracts=contracts)
                return replace(packed, partial_owned=partial_owned,
                               partial_reads=tuple(s for s in selectors
                                                   if "::" not in s and s not in owned))
            except ContextTooLarge:
                if size == 128:
                    raise
        raise AssertionError("Unreachable context fallback")

    def _dependency_files(self, context: "RunContext", task: OrchTask, base: str) -> list[str]:
        """Source files owned by the tasks this task builds on, as read selectors (at most
        DEPENDENCY_READS); none when there are more, so a broad fan-in stays on demand."""
        paths = sorted({
            path for tid in task.depends_on if tid in context.tasks
            for path in context.tasks[tid].files
            if (self.generic or (path.endswith(".py") and not LiveWorkspace.is_test_file(path)))
            and path not in task.files
        })
        present = (set(self.index.at(base).files) if not self.generic
                   else set(self.index.tree(base)))
        paths = [path for path in paths if path in present]
        return paths if len(paths) <= DEPENDENCY_READS else []

    def _generate(
        self, run: Run, context: "RunContext", task: OrchTask, route: Route, base: str,
        path: Path, label: str, hint: str, solo: bool,
    ) -> "Candidate | AttemptFailed | Deferred | Signal":
        try:
            trial = self._trial(run.id, task) if solo else None
            shown = trial is not None and trial[1]
            texts = {rule.id: rule.text for rule in self.state.rules()} if shown else {}
            context_feedback = ""
            repairs = 0
            call_route = route
            while True:
                if self.cancel.is_set() or self.stop_reason:
                    raise Deferred(self.stop_reason or "interrupted during context retrieval")
                if self.max_cents and self.spent >= self.max_cents:
                    raise Deferred("run spend cap reached during context retrieval")
                current = self.state.task(run.id, task.id)
                try:
                    pack = self._builder_pack(
                        run, context, current, route, base, path, hint,
                        texts[trial[0]] if trial is not None and shown else "", context_feedback,
                    )
                except ContextTooLarge as error:
                    raise AttemptFailed("context", str(error)) from None
                except ValueError as error:
                    raise Parked(str(error)) from None
                rules = [*self.rule_ids, *([trial[0]] if trial is not None and shown else [])]
                prompt = ContextPrompt(pack.shared, pack.task + (
                    (pack.retry or "\nImplement the task.\n") if route.provider == "claude"
                    else pack.retry))
                prefix = digest(self.environment_text + prompt.shared)[:12]
                self.state.record(
                    run.id, "attempt.called",
                    {"route": route.label, "prefix": prefix, "level": task.level,
                     "sample": label,
                     "bytes": len(prompt.encode()) + len(self.environment_text.encode()),
                     "rules": rules,
                     "context_requests": current.context_requests,
                     "partial_reads": list(pack.partial_reads),
                     "partial_owned": sorted(pack.partial_owned),
                     "dependency_contracts": {
                         tid: cast(dict[str, object], contract)["version"]
                         for tid, contract in context.dependency_contracts(task, run.base).items()},
                     "trial": [trial[0], "with" if shown else "without"] if trial else []},
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
                    prefix=prefix, rules=rules, base=base,
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
                if status != "NEED_FILES":
                    if result.data.get("need_files"):
                        raise AttemptFailed(
                            "context", "need_files requires NEED_FILES without edits",
                        )
                    if status != "DONE":
                        break
                    try:  # validates every edit and compiles before writing anything
                        apply_edits(path, task.files, result.data.get("edits"),
                                    visible=pack.partial_owned)
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
                    reads = result.data.get("need_files")
                    if (result.data.get("edits") != [] or not isinstance(reads, list)
                            or not 1 <= len(reads) <= 8):
                        raise ValueError("NEED_FILES requires no edits and 1-8 selectors")
                    requested_sources(self.index, path, base, reads, any_file=self.generic)
                    selectors = [str(item) for item in reads]
                except ValueError as error:
                    context_feedback = str(error)
                whole = {name for name in pack.requested if "::" not in name}
                if (selectors and not pack.partial_reads and all(
                        item in pack.requested or item.split("::", 1)[0] in whole
                        for item in selectors)):
                    # Everything asked for is already in this prompt (run r00a92536 parked
                    # T-004 on such a repeat). Say so once within the attempt; a second
                    # repeat fails the attempt and the ladder continues.
                    if not repairable:
                        # "edit", not "context": nothing is oversized, the answer was unusable.
                        raise AttemptFailed("edit", (
                            "NEED_FILES repeated context already supplied: " + dumps(selectors)))
                    repairs += 1
                    context_feedback = (
                        "Every file you asked for is already included above under REQUESTED "
                        f"READ-ONLY CONTEXT: {dumps(selectors)}. Do not request it again; "
                        "return DONE with your edits, or BLOCKED/TEST_DISPUTE with a reason.")
                    self.state.record(run.id, "attempt.format_repair", {
                        "call_id": call_id, "kind": "context", "repairs": repairs,
                    }, task.id, task.attempts)
                    continue
                try:
                    self.state.request_context(
                        run.id, task.id, selectors, base, context_feedback, call_id=call_id,
                        observed_requests=current.context_requests,
                        partial_files=pack.partial_reads,
                    )
                except Conflict as error:
                    raise Parked(str(error)) from None
            if status in {"BLOCKED", "TEST_DISPUTE"}:
                raise Signal(str(status), str(result.data.get("reason", "")))
            if status != "DONE":
                raise AttemptFailed("edit", "Expected DONE, NEED_FILES, BLOCKED or TEST_DISPUTE")
            # The sandbox's scratch tree and linked dependencies are never part of a change.
            self.git(path, "add", "-A", "--", ".",
                     *(orch_verify.excludes(path) if self.generic else ()))
            present = [owned for owned in task.files if (path / owned).exists()]
            if present:  # owned files may sit under a .gitignore pattern
                self.git(path, "add", "-f", "--", *present)
            diff = self.git(path, "diff", "--cached", base)
            if not diff:
                raise AttemptFailed("edit", "edits changed nothing")
            message = commit_message(
                run.id, run.plan_hash, task, label, route.label,
                rules=", ".join(rules) or "none",
                inputs=(digest(dumps(context.dependency_contracts(task, run.base)))
                        if task.interface_depends_on else ""),
            )
            self.git(path, "commit", "-m", message, author=f"orch {route.label}")
            commit = self.git(path, "rev-parse", "HEAD")
            # Every attempt stays addressable as evidence after its worktree is removed.
            with self.git_lock:
                self.git(self.repo, "update-ref", f"refs/orch/{run.id}/{task.id}/{label}", commit)
            return Candidate(commit, diff, label, path, lesson(result.data.get("lesson")))
        except (AttemptFailed, Deferred, Signal) as outcome:
            return outcome

    def _sidecar(self, run_id: str, call_id: str, payload: dict[str, object]) -> None:
        """Keep a settled answer that could not be used, for audit and exact offline replay."""
        folder = self.run_dir(run_id) / "responses"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{call_id}.json").write_text(dumps(payload))

    def _dependencies(self, run_id: str, task: OrchTask, base: str) -> dict[str, str]:
        """Merged code this task's files and frozen tests call, along the call graph."""
        try:
            index = self.index.at(base)
            code = dependency_code(
                index, lambda paths: self.index.read(base, paths),
                index.roots(task.files, task.tests), set(task.files), DEPENDENCY_BYTES,
            )
            if not task.interface_depends_on:
                return code
            tasks, owners = self._owners(run_id)
            available = {
                tid for tid in {owners[name.split("::", 1)[0]] for name in code
                                if name.split("::", 1)[0] in owners}
                if tasks[tid].status == "DONE" and tasks[tid].commit
                and self.git(self.repo, "merge-base", tasks[tid].commit, base) == tasks[tid].commit
            }
            return {name: source for name, source in code.items()
                    if owners.get(name.split("::", 1)[0]) in {None, *available}}
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            # The index only adds context; an attempt never fails for want of it.
            self.state.record(run_id, "index.error", {"error": str(error)[:200]}, task.id)
            return {}

    def _owners(self, run_id: str) -> tuple[dict[str, OrchTask], dict[str, str]]:
        tasks = {t.id: t for t in self.state.tasks(run_id) if t.status != "REPLANNED"}
        return tasks, {path: t.id for t in tasks.values() for path in t.files}

    def _imports(self, run_id: str, task: OrchTask, commit: str, diff: str
                 ) -> AttemptFailed | None:
        """Imports the sandbox cannot satisfy fail the attempt. Imports of a task that is
        absent from this candidate make it wait/refresh (the dependency is added),
        unless that task already depends on this one."""
        index = self.index.at(commit)
        tasks, owner = self._owners(run_id)
        graph = {t.id: t.depends_on for t in tasks.values()}
        needed = closure(graph, task.id)
        problems = [
            f"{path} imports {name}, which is absent from the repository and pinned environment"
            for path in task.files for name in index.missing_imports(
                path, self.environment.modules if self.environment else ())
        ]
        imported = {
            other for path in task.files for target in index.deps.get(path, ())
            if (other := owner.get(target, task.id)) != task.id and other not in needed
        }
        # DONE is live state; the candidate may have branched before that dependency
        # merged. Refresh its snapshot instead of misclassifying a stale stub as a
        # model failure and climbing the repair ladder.
        unbuilt = sorted(
            other for other in imported
            if tasks[other].status != "DONE" or not tasks[other].commit
            or self.git(self.repo, "merge-base", tasks[other].commit, commit) != tasks[other].commit
        )
        problems += [
            f"imports code of {other}, which depends on this task; answer BLOCKED to re-plan"
            for other in unbuilt if task.id in closure(graph, other)
        ]
        if problems:
            return AttemptFailed("import", "\n".join(problems), diff)
        if unbuilt:
            return Waiting(
                unbuilt, f"imports code of {unbuilt}, absent from candidate revision", diff)
        return None

    def _implicit(self, run_id: str, task: OrchTask, commit: str) -> list[str]:
        """Merged tasks this task's code imports without depending on them."""
        index = self.index.at(commit)
        tasks, owner = self._owners(run_id)
        needed = closure({t.id: t.depends_on for t in tasks.values()}, task.id)
        return sorted({
            owner[target] for path in task.files for target in index.deps.get(path, ())
            if owner.get(target, task.id) not in {task.id, *needed}
        })

    def _check(
        self, run: Run, context: "RunContext", task: OrchTask, base: str,
        candidate: "Candidate", route: Route,
    ) -> "AttemptFailed | Signal | None":
        path, commit, diff = candidate.path, candidate.commit, candidate.diff
        changed = set(self.git(path, "diff", "--name-only", base, commit).splitlines())
        if self._project_controls([*task.files, *changed]):
            return AttemptFailed("scope", "Project pytest collection control is frozen", diff)
        outside = sorted(changed - set(task.files))
        if outside:
            return AttemptFailed("scope", f"changed files outside ownership: {outside}", diff)
        current = self.index.read(commit, task.files, suffix="" if self.generic else ".py")
        missing = [owned for owned in task.files if owned in context.stubs and owned not in current]
        if missing:
            return AttemptFailed("interface", f"missing implementation files: {missing}", diff)
        problems = [] if self.generic else [
            problem
            for owned in task.files
            if owned in context.stubs
            for problem in interface_errors(context.stubs[owned], current[owned])
        ]
        if problems:
            return AttemptFailed("interface", "\n".join(problems), diff)
        original = self.index.read(base, task.files)
        problems = [] if self.generic else [
            f"{owned}: {problem}"
            for owned in current
            for problem in implementation_errors(original.get(owned, ""),
                                                current[owned],
                                                context.stubs.get(owned, ""))
        ]
        if problems:
            return AttemptFailed("structure", "\n".join(problems), diff)
        if task.interface_depends_on:
            tasks, _ = self._owners(run.id)
            waiting = [tid for tid in task.depends_on
                       if tasks[tid].status != "DONE" or not tasks[tid].commit
                       or self.git(self.repo, "merge-base", tasks[tid].commit, commit)
                       != tasks[tid].commit]
            if waiting:
                return Waiting(waiting, "await dependency implementations in snapshot", diff)
        imported = None if self.generic else self._imports(run.id, task, commit, diff)
        if imported is not None:
            return imported
        if self.git(path, "status", "--porcelain"):
            return AttemptFailed("scope", "worktree is not clean after commit", diff)
        passed, report = self.verify(path, self.scratch(run.id), commit, task.tests)
        if not passed:
            category, evidence = (
                diagnose_failure(report, set(context.tests_for(task)), current)
                if not self.generic else ("test", report[-4000:]))
            if category == "fixture":
                self.state.record(run.id, "task.fixture_dispute", {
                    "candidate": commit, "evidence": evidence,
                }, task.id, task.attempts)
                return Signal("TEST_DISPUTE", "Check this frozen test fixture before changing "
                              "the implementation:\n" + evidence,
                              AttemptFailed("fixture", evidence, diff))
            return AttemptFailed(category, evidence, diff)
        return self._screen(run.id, task, candidate) if route == DSF else None

    def _screen(self, run_id: str, task: OrchTask, candidate: "Candidate") -> AttemptFailed | None:
        """Jev pre-screen of a passing DSF diff for test special-casing."""
        answers = self._jev(
            run_id, task.id, task.attempts, "screen",
            {"contract": task.contract[:4000], "diff": candidate.diff[-12000:]}, CHEAT_QUESTION,
        )
        probability = cheat_probability(answers.get("cheat")) if answers else None
        if probability is None:
            return None
        flagged = probability >= CHEAT_FLAG
        applied = flagged and self.jev_mode == "on"
        self.state.record(
            run_id, "jev.screen",
            {"candidate": candidate.label, "probability": probability, "flagged": flagged,
             "applied": applied},
            task.id, task.attempts,
        )
        if not applied:
            return None
        return AttemptFailed(
            "suspect", f"Jev: diff likely special-cases the tests (p={probability:.2f})",
            candidate.diff,
        )

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
            merged_tests = [
                test
                for other in self.state.tasks(run.id)
                if other.status == "DONE"
                for test in other.tests
            ]
            try:
                with self.git_lock:
                    self.git(integration, "cherry-pick", commit)
            except RuntimeError as error:
                with self.git_lock:
                    self.git(integration, "cherry-pick", "--abort")
                self._congest()
                if task.rebases + 1 > MAX_REBASES:
                    raise Parked(f"merge conflict after {MAX_REBASES} rebases: {error}") from None
                self.state.transition(
                    run.id, task.id, "READY", "task.rebase", {"error": str(error)[:300]},
                    attempt=task.attempts, rebases=task.rebases + 1,
                )
                return
            merged = self.git(integration, "rev-parse", "HEAD")
            changed = self.git(integration, "diff", "--name-only", before, merged).splitlines()
            affected = self.index.at(merged).affected(merged_tests, changed)
            self.state.record(
                run.id, "task.gate",
                {"tests": len(affected) + len(task.tests), "skipped": len(merged_tests)
                 - len(affected)},
                task.id, task.attempts,
            )
            with self.worktree(run.id, "gate", merged) as gate:
                passed, report = self.verify(
                    gate, self.scratch(run.id), merged, [*affected, *task.tests]
                )
            if not passed:
                raise AttemptFailed("integration", report, diff)
            self.state.record(
                run.id, "attempt.outcome",
                {"route": route.label, "level": task.level, "passed": True},
                task.id, task.attempts,
            )
            implicit = self._implicit(run.id, task, merged)
            self.state.transition(
                run.id, task.id, "DONE", "task.done",
                {"commit": merged, "candidate": candidate.label, "implicit": implicit},
                attempt=task.attempts, verified_head=merged, commit=merged, reason="",
                pending_candidate="", failed_candidate="",
                depends_on=[*task.depends_on, *implicit],
            )
        self._relax()
        self._observe(run.id, task.id, task.attempts, True)
        if candidate.lesson and self._run_strategy(run.id) != "sonnet-dsf":
            rule = self.state.add_rule(
                candidate.lesson, f"{run.id}/{task.id}/{candidate.label} {task.last_signature}"
            )
            if rule is not None:
                self.state.record(run.id, "rule.candidate", {"rule": rule.id}, task.id)
                self.notify(f"[rules] new candidate {rule.id}: {rule.text}")
        self.notify(f"[run] {task.id} DONE at {merged[:10]} ({candidate.label})")

    def _failed(
        self, run_id: str, task_id: str, number: int, level: int, failure: AttemptFailed
    ) -> None:
        task = self.state.task(run_id, task_id)
        mark = signature(failure.category, failure.detail)
        diff_mark = digest(failure.diff)[:12] if failure.diff else ""
        same_diff = bool(diff_mark) and diff_mark == task.last_diff_hash
        repeated = mark == task.last_signature or same_diff
        used = sorted({*task.used_levels, level})
        if failure.category == "context":
            # A local payload bound is not a model weakness. Re-split once; do not spend
            # retries, samples or Opus patches on the identical oversized working set.
            upcoming = 5 if 5 not in used else PARK
        elif failure.category == "output_limit":
            # The widest admissible output budget was already cut by the provider: any rung
            # would resend this unchanged task with a lower or equal budget. Stop durably.
            upcoming = PARK
        else:
            eligible = failure.sampleable
            deterministic = next_level(used, repeated, self._pass_rate(), sampleable=eligible)
            upcoming = self._triage(
                run_id, task, number, used,
                deterministic,
                failure,
            )
            allowed = {level for level in triage_options(used, self._pass_rate())
                       if level != 2 or eligible} | {deterministic}
            if upcoming not in allowed:
                self.state.record(run_id, "recovery.rejected", {
                    "proposed": upcoming, "chosen": deterministic,
                    "category": failure.category, "reason": "used or ineligible recovery action",
                }, task_id, number)
                upcoming = deterministic
        route = self._claude_route(run_id) if level == 3 else DSF
        if failure.category == "context":
            with self.trial_lock:
                self.trials.pop((run_id, task_id, number), None)
        else:
            self._observe(run_id, task_id, number, False)
            self.state.record(
                run_id, "attempt.outcome", {"route": route.label, "level": level, "passed": False},
                task_id, number,
            )
        evidence: dict[str, Any] = {
            "pending_candidate": "",
            "failed_candidate": (task.pending_candidate if failure.sampleable
                                 and task.pending_candidate else task.failed_candidate),
            "used_levels": used,
            "claude_attempts": task.claude_attempts + int(level == 3),
            "last_signature": mark,
            "last_diff_hash": diff_mark,
            "last_error": failure.detail[-3000:],
            "last_diff": failure.diff[-4000:],
        }
        if task.pending_review:
            saved = self.reviews.pending(run_id, task_id)
            if (failure.category == "dispute"
                    or saved is not None and self.reviews.applied(saved) is not None):
                # A deterministic proposal rejection is finished too; only provider
                # trouble keeps this operation pending for another bounded dispatch.
                evidence["pending_review"] = ""
        data = {"category": failure.category, "signature": mark, "level": level,
                "sampleable": failure.sampleable, "candidate": task.pending_candidate}
        if upcoming == PARK:
            reason = (f"output_limit: {failure.detail[:400]}"
                      if failure.category == "output_limit" else
                      f"ladder exhausted at level {level} ({failure.category}, {mark})")
            self.state.transition(
                run_id, task_id, "PARKED", "task.parked", {**data, "reason": reason},
                attempt=number, reason=reason, **evidence,
            )
            self.notify(f"[run] {task_id} PARKED: {reason}")
            return
        self.state.transition(
            run_id, task_id, "READY", "task.failed", {**data, "next": upcoming},
            attempt=number, level=upcoming, **evidence,
        )
        self.notify(
            f"[run] {task_id} a{number} failed ({failure.category}); next rung {upcoming}"
        )


def mutant_limit(baseline: float) -> float:
    """Per-mutant deadline from the unmutated run: a mutant that loops forever must not
    cost a whole sandbox deadline."""
    return min(25.0, max(3.0, 4 * baseline + 1))


PROJECT_TEST_SHARDS = 4
PROJECT_SHARD_MIN_FILES = 24  # below this a suite runs as one process
# pytest's default norecursedirs: explicit file arguments bypass them, so skip them here.
_NORECURSE = re.compile(r"(^|/)(\.[^/]*|_darcs|build|CVS|dist|node_modules|venv|[^/]*\.egg|"
                        r"\{arch\})/")


def project_test_shards(tree_listing: str, shards: int) -> list[list[str]]:
    """Size-balanced groups of the files pytest collects by default (`git ls-tree -r -l`).

    Returns [] when the suite is too small to be worth splitting or `shards` < 2.
    """
    files: list[tuple[int, str]] = []
    for line in tree_listing.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) != 4 or parts[1] != "blob" or not parts[3].isdigit():
            continue
        name = PurePosixPath(path).name
        if ((name.startswith("test_") or name.endswith("_test.py")) and name.endswith(".py")
                and not _NORECURSE.search(path) and LiveWorkspace.safe_path(path)):
            files.append((int(parts[3]), path))
    if shards < 2 or len(files) < PROJECT_SHARD_MIN_FILES:
        return []
    groups: list[list[str]] = [[] for _ in range(shards)]
    sizes = [0] * shards
    for size, path in sorted(files, reverse=True):  # largest first, into the lightest group
        index = sizes.index(min(sizes))
        groups[index].append(path)
        sizes[index] += size
    return [sorted(group) for group in groups if group]


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
    rules: str
    trial: str
    rule_ids: tuple[str, ...] = ()


@dataclass
class Draft:
    """One plan sample: the plan (None if it never passed lint) and its proxy score."""

    plan: dict[str, object] | None
    repairs: int
    errors: list[list[str]]  # lint errors of each rejected attempt
    vacuous: list[str] = field(default_factory=list)
    splits: list[str] = field(default_factory=list)
    probe_note: str = ""
    score: float = 0.0
    parts: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    commit: str
    diff: str
    label: str
    path: Path
    lesson: str = ""


def test_modules(test: str) -> list[str]:
    """Module keys of a test id: a dotted Python `pkg.mod.Class.method`, or a declared
    check's test files."""
    check = orch_verify.parse(test)
    return list(check.files) if check is not None else [test.rsplit(".", 2)[0]]


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

        def is_test(path: str) -> bool:
            return path in checked or LiveWorkspace.is_test_file(path)

        return cls(
            files=files,
            stubs={p: c for p, c in files.items() if not is_test(p)
                   and (p.endswith(".py") or plan.get("engine") == "generic")},
            # Python tests are addressed by dotted module ids; declared checks by their files.
            modules={(python_module(p) if p.endswith(".py") and p not in checked else p): p
                     for p in files if is_test(p)},
            requirements={
                str(item["id"]): str(item["text"])
                for item in cast(list[dict[str, str]], plan["requirements"])
            },
            tasks={task.id: task for task in tasks_from(plan)},
        )

    def interfaces_for(self, task: OrchTask, index: RepoIndex) -> dict[str, str]:
        """Frozen contracts for this task, declared dependencies and imported helpers."""
        graph = {tid: other.depends_on for tid, other in self.tasks.items()}
        graph[task.id] = task.depends_on  # includes dependencies discovered during execution
        paths = set(task.files)
        for tid in closure(graph, task.id):
            if tid in self.tasks:
                paths.update(self.tasks[tid].files)
        for path in tuple(paths):
            paths.update(index.reach(path))
        symbols: dict[str, set[str]] = {}
        for path, symbol, _ in index.referenced(index.roots(task.files, task.tests)):
            symbols.setdefault(path, set()).add(symbol.name)
        return {path: (self.stubs[path] if path in task.files else
                       excerpt(path, self.stubs[path], symbols.get(path, set())))
                for path in sorted(paths) if path in self.stubs}

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

    def tests_for(self, task: OrchTask, *, sliced: bool = False) -> dict[str, str]:
        """Frozen test files that hold this task's tests."""
        paths = {
            self.modules[module]
            for test in task.tests
            for module in test_modules(test)
            if module in self.modules
        }
        if not sliced:
            return {path: self.files[path] for path in sorted(paths)}
        return {path: (self.files[path] if not path.endswith(".py") else excerpt(
            path, self.files[path], {
                ".".join(test.rsplit(".", 2)[1:]) for test in task.tests
                if path in {self.modules.get(m) for m in test_modules(test)}
            })) for path in sorted(paths)}
