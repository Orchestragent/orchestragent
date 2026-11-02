"""`orch`: plan, run, resume, status, trace, why and cost for one local repository.

Nothing here pushes, switches billing or reads credentials: model calls go through
ProviderClients (Claude subscription, DSH under its explicit CNY budget), and every
result lands on a local `orch/<run>` branch for the user to merge.
"""

import argparse
import getpass
import locale
import math
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

from orchestragent.access import Access
from orchestragent.live_workspace import LiveWorkspace
from orchestragent.native import NativeResult
from orchestragent.orch_audit import audit
from orchestragent.orch_baseline import baseline
from orchestragent.orch_bench import Case, mock_caller, read_report, report_lines, run_suite
from orchestragent.orch_build import parse_trailers
from orchestragent.orch_environment import PytestEnvironment
from orchestragent.orch_index import Index, Symbol, backend
from orchestragent.orch_ladder import RUNGS
from orchestragent.orch_plan import REQ_TEST, plan_files, unittest_ids
from orchestragent.orch_planning import PlanningStore
from orchestragent.orch_policy import PolicyStore, Tuning
from orchestragent.orch_progress import lines as progress_lines
from orchestragent.orch_progress import snapshot as progress_snapshot
from orchestragent.orch_rules import PROXY_RUNS
from orchestragent.orch_run import (
    CLAUDE_PLAN,
    DSF,
    JEV_MODES,
    SONNET_PLAN,
    Caller,
    Orchestrator,
    PlanError,
    RunContext,
    Runner,
    orch_home,
)
from orchestragent.orch_state import IN_FLIGHT, OrchState, OrchTask, Run
from orchestragent.orch_usage import lines as usage_lines
from orchestragent.orch_usage import report as usage_report
from orchestragent.orch_usage import unresolved
from orchestragent.orch_watch import Json, Source
from orchestragent.orch_watch import export as watch_export
from orchestragent.orch_watch_server import Settings as WatchSettings
from orchestragent.orch_watch_server import is_loopback, new_token, serve
from orchestragent.orch_watch_tui import choose, plain, tui
from orchestragent.platform import default_data_dir, sandbox_name
from orchestragent.providers import ProviderClients, dsf_transport
from orchestragent.serialization import dumps
from orchestragent.store import Conflict
from orchestragent.workspace import Workspace

ROOT = Path(__file__).resolve().parents[2]
EXIT = {"done": 0, "parked": 3, "paused": 4}
PLAN_REJECTED = 5


def state_of(repo: Path) -> OrchState:
    return OrchState(orch_home(repo.resolve()) / "state.sqlite")


def pick(state: OrchState, run_id: str) -> Run:
    return state.run(run_id) if run_id else state.latest()


SUMMARY_AT = 30  # wider runs show aggregates instead of one line per task


def summary_lines(state: OrchState, run: Run, tasks: list[OrchTask]) -> list[str]:
    """Aggregates for wide runs: what a person watching 100+ tasks needs."""
    events = state.events(run.id)
    counts: dict[str, int] = {}
    rungs: dict[int, int] = {}
    for task in tasks:
        counts[task.status] = counts.get(task.status, 0) + 1
        rungs[task.level] = rungs.get(task.level, 0) + 1
    order = ("DONE", "MERGING", "VERIFYING", "RUNNING", "READY", "PLANNED", "PARKED",
             "REPLANNED")
    started = [e.at for e in events if e.kind == "task.started"]
    merged = [e.at for e in events if e.kind == "task.done"]
    hours = (max(merged) - min(started)) / 3600 if merged and started else 0
    gates = [e.data for e in events if e.kind == "task.gate"]
    scores = [task.mutation for task in tasks if task.mutation >= 0]
    spent = state.spent(run.id)
    attempts = sum(task.attempts for task in tasks)
    flight = [t for t in tasks if t.status in {"RUNNING", "VERIFYING", "MERGING"}]
    lines = [
        f"  {len(tasks)} task(s): "
        + ", ".join(f"{name} {counts[name]}" for name in order if counts.get(name)),
        f"  rungs {' '.join(f'{lvl}:{n}' for lvl, n in sorted(rungs.items()))}   attempts "
        f"{attempts} ({attempts / max(1, len(tasks)):.1f}/task)"
        + (f"   mutation {sum(scores) / len(scores):.2f} (avg of {len(scores)})" if scores
           else ""),
        f"  merged {len(merged)}" + (f" ({len(merged) / hours:.0f}/h)" if hours > 0 else "")
        + f"   spend ¥{spent / 100:.2f}   gate tests run "
        f"{sum(cast(int, g['tests']) for g in gates)}, skipped by impact analysis "
        f"{sum(cast(int, g['skipped']) for g in gates)}",
    ]
    if flight:
        lines.append("  in flight: " + ", ".join(f"{t.id} {t.status} a{t.attempts}"
                                                 for t in flight[:12]))
    lines.append(f"  (`orch status --all {run.id}` lists every task)")
    return lines


def status_lines(state: OrchState, run: Run, full: bool = True) -> list[str]:
    tasks = state.tasks(run.id)
    parked = {task.id for task in tasks if task.status == "PARKED"}
    lines = [
        f"run {run.id}  {run.status}  branch {run.branch}  plan {run.plan_hash}",
        *([f"  {run.message}"] if run.message else []),
        *([f"  policy {run.policy_id}"] if run.policy_id else []),
        *([f"  pytest environment {run.environment_fingerprint}"]
          if run.environment_fingerprint else []),
    ]
    if not full and len(tasks) > SUMMARY_AT:
        lines += summary_lines(state, run, tasks)
        tasks = [task for task in tasks if task.status == "PARKED"]
    for task in tasks:
        blocked = sorted(set(task.depends_on) & parked) if task.status == "PLANNED" else []
        note = (
            task.reason if task.status == "PARKED"
            else f"blocked by {', '.join(blocked)}" if blocked
            else task.commit[:10]
        )
        score = f" mut={task.mutation:.2f}" if task.mutation >= 0 else ""
        lines.append(
            f"  {task.id}  {task.status:<9} attempts={task.attempts} rung={task.level}{score}  "
            f"{','.join(task.reqs):<8} {task.goal[:48]}  {note}".rstrip()
        )
    for task in tasks:
        if task.status == "PARKED":
            lines += [f"PARKED {task.id}: {task.reason}", f"  signature {task.last_signature}"]
            lines += [f"  | {line}" for line in task.last_error.strip().splitlines()[-6:]]
    return lines


def why_lines(state: OrchState, run: Run, req: str) -> list[str]:
    texts = {
        str(item["id"]): str(item["text"])
        for item in cast(list[dict[str, str]], run.plan["requirements"])
    }
    if req not in texts:
        raise KeyError(f"{req} is not a requirement of run {run.id}")
    marker = req[1:]
    tests = [
        test
        for path, source in sorted(plan_files(run.plan).items())
        if path.endswith(".py")
        for test in unittest_ids(path, source)
        if (match := REQ_TEST.fullmatch(test.rsplit(".", 1)[1])) and match.group(1) == marker
    ]
    lines = [f"{req}: {texts[req]}", *[f"  test {test}" for test in tests]]
    for task in state.tasks(run.id):
        if req in task.reqs:
            lines.append(f"  task {task.id} {task.status} {task.commit[:10]}  {task.goal[:60]}")
    return lines


def cost_lines(state: OrchState, run: Run, by: str) -> list[str]:
    return usage_lines(state.usage_calls(run.id), by)


def locate(index: Index, revision: str, target: str) -> tuple[str, int, Symbol | None]:
    """`file:line` as given, or a symbol's definition from the code index.

    For a symbol the line is the first body statement, not the `def` line: signatures
    come from the plan's stubs, while the body is what a task wrote.
    """
    found = re.fullmatch(r"(.+):([1-9][0-9]*)", target)
    if found:
        return found.group(1), int(found.group(2)), None
    hits = index.at(revision).find(target)
    if not hits:
        raise KeyError(f"{target} is neither file:line nor a defined symbol")
    path, symbol = hits[0]
    return path, symbol.body, symbol


def trace_lines(repo: Path, state: OrchState, run: Run, target: str) -> list[str]:
    index = Index(repo, orch_home(repo) / "index.sqlite")
    path, line, symbol = locate(index, run.branch, target)

    def blame(first: int, last: int) -> list[str]:
        # -w -M -C: reformatting and moved or copied lines keep their original commit.
        porcelain = Workspace.git(
            repo, "blame", "-w", "-M", "-C", "--line-porcelain", "-L", f"{first},{last}",
            run.branch, "--", path,
        )
        return [row.split(" ", 1)[0] for row in porcelain.splitlines()
                if re.fullmatch(r"[0-9a-f]{40} \d+ \d+( \d+)?", row)]

    commit = blame(line, line)[0]
    trailers = parse_trailers(Workspace.git(repo, "show", "-s", "--format=%B", commit))
    lines = [f"{path}:{line} <- commit {commit[:10]}"]
    lines += [f"  {key}: {value}" for key, value in trailers.items()]
    task_id = trailers.get("Orch-Task", "")
    if task_id:
        task: OrchTask = state.task(trailers.get("Orch-Run", run.id), task_id)
        lines.append(f"  goal: {task.goal}")
    else:
        lines.append("  (not an orch task commit)")
    if symbol is not None:
        # AST-span blame: every commit that wrote a line of this definition.
        counts: dict[str, int] = {}
        for sha in blame(symbol.line, symbol.end):
            counts[sha] = counts.get(sha, 0) + 1
        lines.append(f"  {symbol.kind} {symbol.name} lines {symbol.line}-{symbol.end}:")
        for sha, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            other = parse_trailers(Workspace.git(repo, "show", "-s", "--format=%B", sha))
            label = other.get("Orch-Task", "") or "(not an orch task commit)"
            lines.append(f"    {sha[:10]} {count:>3} line(s)  {label}")
    return lines


def index_lines(repo: Path, revision: str, query: str) -> list[str]:
    """Index statistics for a revision, or a symbol's definitions and users."""
    index = Index(repo, orch_home(repo) / "index.sqlite")
    built = index.at(revision)
    if query:
        hits = built.find(query)
        if not hits:
            raise KeyError(f"no symbol named {query} at {revision}")
        lines = []
        for path, symbol in hits:
            lines.append(f"{path}:{symbol.line}  {symbol.kind} {symbol.signature}")
            lines += [f"  used by {other}::{owner or '<module>'}"
                      for other, owner in built.users_of(path, symbol.name)]
        return lines
    languages: dict[str, list[int]] = {}
    for f in built.files.values():
        row = languages.setdefault(f"{f.language} ({f.backend})", [0, 0])
        row[0] += 1
        row[1] += len(f.symbols)
    rank = built.rank()
    lines = [f"index {backend()} at {revision}"]
    lines += [f"  {name:<28} {files:>5} files {symbols:>6} symbols"
              for name, (files, symbols) in sorted(languages.items())]
    top = [path for path in sorted(rank, key=lambda p: (-rank[p], p))[:5] if rank[path]]
    lines += [f"  most used: {', '.join(f'{p} ({rank[p]})' for p in top)}"] if top else []
    lines += [f"  snapshot {rev[:10]} {label} ({count} files)"
              for rev, label, count in index.snapshots()]
    return [line for line in lines if line.strip()]


CANARY = (
    "import unittest\n\n\nclass CanaryTest(unittest.TestCase):\n"
    "    def test_ok(self) -> None:\n        self.assertEqual(1 + 1, 2)\n"
)


def doctor_lines(
    repo: Path,
    providers: dict[str, object],
    budget: dict[str, object],
    runner: Runner,
    scratch: Path,
    *, allow_unverified_auth: bool = False,
) -> tuple[bool, list[str]]:
    """Every prerequisite of a real run, each with the fix when it is missing."""
    checks: list[tuple[bool, str]] = [
        (sys.version_info >= (3, 12), f"python {sys.version.split()[0]} (3.12+ required)")
    ]
    try:
        head = Workspace.git(repo, "rev-parse", "--short", "HEAD")
        checks.append((True, f"repo {repo} at {head}"))
    except (OSError, subprocess.SubprocessError):
        checks.append((False, f"repo {repo}: not a git repository with a commit (git init)"))
    canary = scratch / "canary"
    canary.mkdir(parents=True, exist_ok=True)
    (canary / "test_canary.py").write_text(CANARY)
    try:
        passed, report = runner(canary, scratch, "doctor", ["test_canary.CanaryTest.test_ok"])
        checks.append((passed, f"sandbox {sandbox_name()}: " + (
            "canary test passed" if passed else report.strip().splitlines()[-1][:120]
        )))
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        checks.append((False, f"sandbox {sandbox_name()}: {error}"))
    checks.append((True, f"index {backend()}"))  # tree-sitter is optional; ast always works
    claude = cast(dict[str, object], providers.get("claude", {}))
    checks.append((
        claude.get("ready") is True,
        f"claude subscription login: {claude.get('auth', 'unknown')}"
        + ("" if claude.get("ready") is True
           else " -> claude auth login (Mac: scripts/login.command, choose Claude)"),
    ))
    dsh = cast(dict[str, object], providers.get("dsh", {}))
    spent = cast(int, budget.get("spent_cents", 0))
    jev = cast(dict[str, object], providers.get("jev", {}))
    optional = (
        "  ok  jev (typesafe-ai/jev) ready for triage and pre-screen"
        if jev.get("ready") is True
        else "  opt jev (typesafe-ai/jev) not configured; deterministic triage is used "
        "-> orch keys jev (Mac: scripts/login.command, choose Jev)"
    )
    transport = str(dsh.get("transport", "cli"))
    authentication = cast(dict[str, object], dsh.get("authentication", {}))
    if dsh.get("credential_ready") is True:
        verified = dsh.get("verified") is True
        first_call = (allow_unverified_auth and dsh.get("configured") is True
                      and authentication.get("state") == "unverified")
        checks.append((verified or first_call, f"dsh ({DSF.model}/{transport}) authentication " + (
            "verified by actual inference" if verified else
            "unverified; local setup permits explicitly authorized first inference"
            if first_call else
            "rejected -> update the key locally with orch keys dsh"
            if authentication.get("state") == "auth_required" else
            "unverified: local setup only; an explicitly authorized first inference is allowed"
        )))
    checks += [
        (
            True,  # informational: both transports send the same pinned request
            f"dsh transport {transport}: " + (
                "direct DeepSeek API with your key (no DSH CLI installed)" if transport == "api"
                else "official DSH CLI behind the request guard"
            ),
        ),
        (
            dsh.get("credential_ready") is True,
            f"dsh ({DSF.model}) API key "
            + ("loaded" if dsh.get("credential_ready") is True
               else "missing -> orch keys dsh (Mac: scripts/login.command, choose DSH)"),
        ),
        (
            True,  # informational: no local cap; only the provider's own balance can stop calls
            f"dsh spent ¥{spent / 100:.2f} on this machine; no local cap "
            "(an empty DeepSeek balance stops dispatch)",
        ),
    ]
    lines = [f"  {'ok' if ok else 'FIX'} {text}" for ok, text in checks]
    return all(ok for ok, _ in checks), ["orch doctor", *lines, optional]


def keys_lines(
    access: Access, provider: str, from_stdin: bool,
    secret: Callable[[str], str] = getpass.getpass,
) -> list[str]:
    """Bring your own key: store it on this machine only. There is no local spending cap.

    The key is read without echo (or from stdin for unattended setup), never from the
    command line, and lands in the OS keychain (macOS) or a 0600 file under the data
    directory. Nothing is sent anywhere until a run makes a call; the provider's own
    balance is the only limit (an empty balance stops dispatch).
    """
    label = {"dsh": "DeepSeek API key (DSF builder)", "jev": "Vercel AI Gateway key (Jev)"}
    operation = access.operation(provider, 0, "balance")
    key = sys.stdin.readline().strip() if from_stdin else secret(f"{label[provider]}: ")
    access.authorize(provider, 0, "balance", str(operation["operation_digest"]), key.strip())
    status = access.status(provider)
    return [
        f"{provider}: key stored on this machine; no local spending cap "
        f"(spent ¥{cast(int, status['spent_cents']) / 100:.2f} so far)",
        "next: orch doctor",
    ]


def rule_lines(state: OrchState, rule_id: str, status: str) -> list[str]:
    """List every rule, or show one; `status` applies a manual promote/retire."""
    rules = state.rules()
    if not rule_id:
        return ["rule kind    status     gain   with  without  proxy w/wo  text"] + [
            f"{r.id:<4} {r.kind:<7} {r.status:<9} {r.gain:+.2f} {r.with_passed:>3}/"
            f"{r.with_total:<3} {r.without_passed:>3}/{r.without_total:<3}  "
            + (f"{r.proxy_with_passed}/{r.proxy_with_total} {r.proxy_without_passed}/"
               f"{r.proxy_without_total}" if r.kind == "planner" else "-").ljust(10)
            + f"  {r.text}"
            for r in rules
        ] if rules else [
            "no rules yet: builder rules come from Claude patches, planner rules from "
            "planning mistakes"
        ]
    found = next((r for r in rules if r.id == rule_id), None)
    if found is None:
        raise KeyError(f"unknown rule {rule_id}")
    if status:
        found = replace(found, status=status, note=f"manual {status}")
        state.save_rule(found)
    return [
        f"{found.id} {found.status}: {found.text}",
        f"  {found.kind} rule from {found.source}",
        f"  with {found.with_passed}/{found.with_total}, without "
        f"{found.without_passed}/{found.without_total}, gain {found.gain:+.2f}"
        + (" (real run outcomes)" if found.kind == "planner" else ""),
        *([f"  proxy with {found.proxy_with_passed}/{found.proxy_with_total}, without "
           f"{found.proxy_without_passed}/{found.proxy_without_total}"]
          if found.kind == "planner" else []),
        *([f"  {found.note}"] if found.note else []),
    ]


def providers(data_dir: Path) -> ProviderClients:
    """Unattended runs need a deadline: a hung vendor CLI fails one rung, not the night.

    The default applies to this client only. A legacy zero/unlimited setting becomes
    900 seconds for orchestration; positive explicit deadlines still win.
    """
    previous = os.environ.get("ORCHESTRA_NATIVE_TIMEOUT")
    if previous is not None and float(previous) != 0:
        return ProviderClients(data_dir)
    os.environ["ORCHESTRA_NATIVE_TIMEOUT"] = "900"
    try:
        return ProviderClients(data_dir)
    finally:
        if previous is None:
            del os.environ["ORCHESTRA_NATIVE_TIMEOUT"]
        else:
            os.environ["ORCHESTRA_NATIVE_TIMEOUT"] = previous


def jev_lines(state: OrchState, run: Run) -> list[str]:
    """How Jev's triage compared with the deterministic ladder: calibration evidence."""
    events = state.events(run.id)
    triage = [e.data for e in events if e.kind == "jev.triage"]
    screens = [e.data for e in events if e.kind == "jev.screen"]
    calls = [e.data for e in events if e.kind == "jev.call"]
    names = {level: name for level, (name, _) in RUNGS.items()}
    agree = sum(1 for t in triage if t["choice"] == names.get(cast(int, t["deterministic"])))
    confident = [t for t in triage if t["level"] is not None]
    spent = sum(
        cast(int, cast(dict[str, object], c["usage"]).get("settled_cents", 0)) for c in calls
    )
    lines = [
        f"jev calls {len(calls)} (failed {sum(c['outcome'] != 'ok' for c in calls)}), "
        f"spent ¥{spent / 100:.2f}",
        f"triage {len(triage)}: agrees with the rules {agree}, confident {len(confident)}, "
        f"applied {sum(1 for t in triage if t['applied'])}",
        f"screens {len(screens)}: flagged {sum(1 for x in screens if x['flagged'])}, "
        f"applied {sum(1 for x in screens if x['applied'])}",
    ]
    lines += [
        f"  {e.task_id} a{e.span.rsplit('/a', 1)[-1]}: rules {e.data['deterministic']}, "
        f"jev {e.data['choice'] or '-'} {cast(float, e.data['confidence']):.2f}"
        + (" (applied)" if e.data["applied"] else "")
        for e in events
        if e.kind == "jev.triage"
    ]
    return lines


def no_model(*args: object, **kwargs: object) -> NativeResult:
    """`orch mutate` and `orch evolve --check` never call a model."""
    raise RuntimeError("this command does not call models")


def proxy_lines(orchestrator: Orchestrator) -> list[str]:
    """How well the planner proxy predicts real outcomes (the design's periodic check)."""
    trusted, rho, runs = orchestrator.proxy_trust()
    history = orchestrator.state.planning_history()
    lines = [
        f"planner proxy vs real outcome over {runs} run(s): "
        + (f"rho {rho:+.2f}, " if rho is not None else f"needs {PROXY_RUNS} runs, ")
        + ("trusted" if trusted else "untrusted: planner rules are judged on real outcomes")
    ]
    lines += [f"  {run} proxy {score:.2f}  blocked {blocked:.2f}"
              for run, score, blocked in history]
    return lines


def mutation_lines(state: OrchState, run: Run) -> list[str]:
    """Mutation scores of the latest measurement per task, weakest first."""
    latest: dict[str, dict[str, object]] = {}
    for event in state.events(run.id):
        if event.kind == "task.mutation":
            latest[event.task_id] = event.data
    if not latest:
        return []
    total = sum(cast(int, data["total"]) for data in latest.values())
    killed = sum(cast(int, data["killed"]) for data in latest.values())
    lines = [f"mutation score {killed / total if total else 1:.2f} ({killed}/{total} mutants "
             f"killed across {len(latest)} task(s))"]
    weakest = sorted(latest.items(), key=lambda item: (cast(float, item[1]["score"]), item[0]))
    for task_id, data in weakest:
        survivors = cast(list[dict[str, object]], data["survivors"])
        if not survivors:
            continue
        gaps = cast(list[str], data["gaps"])
        lines.append(f"  {task_id} {cast(float, data['score']):.2f}: {'; '.join(gaps[:2])}")
        lines += [f"    {s['path']}:{s['line']} {s['symbol']}: {s['before']} -> {s['after']}"
                  for s in survivors[:3]]
    if any(cast(list[object], data["survivors"]) for data in latest.values()):
        lines.append(f"  add tests that kill the survivors: orch harden --run {run.id}")
    return lines


def report_text(state: OrchState, run: Run) -> str:
    lines = [*status_lines(state, run, full=False), "", *cost_lines(state, run, "task"),
             "", *cost_lines(state, run, "model")]
    scores = mutation_lines(state, run)
    lines += ["", *scores] if scores else []
    if run.status == "done":
        lines += ["", f"merge when satisfied: git merge {run.branch}"]
    return "\n".join(lines) + "\n"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="orch", description=__doc__)
    root.add_argument("--repo", type=Path, default=None, help="target git repository (cwd)")
    root.add_argument(
        "--data-dir", type=Path,
        default=default_data_dir(),
    )
    commands = root.add_subparsers(dest="command", required=True)
    bench = commands.add_parser("bench", help="evaluate strategies with held-out acceptance tests")
    bench.add_argument("suite", nargs="?", type=Path, help="versioned suite JSON")
    bench.add_argument("--output", type=Path, required=True, help="new output directory")
    bench.add_argument("--report", action="store_true", help="read saved results; no model calls")
    bench.add_argument("--json", action="store_true")
    bench.add_argument("--live", action="store_true", help="use configured providers; default Mock")
    bench.add_argument("--max-calls", type=int, default=20,
                       help="suite-wide model dispatch cap, including planning and repairs")
    bench.add_argument("--max-cny", type=float, default=0,
                       help="optional suite-wide spend stop in CNY (0 = off, the default)")
    bench.add_argument("--jobs", type=int, default=2)
    bench.add_argument("--repeats", type=int, default=1)
    bench.add_argument("--strategy", choices=(
        "opus-dsf", "sonnet-dsf", "single-opus", "single-sonnet", "single-dsf"),
                       default="opus-dsf")
    bench.add_argument("--baseline-attempts", type=int, default=2)
    bench.add_argument("--context-mode", choices=("full", "scoped"), default="full")
    bench.add_argument("--context-bytes", type=int, default=16000,
                       help="scoped single-model request limit, including schema")
    bench.add_argument("--policy", default="", help="frozen policy id (or current) from --repo")
    bench.add_argument("--distributed", action="store_true")
    solve = commands.add_parser("solve", help="DSF-first task with bounded on-demand context")
    solve.add_argument("spec", type=Path)
    solve.add_argument("--model", choices=("dsf", "sonnet", "opus"), default="dsf")
    solve.add_argument("--live", action="store_true", help="call the explicitly selected provider")
    solve.add_argument("--max-calls", type=int, default=2,
                       help="total dispatches, including file requests and repairs (1-10)")
    solve.add_argument("--context-bytes", type=int, default=16000)
    solve.add_argument("--json", action="store_true")
    policy = commands.add_parser("policy", help="version, evaluate, promote and roll back policies")
    policy_commands = policy.add_subparsers(dest="policy_command", required=True)
    enable = policy_commands.add_parser("enable", help="freeze current active rules as baseline")
    enable.add_argument("--system", action="store_true", help="also freeze scheduling defaults")
    policy_commands.add_parser("status", help="active snapshot and append-only activation history")
    policy_commands.add_parser("show", help="read any immutable snapshot").add_argument("policy_id")
    snapshot = policy_commands.add_parser("snapshot", help="stage a candidate without activation")
    snapshot.add_argument("--include", nargs="*", default=[])
    snapshot.add_argument("--exclude", nargs="*", default=[])
    snapshot.add_argument("--jobs", type=int)
    snapshot.add_argument("--test-jobs", type=int)
    snapshot.add_argument("--verify-jobs", type=int)
    snapshot.add_argument("--cache-mode", choices=("auto", "off"))
    suggest = policy_commands.add_parser("suggest", help="stage a scheduling hypothesis from a run")
    suggest.add_argument("run_id")
    promote = policy_commands.add_parser("promote", help="gate complete paired held-out reports")
    promote.add_argument("policy_id")
    promote.add_argument("--baseline", type=Path, required=True)
    promote.add_argument("--candidate", type=Path, required=True)
    rollback = policy_commands.add_parser("rollback", help="restore a previously accepted version")
    rollback.add_argument("policy_id")
    environment = commands.add_parser("env", help="pin an installed curated pytest bundle")
    env_commands = environment.add_subparsers(dest="env_command", required=True)
    for env_action in ("create", "inspect"):
        env_command = env_commands.add_parser(env_action)
        env_command.add_argument("path", type=Path)
        if env_action == "create":
            env_command.add_argument("--package", action="append", default=[],
                                     help="include an installed pure-Python distribution")
            env_command.add_argument("--source-root", action="append", default=[],
                                     help="add a repository import root, e.g. src (root is first)")
    verify = commands.add_parser("verify", help="sandbox pytest against an isolated Git revision")
    verify.add_argument("names", nargs="*", help="pytest nodeids; default all tests")
    verify.add_argument("--revision", default="HEAD")
    verify.add_argument("--environment", type=Path, required=True)
    verify.add_argument("--timeout", type=float, default=25)
    verify.add_argument("--json", action="store_true")
    doctor = commands.add_parser("doctor", help="check logins, DSF budget, sandbox and repository")
    doctor.add_argument("--allow-unverified-auth", action="store_true",
                        help="allow configured first inference; known auth rejection still fails")
    keys = commands.add_parser("keys", help="bring your own key: DeepSeek (dsh) or Jev")
    keys.add_argument("provider", choices=("dsh", "jev"))
    keys.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)  # no longer asked
    keys.add_argument("--key-stdin", action="store_true",
                      help="read the key from stdin (unattended setup); never pass it as an arg")
    plan = commands.add_parser("plan", help="Claude plans; lint gates; stubs+tests checkpoint")
    plan.add_argument("spec", type=Path, nargs="?")
    plan.add_argument("--resume", default="", metavar="RUN_ID",
                      help="resume pinned distributed planning without regenerating saved phases")
    plan.add_argument("--ack-unknown-call", action="append", default=[], metavar="CALL_ID",
                      help="allow possible repeat billing for this unresolved planning call")
    plan.add_argument("--base", default="HEAD")
    plan.add_argument("--strategy", choices=("opus-dsf", "sonnet-dsf"))
    plan.add_argument("--distributed", action=argparse.BooleanOptionalAction, default=True,
                      help="scoped boundary planning plus independent parallel DSF test design "
                           "(default; --no-distributed selects the single-draft planner)")
    plan.add_argument("--reviewed-boundary", action="store_true",
                      help="DSF drafts boundaries; the configured planner reviews semantics")
    plan.add_argument("--test-jobs", type=int,
                      help="test-design concurrency (1..16, frozen tuning or default 16)")
    plan.add_argument("--integration-checks", action=argparse.BooleanOptionalAction, default=None,
                      help="independent tests for multi-upstream terminal tasks (default: on)")
    plan.add_argument("--environment", type=Path,
                      help="pin pytest project baseline/final gates through resume")
    plan.add_argument("--samples", type=int, default=1,
                      help="parallel plan drafts (1-4); a deterministic score picks one")
    plan.add_argument("--modules", action="store_true",
                      help="large builds: module boundaries first, then parallel sub-plans")
    evolve = commands.add_parser(
        "evolve", help="replay past specs to judge a planner candidate rule (or --check)"
    )
    evolve.add_argument("rule_id", nargs="?", default="")
    evolve.add_argument("--replays", type=int, default=3)
    evolve.add_argument("--check", action="store_true",
                        help="only compare the planner proxy with real run outcomes")
    for name in ("run", "resume"):
        command = commands.add_parser(name, help="execute (or continue) a run")
        command.add_argument("run_id", nargs="?", default="")
        command.add_argument("--jobs", type=int, help="override frozen tuning or default 4")
        command.add_argument("--cache-mode", choices=("auto", "off"),
                             help="override frozen tuning or default auto cache scheduling")
        command.add_argument(
            "--jev", choices=("auto", *JEV_MODES), default="auto",
            help="Jev triage/pre-screen: auto = on when configured; shadow only records",
        )
        command.add_argument(
            "--mutants", type=int, default=12,
            help="AST mutants per merged task to score its tests at the end (0 = off)",
        )
        command.add_argument("--verify-jobs", type=int,
                             help="concurrent sandbox runs (frozen tuning or CPU count)")
        command.add_argument("--rpm", type=float, default=0,
                             help="builder calls started per minute across the run (0 = off)")
        command.add_argument("--notify", action="store_true",
                             help="desktop notification when the run stops (macOS/Linux)")
        command.add_argument("--max-cny", type=float, default=0,
                             help="pause when this run's model spend reaches it (0 = off)")
    audit_command = commands.add_parser("audit", help="check a run's invariants (and tests)")
    audit_command.add_argument("run_id", nargs="?", default="")
    audit_command.add_argument("--no-tests", action="store_true")
    mutate = commands.add_parser("mutate", help="score merged tasks' tests with AST mutants")
    harden = commands.add_parser("harden", help="Claude adds tests that kill surviving mutants")
    for command in (mutate, harden):
        command.add_argument("tasks", nargs="*", help="task ids (default: every merged task)")
        command.add_argument("--run", default="")
        command.add_argument("--cap", type=int, default=12, help="mutants per task")
    mutate.add_argument("--jobs", type=int, default=4)
    status = commands.add_parser("status", help="tasks, states and PARKED evidence")
    status.add_argument("run_id", nargs="?", default="")
    status.add_argument("--all", action="store_true", help="one line per task even when wide")
    progress = commands.add_parser("progress", help="cluster delivery, waits, recovery and usage")
    progress.add_argument("--run", default="")
    progress.add_argument("--json", action="store_true")
    context = commands.add_parser("context", help="inspect task sources without model calls")
    context.add_argument("task")
    context.add_argument("--run", default="")
    context.add_argument("--json", action="store_true")
    context.add_argument("--show", action="store_true", help="include interface/test excerpts")
    cache = commands.add_parser("cache", help="last run's observed cache scheduling decisions")
    cache.add_argument("--run", default="")
    cache.add_argument("--json", action="store_true")
    reconcile = commands.add_parser("reconcile", help="inspect durable state and unresolved calls")
    reconcile.add_argument("--run", default="")
    reconcile.add_argument("--json", action="store_true")
    trace = commands.add_parser("trace", help="file:line or symbol -> commit -> task")
    trace.add_argument("target")
    trace.add_argument("--run", default="")
    index = commands.add_parser("index", help="code index stats, or a symbol and its users")
    index.add_argument("symbol", nargs="?", default="")
    index.add_argument("--revision", default="", help="default: the run branch, else HEAD")
    index.add_argument("--run", default="")
    why = commands.add_parser("why", help="requirement -> tests, tasks, commits")
    why.add_argument("req")
    why.add_argument("--run", default="")
    cost = commands.add_parser("cost", help="tokens, cache hits and CNY by task or model")
    cost.add_argument("--run", default="")
    cost.add_argument("--by", choices=("task", "model", "rule", "purpose"), default="task")
    cost.add_argument("--task", default="", help="one task, or [shared] for planning")
    cost.add_argument("--calls", action="store_true", help="show individual call receipts")
    cost.add_argument("--call", default="", help="inspect one exact call ID")
    cost.add_argument("--json", action="store_true", help="machine-readable receipts and totals")
    cost.add_argument("--scope", choices=("all", "online", "evolve", "harden"), default="all")
    jev = commands.add_parser("jev", help="Jev decisions compared with the deterministic rules")
    jev.add_argument("--run", default="")
    rule = commands.add_parser(
        "rule", help="learned builder and planner rules: list, show, promote, retire"
    )
    rule.add_argument("rule_id", nargs="?", default="")
    action = rule.add_mutually_exclusive_group()
    action.add_argument("--promote", action="store_true")
    action.add_argument("--retire", action="store_true")
    retry = commands.add_parser("retry", help="send a PARKED task back to READY, fresh ladder")
    retry.add_argument("task")
    retry.add_argument("--run", default="")
    retry.add_argument("--ack-unknown", action="store_true",
                       help="authorize retry despite unresolved possibly billable calls")
    retry.add_argument("--discard-candidate", action="store_true",
                       help="discard saved code and authorize fresh generation")
    mcp = commands.add_parser(
        "mcp", help="serve orch to other agent harnesses over MCP (stdio JSON-RPC)")
    mcp.add_argument("--install", choices=("claude-code", "cursor", "claude-desktop"),
                     help="register `orch mcp` with that client instead of serving")
    watch = commands.add_parser(
        "watch", help="live architecture view in the terminal (read-only; --web for a browser)")
    watch.add_argument("--run", default="", help="pin one run (default: follow the newest)")
    watch.add_argument("--db", type=Path, default=None,
                       help="read this state.sqlite instead of the repository's orch state")
    watch.add_argument("--replay", action="store_true", help="replay the run in time order")
    watch.add_argument("--speed", type=float, default=8.0, help="replay speed multiplier")
    watch.add_argument("--plain", action="store_true", help="one text line per event, no curses")
    watch.add_argument("--ascii", action="store_true", help="ASCII instead of Unicode drawing")
    watch.add_argument("--web", action="store_true", help="serve the browser view instead")
    watch.add_argument("--bind", default="127.0.0.1", help="web: listen address (non-loopback "
                       "requires a token)")
    watch.add_argument("--port", type=int, default=8780, help="web: port (0 = any free port)")
    watch.add_argument("--token", default="", help="web: bearer token (generated if needed)")
    watch.add_argument("--export", type=Path, default=None,
                       help="write one self-contained HTML file that replays offline")
    return root


def watch_command(args: argparse.Namespace, repo: Path) -> int:
    """`orch watch`: TUI by default, plain text without a terminal, browser only with --web."""
    db = cast(Path, args.db or orch_home(repo) / "state.sqlite").resolve()
    if not math.isfinite(args.speed) or args.speed <= 0:
        raise ValueError("watch --speed must be a positive number")
    if not args.web and (args.bind != "127.0.0.1" or args.token or args.port != 8780):
        raise ValueError("--bind, --port and --token apply to --web")
    if args.export is not None:
        model = Source(db, args.run).poll()
        if model is None:
            raise ValueError(f"no orchestration run to export in {db}")
        path = watch_export(model, cast(Path, args.export).resolve(), args.speed)
        print(f"orch watch: wrote {path} (self-contained; open it in any browser, no network)")
        return 0
    mode = choose(web=args.web, plain=args.plain, stdout_tty=sys.stdout.isatty(),
                  stdin_tty=sys.stdin.isatty(), term=os.environ.get("TERM", ""))
    if mode == "web":
        token = args.token or ("" if is_loopback(args.bind) else new_token())
        settings = WatchSettings(db, args.run, "replay" if args.replay else "live", args.speed,
                                 token)
        return serve(settings, args.bind, args.port)
    source = Source(db, args.run)
    load: Callable[[], Json | None] = source.poll
    if args.replay:
        frozen: list[Json | None] = []

        def load() -> Json | None:
            if frozen and frozen[0] is not None:
                return None  # a replay plays one snapshot
            frozen[:] = [source.poll()]
            return frozen[0]

    try:
        if mode == "tui":
            try:
                return tui(load, replay=args.replay, speed=args.speed, ascii_only=args.ascii,
                           env=dict(os.environ), encoding=locale.getpreferredencoding(False))
            except Exception as error:  # noqa: BLE001 - any terminal failure degrades to text
                print(f"orch watch: terminal UI unavailable ({type(error).__name__}: {error}); "
                      "falling back to plain text", file=sys.stderr)
        return plain(load, sys.stdout, replay=args.replay, speed=args.speed,
                     ascii_only=args.ascii)
    except KeyboardInterrupt:
        return 0
    finally:
        source.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repo = cast(Path, args.repo or Path.cwd()).resolve()
    if args.repo is None and repo == ROOT and args.command in {"plan", "run", "resume", "solve"}:
        print("orch: refusing to build inside the Orchestragent checkout; pass --repo <project>",
              file=sys.stderr)
        return 1
    data_dir = cast(Path, args.data_dir).resolve()
    try:
        if args.command == "watch":
            return watch_command(args, repo)
        if args.command == "mcp":
            if args.install:
                from orchestragent.orch_desktop import install
                print(install(args.install))
                return 0
            from orchestragent.orch_mcp import serve
            return serve()
        if args.command == "solve":
            if not args.live:
                raise ValueError("solve requires --live; no provider was called")
            if not 1 <= args.max_calls <= 10 or not 2048 <= args.context_bytes <= 32768:
                raise ValueError("solve needs 1-10 calls and 2048-32768 context bytes")
            spec = cast(Path, args.spec).read_text()
            if not spec.strip():
                raise ValueError("solve needs a nonempty specification")
            route = {"dsf": DSF, "sonnet": SONNET_PLAN, "opus": CLAUDE_PLAN}[args.model]
            solver_clients = providers(data_dir)
            try:
                orchestrator = Orchestrator(
                    repo, solver_clients.call, LiveWorkspace.run_unittests,
                    notify=lambda message: print(message, file=sys.stderr),
                )
                run_id, revision, status = baseline(
                    orchestrator, spec, attempts=args.max_calls, route=route,
                    context_mode="scoped", context_bytes=args.context_bytes,
                )
            finally:
                solver_clients.close()
            result = {"run_id": run_id, "status": status, "revision": revision,
                      "branch": f"orch/{run_id}/single", "model": route.model,
                      "context_bytes": args.context_bytes,
                      "usage": usage_report(orchestrator.state.usage.calls(run_id), "model")}
            print(dumps(result) if args.json else
                  f"{status}: {run_id}\nmodel: {route.model}\nrevision: {revision}\n"
                  f"candidate branch: {result['branch']}\nusage: orch cost --run {run_id}")
            return EXIT.get(status, 3)
        if args.command == "env":
            environment = (PytestEnvironment.create(
                args.path, packages=tuple(args.package), source_roots=(".", *args.source_root))
                           if args.env_command == "create"
                           else PytestEnvironment.load(args.path))
            print(dumps({**asdict(environment), "root": str(environment.root)}))
            return 0
        if args.command == "verify":
            environment = PytestEnvironment.load(args.environment)
            revision = Workspace.git(repo, "rev-parse", "--verify", "--end-of-options",
                                     f"{args.revision}^{{commit}}")
            with tempfile.TemporaryDirectory(prefix="orch-pytest-verify-") as directory:
                tree, scratch = Path(directory) / "tree", Path(directory) / "scratch"
                scratch.mkdir()
                Workspace.git(repo, "worktree", "add", "--detach", str(tree), revision)
                try:
                    passed, detail = LiveWorkspace.run_pytest(
                        tree, scratch, revision, args.names or None,
                        environment=environment, timeout=args.timeout)
                finally:
                    Workspace.git(repo, "worktree", "remove", "--force", str(tree))
            print(dumps({"passed": passed, "revision": revision,
                         "environment": environment.fingerprint, "report": detail})
                  if args.json else detail)
            return 0 if passed else 3
        if args.command == "policy":
            registry = PolicyStore(state_of(repo))
            if args.policy_command == "enable":
                print(dumps(asdict(registry.enable(tuning=Tuning() if args.system else None))))
            elif args.policy_command == "snapshot":
                changes = {key: getattr(args, key) for key in
                           ("jobs", "test_jobs", "verify_jobs", "cache_mode")
                           if getattr(args, key) is not None}
                active = registry.current()
                previous = active.tuning if active and active.tuning else Tuning()
                tuning = replace(previous, **changes) if changes else None
                candidate = registry.snapshot(tuple(args.include), tuple(args.exclude),
                                              tuning=tuning,
                                              expected_parent=active.id if active else None)
                print(dumps(asdict(candidate)))
            elif args.policy_command == "suggest":
                print(dumps(asdict(registry.suggest(args.run_id))))
            elif args.policy_command == "promote":
                print(dumps(asdict(registry.promote(args.policy_id, read_report(args.baseline),
                                                  read_report(args.candidate)))))
            elif args.policy_command == "rollback":
                print(dumps(asdict(registry.rollback(args.policy_id))))
            elif args.policy_command == "show":
                print(dumps(asdict(registry.get(args.policy_id))))
            else:
                active = registry.current()
                print(dumps({"active": asdict(active) if active else None,
                             "history": registry.history()}))
            return 0
        if args.command == "bench":
            output = cast(Path, args.output).resolve()
            if args.report:
                result = read_report(output)
            else:
                if args.suite is None:
                    raise ValueError("bench requires a suite JSON (or --report)")
                if not math.isfinite(args.max_cny) or args.max_cny < 0:
                    raise ValueError("bench --max-cny must be finite and not negative")
                if (not 1 <= args.baseline_attempts <= 10
                        or not 2048 <= args.context_bytes <= 32768):
                    raise ValueError("bench needs 1-10 baseline calls and 2048-32768 context bytes")
                if args.context_mode == "scoped" and not args.strategy.startswith("single-"):
                    raise ValueError("scoped context is supported only for single-model strategies")
                if args.strategy.startswith("single-") and not args.live:
                    raise ValueError(f"{args.strategy} requires --live; "
                                     "use Mock for scheduler tests")
                frozen = None
                if args.policy:
                    registry = PolicyStore(state_of(repo))
                    frozen = (registry.current() if args.policy == "current"
                              else registry.get(args.policy))
                    if frozen is None:
                        raise ValueError("No active policy; use policy enable first")
                clients = providers(data_dir) if args.live else None
                def caller_factory(case: Case) -> Caller:
                    if clients is not None and frozen is not None:
                        # Future candidates must not reuse an already observed validation spec,
                        # including runs that fail before reaching promotion.
                        registry.observe_spec(case.spec)
                    return (clients.call if clients is not None else
                            mock_caller(case, args.strategy, distributed=args.distributed))

                try:
                    result = run_suite(
                        args.suite, output, caller_factory,
                        LiveWorkspace.run_unittests, mode="real" if clients else "mock",
                        max_calls=args.max_calls, jobs=args.jobs, repeats=args.repeats,
                        max_cents=round(args.max_cny * 100),
                        policy=frozen, strategy=args.strategy, distributed=args.distributed,
                        baseline_attempts=args.baseline_attempts,
                        context_mode=args.context_mode, context_bytes=args.context_bytes,
                        provider_timeout=getattr(clients, "timeout", None),
                        transport=(dsf_transport()
                                   if clients is not None
                                   and (not args.strategy.startswith("single-")
                                        or args.strategy == "single-dsf")
                                   else ""),
                    )
                except KeyboardInterrupt:
                    result = read_report(output)
                finally:
                    if clients:
                        clients.close()
            print(dumps(result) if args.json else "\n".join(report_lines(result)))
            if args.report:
                return 0
            if result["passed_trials"] == result["expected_trials"]:
                return 0
            return 4 if result["success_rate"] is None else 3
        if args.command == "keys":
            try:
                lines = keys_lines(Access(data_dir), args.provider, args.key_stdin)
            except (EOFError, KeyboardInterrupt):
                print("\norch keys: cancelled; nothing was stored", file=sys.stderr)
                return 1
            print("\n".join(lines))
            return 0
        if args.command == "doctor":
            clients = providers(data_dir)
            try:
                ok, lines = doctor_lines(
                    repo, clients.status(), clients.access.status("dsh"),
                    LiveWorkspace.run_unittests, data_dir / "orch-doctor",
                    allow_unverified_auth=args.allow_unverified_auth,
                )
            finally:
                clients.close()
            print("\n".join(lines))
            return 0 if ok else 1
        if args.command in {"plan", "run", "resume"}:
            pending = None
            if args.command == "plan":
                if bool(args.spec) == bool(args.resume):
                    raise ValueError("Provide a spec file or --resume RUN_ID, exclusively")
                if args.ack_unknown_call and not args.resume:
                    raise ValueError("--ack-unknown-call requires --resume")
                if args.reviewed_boundary and not (args.distributed or args.resume):
                    raise ValueError("--reviewed-boundary requires --distributed")
                if args.resume:
                    pending = PlanningStore(state_of(repo).store).session(args.resume)
                    if (args.strategy is not None and args.strategy != pending.strategy
                            or args.base != "HEAD" or args.modules or args.samples != 1
                            or args.test_jobs is not None and args.test_jobs != pending.test_jobs
                            or args.reviewed_boundary and not pending.reviewed_boundary):
                        raise ValueError("Resume uses the pinned strategy, base and planning mode")
                    if (args.integration_checks is not None
                            and args.integration_checks != pending.integration_checks):
                        raise ValueError("Resume uses the pinned integration test policy")
            project_environment = (PytestEnvironment.load(args.environment)
                                   if args.command == "plan" and args.environment else None)
            saved = None if args.command == "plan" else pick(state_of(repo), args.run_id)
            strategy = (args.strategy or "opus-dsf") if saved is None else saved.strategy
            if pending is not None:
                strategy = pending.strategy
            requested_jev = str(getattr(args, "jev", "off"))
            if strategy == "sonnet-dsf" and requested_jev not in {"off", "auto"}:
                raise ValueError("sonnet-dsf uses only Sonnet and DSF; Jev must be off")
            clients = providers(data_dir)
            mode = str(getattr(args, "jev", "off"))
            if strategy == "sonnet-dsf":
                mode = "off"
            ready = clients.jev.status()["ready"] is True
            if mode != "off" and not ready:
                if mode != "auto":
                    print("orch: Jev is not configured; continuing without it", file=sys.stderr)
                mode = "off"
            orchestrator = Orchestrator(
                repo, clients.call, LiveWorkspace.run_unittests,
                decide=clients.jev.decide,
                jev_mode="on" if mode == "auto" else mode,
                environment=project_environment,
                strategy=strategy,
            )
            try:
                if args.command == "plan":
                    if args.resume:
                        run = orchestrator.resume_plan(args.resume,
                                                       ack_unknown_calls=args.ack_unknown_call)
                    else:
                        run = orchestrator.plan(
                            cast(Path, args.spec).read_text(), args.base, samples=args.samples,
                            modules=args.modules, distributed=args.distributed,
                            test_jobs=args.test_jobs,
                            reviewed_boundary=args.reviewed_boundary,
                            integration_checks=(args.integration_checks
                                                if args.integration_checks is not None else True),
                        )
                else:
                    run = pick(orchestrator.state, args.run_id)
                    run = orchestrator.execute(
                        run.id, args.jobs, max(0, args.mutants), args.verify_jobs,
                        max(0.0, args.rpm), round(max(0.0, args.max_cny) * 100),
                        cache_mode=args.cache_mode,
                    )
            finally:
                clients.close()
            report = report_text(orchestrator.state, run)
            (orchestrator.run_dir(run.id) / "REPORT.md").write_text(report)
            print(report, end="")
            if getattr(args, "notify", False):
                from orchestragent.orch_desktop import notify
                notify(f"orch {run.id}: {run.status}", f"{repo.name} → branch {run.branch}")
            return EXIT.get(run.status, 0)
        if args.command == "audit":
            orchestrator = Orchestrator(repo, no_model, LiveWorkspace.run_unittests)
            run = pick(orchestrator.state, args.run_id)
            with orchestrator.exclusive(run.id):
                findings = audit(orchestrator, run.id, run_tests=not args.no_tests)
            print("\n".join([f"audit {run.id}: {len(findings)} finding(s)",
                             *(f"  {line}" for line in findings)]))
            return 0 if not findings else 1
        if args.command == "evolve":
            claude = None if args.check else providers(data_dir)
            orchestrator = Orchestrator(
                repo, claude.call if claude else no_model, LiveWorkspace.run_unittests
            )
            try:
                lines = proxy_lines(orchestrator) if args.check else orchestrator.evolve(
                    args.rule_id, max(1, args.replays)
                )
            finally:
                if claude:
                    claude.close()
            print("\n".join(lines))
            return 0
        if args.command in {"mutate", "harden"}:
            claude = providers(data_dir) if args.command == "harden" else None
            orchestrator = Orchestrator(
                repo, claude.call if claude else no_model, LiveWorkspace.run_unittests,
            )
            run = pick(orchestrator.state, args.run)
            only = list(args.tasks) or None
            try:
                with orchestrator.exclusive(run.id):
                    if args.command == "mutate":
                        orchestrator.mutation(run.id, args.cap, only or [
                            t.id for t in orchestrator.state.tasks(run.id) if t.status == "DONE"
                        ], args.jobs)
                        lines = mutation_lines(orchestrator.state, run) or ["no merged tasks"]
                    else:
                        lines = orchestrator.harden(run.id, only, args.cap)
            finally:
                if claude:
                    claude.close()
            print("\n".join(lines))
            return 0
        state = state_of(repo)
        if args.command == "progress":
            observed = progress_snapshot(state, args.run)
            print(dumps(asdict(observed)) if args.json else "\n".join(progress_lines(observed)))
            return 0
        if args.command == "cost":
            run_id = args.run
            if not run_id and args.call:
                matches = [call for call in state.usage.calls() if call.id == args.call]
                if matches:
                    run_id = matches[0].run_id
            if not run_id:
                try:
                    run_id = state.usage.latest_run()
                except Conflict:
                    run_id = state.latest().id
            calls = state.usage_calls(run_id)
            if not calls:
                # Distinguish a valid old run with no usage from a misspelled ID.
                state.run(run_id)
            if args.task:
                calls = [call for call in calls if (call.task_id or "[shared]") == args.task]
            if args.scope != "all":
                scopes = {"planning", "execution"} if args.scope == "online" else {args.scope}
                calls = [call for call in calls if call.scope in scopes]
            if args.call:
                calls = [call for call in calls if call.id == args.call]
                if not calls:
                    raise KeyError(f"unknown call {args.call} in run {run_id}")
            if args.json:
                print(dumps({"run_id": run_id, "scope": args.scope,
                             **usage_report(calls, args.by)}))
            else:
                print("\n".join([f"run {run_id} scope={args.scope}",
                                 *usage_lines(calls, args.by, args.calls or bool(args.call))]))
            return 0
        if args.command == "index":
            revision = args.revision
            if not revision:
                try:
                    revision = pick(state, args.run).branch
                except KeyError:
                    revision = "HEAD"
            print("\n".join(index_lines(repo, revision, args.symbol)))
            return 0
        if args.command == "rule":
            status = "active" if args.promote else "retired" if args.retire else ""
            print("\n".join(rule_lines(state, args.rule_id, status)))
            return 0
        run_id = str(getattr(args, "run_id", "") or getattr(args, "run", ""))
        run = pick(state, run_id)
        if args.command == "reconcile":
            tasks = state.tasks(run.id)
            candidates = {t.id: t.pending_candidate for t in tasks if t.pending_candidate}
            missing = []
            for task_id, commit in candidates.items():
                try:
                    if Workspace.git(repo, "cat-file", "-t", commit) != "commit":
                        missing.append(task_id)
                except subprocess.SubprocessError:
                    missing.append(task_id)
            head = Workspace.git(repo, "rev-parse", run.branch)
            reconciliation = {"run_id": run.id, "consistency": "non-atomic observation",
                        "run_status": run.status,
                        "verified_head": run.verified_head, "branch_head": head,
                        "head_matches": head == run.verified_head, "candidates": candidates,
                        "missing_candidates": missing,
                        "active": {t.id: t.status for t in tasks if t.status in IN_FLIGHT},
                        "unresolved_calls": [asdict(call) for call in state.usage.calls(run.id)
                                             if unresolved(call.outcome, call.usage)],
                        "waiting": {t.id: t.depends_on for t in tasks if t.status == "PLANNED"},
                        "parked": {t.id: t.reason for t in tasks if t.status == "PARKED"}}
            print(dumps(reconciliation))
            return 0
        if args.command == "cache":
            snapshot = next((event.data for event in reversed(state.events(run.id))
                             if event.kind == "run.cache"), None)
            result = {"run_id": run.id, "recorded_policy": snapshot}
            print(dumps(result) if args.json else
                  f"run {run.id}\ncache policy: {dumps(snapshot) if snapshot else 'not recorded'}")
            return 0
        if args.command == "context":
            task = state.task(run.id, args.task)
            context = RunContext.of(run, state.tasks(run.id))
            store = Index(repo, orch_home(repo) / "index.sqlite")
            if run.environment_root or run.environment_fingerprint:
                environment = PytestEnvironment.load(Path(run.environment_root))
                if environment.fingerprint != run.environment_fingerprint:
                    raise ValueError("Pinned environment fingerprint changed")
                store.source_roots = environment.source_roots
            index = store.at(run.verified_head)
            selected = {"owned_files": sorted(task.files),
                        "interface_files": list(context.interfaces_for(task, index)),
                        "test_files": sorted(context.tests_for(task))}
            if args.json:
                print(dumps({"run_id": run.id, "task_id": task.id,
                             "revision": run.verified_head, **selected,
                             "context_requests": task.context_requests,
                             "requested_selectors": task.context_selectors,
                             **({"interface_excerpts": context.interfaces_for(task, index),
                                 "test_excerpts": context.tests_for(task, sliced=True)}
                                if args.show else {})}))
            else:
                print(f"run {run.id} task {task.id} at {run.verified_head}\n"
                      + "\n".join(f"{kind}: {', '.join(paths) or '(none)'}"
                                  for kind, paths in selected.items())
                      + f"\ncontext requests: {task.context_requests}/3\n"
                      + f"requested: {', '.join(task.context_selectors) or '(none)'}")
                if args.show:
                    print(dumps({"interfaces": context.interfaces_for(task, index),
                                 "tests": context.tests_for(task, sliced=True)}))
            return 0
        if args.command == "status":
            lines = status_lines(state, run, full=args.all)
        elif args.command == "why":
            lines = why_lines(state, run, args.req)
        elif args.command == "jev":
            lines = jev_lines(state, run)
        elif args.command == "retry":
            task = state.retry(run.id, args.task, ack_unknown=args.ack_unknown,
                               discard_candidate=args.discard_candidate)
            lines = [f"{task.id} {task.status}; continue with `orch run {run.id}`"]
        else:
            lines = trace_lines(repo, state, run, args.target)
        print("\n".join(lines))
        return 0
    except PlanError as error:
        print(f"orch plan failed: {error}", file=sys.stderr)
        print("\n".join(f"  - {item}" for item in error.errors), file=sys.stderr)
        return PLAN_REJECTED
    except subprocess.SubprocessError as error:
        detail = getattr(error, "stderr", "") or ""
        print(f"orch: git failed in {repo} (a git repository with a commit?) {detail}".strip(),
              file=sys.stderr)
        return 1
    except (KeyError, RuntimeError, OSError, Conflict, ValueError) as error:
        print(f"orch: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
