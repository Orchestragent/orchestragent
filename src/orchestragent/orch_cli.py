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
from collections.abc import Callable, Sequence
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import cast

from orchestragent import orch_verify
from orchestragent.access import Access
from orchestragent.native import NativeResult
from orchestragent.orch_audit import audit
from orchestragent.orch_build import parse_trailers
from orchestragent.orch_desktop import CLIENTS as MCP_CLIENTS
from orchestragent.orch_planning import PlanningStore
from orchestragent.orch_progress import lines as progress_lines
from orchestragent.orch_progress import snapshot as progress_snapshot
from orchestragent.orch_run import (
    DSF,
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
from orchestragent.orch_verify import Check
from orchestragent.orch_watch import Json, Source
from orchestragent.orch_watch import export as watch_export
from orchestragent.orch_watch_server import Settings as WatchSettings
from orchestragent.orch_watch_server import is_loopback, new_token, serve
from orchestragent.orch_watch_tui import choose, plain, tui
from orchestragent.platform import default_data_dir, sandbox_name
from orchestragent.providers import ProviderClients
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
    spent = state.spent(run.id)
    attempts = sum(task.attempts for task in tasks)
    flight = [t for t in tasks if t.status in {"RUNNING", "VERIFYING", "MERGING"}]
    lines = [
        f"  {len(tasks)} task(s): "
        + ", ".join(f"{name} {counts[name]}" for name in order if counts.get(name)),
        f"  rungs {' '.join(f'{lvl}:{n}' for lvl, n in sorted(rungs.items()))}   attempts "
        f"{attempts} ({attempts / max(1, len(tasks)):.1f}/task)",
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
        lines.append(
            f"  {task.id}  {task.status:<9} attempts={task.attempts} rung={task.level}  "
            f"{','.join(task.reqs):<8} {task.goal[:48]}  {note}".rstrip()
        )
    for task in tasks:
        if task.status == "PARKED":
            lines += [f"PARKED {task.id}: {task.reason}", f"  files {', '.join(task.files)}"]
            lines += [f"  | {line}" for line in _failure_lines(task.last_error)]
            if task.human_hint:
                lines += [f"  hint {line.removeprefix('- ')}"
                          for line in task.human_hint.splitlines()]
            lines += ["  next: orch hint " + task.id + ' "<what the builder should know>"'
                      "   (retries with your guidance)",
                      f"        orch take {task.id}   (finish it yourself, then"
                      f" orch submit {task.id})",
                      f"        orch retry {task.id}  (fresh attempts, same inputs)"]
    return lines


def _failure_lines(report: str, limit: int = 12) -> list[str]:
    """The failing checks and the end of their output, without the audit trailer."""
    keep = [line for line in report.strip().splitlines()
            if line.strip() and "=" not in line.split(" ", 1)[0]]
    heads = [line for line in keep if line.startswith(("FAIL", "TIMEOUT"))]
    rest = [line for line in keep if line not in heads]
    return [*(line[:160] for line in heads[:3]), *(line[:160] for line in rest[-(limit - 3):])]


def why_lines(state: OrchState, run: Run, req: str) -> list[str]:
    texts = {
        str(item["id"]): str(item["text"])
        for item in cast(list[dict[str, str]], run.plan["requirements"])
    }
    if req not in texts:
        raise KeyError(f"{req} is not a requirement of run {run.id}")
    tests = [  # the checks of the tasks that accept this requirement
        f"{task.id}: {check.command}"
        for task in state.tasks(run.id) if req in task.reqs
        for test in task.tests if (check := orch_verify.parse(test)) is not None
    ]
    lines = [f"{req}: {texts[req]}", *[f"  check {test}" for test in tests]]
    for task in state.tasks(run.id):
        if req in task.reqs:
            lines.append(f"  task {task.id} {task.status} {task.commit[:10]}  {task.goal[:60]}")
    return lines


def cost_lines(state: OrchState, run: Run, by: str) -> list[str]:
    return usage_lines(state.usage_calls(run.id), by)


def locate(target: str) -> tuple[str, int]:
    """`file:line` as given."""
    found = re.fullmatch(r"(.+):([1-9][0-9]*)", target)
    if not found:
        raise KeyError(f"{target} is not file:line")
    return found.group(1), int(found.group(2))


def trace_lines(repo: Path, state: OrchState, run: Run, target: str) -> list[str]:
    path, line = locate(target)

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
    return lines


def canary_reason(report: str) -> str:
    """The first line of a failed canary's own output, not the run's audit trailer."""
    lines = [line.strip() for line in report.split("\n\n--- ", 1)[-1].splitlines()[1:]
             if line.strip() and "=" not in line.split(" ", 1)[0]]
    return (lines[0] if lines else report.strip().splitlines()[0])[:160]


CANARY = Check("sh canary.sh", ("canary.sh",))


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
    # The canary passes only when processes run and the network is closed.
    (canary / "canary.sh").write_text(
        'echo ok | grep -q ok || exit 1\n'
        'if command -v python3 >/dev/null; then python3 -c "import socket\n'
        'try:\n socket.create_connection((\'1.1.1.1\', 80), 3)\nexcept OSError: raise '
        'SystemExit(0)\nraise SystemExit(1)"; fi\n')
    try:
        passed, report = runner(canary, scratch, "doctor", [CANARY.id])
        checks.append((passed, f"sandbox {sandbox_name()}: " + (
            "canary passed (processes run, network closed)" if passed
            else canary_reason(report)
        )))
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        checks.append((False, f"sandbox {sandbox_name()}: {error}"))
    from orchestragent import orch_models
    custom = {role: value for role, (value, _) in orch_models.effective().items()
              if value != orch_models.VERIFIED[role]}
    checks.append((True, "models: the benchmarked (verified) set" if not custom else
                   "models: custom, unverified (" + ", ".join(
                       f"{role}={value}" for role, value in custom.items())
                   + ") -> orch models reset restores the verified set"))
    from orchestragent import byok
    data = default_data_dir()
    for name, entry in byok.registry(data).items():
        has_key = bool(byok.api_key(entry, partial(Access(data).load_secret, name)))
        local = str(entry.get("base_url", "")).startswith("http://localhost")
        checks.append((has_key or local, f"provider {name} ({entry.get('base_url')}): "
                       + ("key present" if has_key else "local, no key" if local
                          else f"key missing -> orch keys {name}")))
    claude = cast(dict[str, object], providers.get("claude", {}))
    claude_modes = {
        "claude_subscription": "Claude subscription",
        "claude_api_key": "Anthropic API key (billed per token)",
        "claude_bedrock": "Amazon Bedrock", "claude_vertex": "Google Vertex AI",
    }
    auth = str(claude.get("auth", "unknown"))
    checks.append((
        claude.get("ready") is True,
        f"claude (planner) via {claude_modes[auth]}" if claude.get("ready") is True
        else f"claude (planner): {auth} -> claude auth login (subscription), or set "
             "ANTHROPIC_API_KEY as Claude Code documents",
    ))
    dsh = cast(dict[str, object], providers.get("dsh", {}))
    spent = cast(int, budget.get("spent_cents", 0))
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
               else "missing -> orch keys dsh"),
        ),
        (
            True,  # informational: no local cap; only the provider's own balance can stop calls
            f"dsh spent ¥{spent / 100:.2f} on this machine; no local cap "
            "(an empty DeepSeek balance stops dispatch)",
        ),
    ]
    lines = [f"  {'ok' if ok else 'FIX'} {text}" for ok, text in checks]
    return all(ok for ok, _ in checks), ["orch doctor", *lines]


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
    label = {"dsh": "DeepSeek API key (DSF builder)"}
    operation = access.operation(provider, 0, "balance")
    key = sys.stdin.readline().strip() if from_stdin else secret(f"{label[provider]}: ")
    access.authorize(provider, 0, "balance", str(operation["operation_digest"]), key.strip())
    status = access.status(provider)
    return [
        f"{provider}: key stored on this machine; no local spending cap "
        f"(spent ¥{cast(int, status['spent_cents']) / 100:.2f} so far)",
        "next: orch doctor",
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


def no_model(*args: object, **kwargs: object) -> NativeResult:
    """Commands that only read or merge finished work never call a model."""
    raise RuntimeError("this command does not call models")






def report_text(state: OrchState, run: Run) -> str:
    lines = [*status_lines(state, run, full=False), "", *cost_lines(state, run, "task"),
             "", *cost_lines(state, run, "model")]
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
    doctor = commands.add_parser("doctor", help="check logins, DSF budget, sandbox and repository")
    doctor.add_argument("--allow-unverified-auth", action="store_true",
                        help="allow configured first inference; known auth rejection still fails")
    keys = commands.add_parser(
        "keys", help="bring your own key: dsh (DeepSeek) or a registered provider")
    keys.add_argument("provider", help="dsh, or a name from `orch providers list`")
    keys.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)  # no longer asked
    keys.add_argument("--key-stdin", action="store_true",
                      help="read the key from stdin (unattended setup); never pass it as an arg")
    plan = commands.add_parser(
        "plan", help="the planner drafts tasks and stubs; test designers freeze checks")
    plan.add_argument("spec", type=Path, nargs="?")
    plan.add_argument("--resume", default="", metavar="RUN_ID",
                      help="resume pinned distributed planning without regenerating saved phases")
    plan.add_argument("--ack-unknown-call", action="append", default=[], metavar="CALL_ID",
                      help="allow possible repeat billing for this unresolved planning call")
    plan.add_argument("--base", default="HEAD")
    plan.add_argument("--reviewed-boundary", action="store_true",
                      help="DSF drafts boundaries; the configured planner reviews semantics")
    plan.add_argument("--test-jobs", type=int,
                      help="test-design concurrency (1..16, default 16)")
    plan.add_argument("--integration-checks", action=argparse.BooleanOptionalAction, default=None,
                      help="independent tests for multi-upstream terminal tasks (default: on)")
    for name in ("run", "resume"):
        command = commands.add_parser(name, help="execute (or continue) a run")
        command.add_argument("run_id", nargs="?", default="")
        command.add_argument("--jobs", type=int, help="concurrent model calls (default 4)")
        command.add_argument("--verify-jobs", type=int,
                             help="concurrent sandbox runs (default: CPU count)")
        command.add_argument("--rpm", type=float, default=0,
                             help="builder calls started per minute across the run (0 = off)")
        command.add_argument("--notify", action="store_true",
                             help="desktop notification when the run stops (macOS/Linux)")
        command.add_argument("--max-cny", type=float, default=0,
                             help="pause when this run's model spend reaches it (0 = off)")
    audit_command = commands.add_parser("audit", help="check a run's invariants (and tests)")
    audit_command.add_argument("run_id", nargs="?", default="")
    audit_command.add_argument("--no-tests", action="store_true")
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
    reconcile = commands.add_parser("reconcile", help="inspect durable state and unresolved calls")
    reconcile.add_argument("--run", default="")
    reconcile.add_argument("--json", action="store_true")
    trace = commands.add_parser("trace", help="file:line -> commit -> task")
    trace.add_argument("target")
    trace.add_argument("--run", default="")
    why = commands.add_parser("why", help="requirement -> tests, tasks, commits")
    why.add_argument("req")
    why.add_argument("--run", default="")
    cost = commands.add_parser("cost", help="tokens, cache hits and CNY by task or model")
    cost.add_argument("--run", default="")
    cost.add_argument("--by", choices=("task", "model", "purpose"), default="task")
    cost.add_argument("--task", default="", help="one task, or [shared] for planning")
    cost.add_argument("--calls", action="store_true", help="show individual call receipts")
    cost.add_argument("--call", default="", help="inspect one exact call ID")
    cost.add_argument("--json", action="store_true", help="machine-readable receipts and totals")
    cost.add_argument("--scope", choices=("all", "online", "evolve", "harden"), default="all")
    retry = commands.add_parser("retry", help="send a PARKED task back to READY, fresh ladder")
    retry.add_argument("task")
    retry.add_argument("--run", default="")
    retry.add_argument("--ack-unknown", action="store_true",
                       help="authorize retry despite unresolved possibly billable calls")
    retry.add_argument("--discard-candidate", action="store_true",
                       help="discard saved code and authorize fresh generation")
    hint = commands.add_parser(
        "hint", help="give a task guidance for its next attempts (a parked task retries)")
    hint.add_argument("task")
    hint.add_argument("text", help="what the builder should know, in plain words")
    hint.add_argument("--run", default="")
    take = commands.add_parser(
        "take", help="finish a task yourself: check out its best code to edit")
    take.add_argument("task")
    take.add_argument("--run", default="")
    submit = commands.add_parser(
        "submit", help="verify your edits with the task's frozen checks and merge them")
    submit.add_argument("task")
    submit.add_argument("--run", default="")
    providers_cmd = commands.add_parser(
        "providers", help="bring-your-own-key providers (any OpenAI-compatible API)")
    providers_commands = providers_cmd.add_subparsers(dest="providers_command")
    provider_add = providers_commands.add_parser("add", help="register an endpoint")
    provider_add.add_argument("name")
    provider_add.add_argument("--base-url", required=True,
                              help="e.g. https://api.openai.com/v1 or http://localhost:11434/v1")
    provider_add.add_argument("--key-env", default="",
                              help="read the key from this environment variable")
    provider_add.add_argument("--max-tokens", type=int, default=0,
                              help="output limit to request (0 = provider default)")
    providers_commands.add_parser("list", help="registered providers (the default)")
    provider_remove = providers_commands.add_parser("remove", help="unregister")
    provider_remove.add_argument("name")
    provider_test = providers_commands.add_parser("test", help="one tiny real request")
    provider_test.add_argument("name")
    provider_test.add_argument("model")
    models = commands.add_parser(
        "models", help="which model serves each role; the benchmarked set is the default")
    models_commands = models.add_subparsers(dest="models_command")
    models_set = models_commands.add_parser("set", help="choose a role's model (unverified)")
    models_set.add_argument("role", choices=("planner", "supervisor", "test_designer", "builder"))
    models_set.add_argument("value", help="provider/model:effort")
    models_reset = models_commands.add_parser("reset", help="back to the verified set")
    models_reset.add_argument("role", nargs="?", default="",
                              choices=("", "planner", "supervisor", "test_designer", "builder"))
    app = commands.add_parser("app", help="macOS: install Orch.app, a double-click dashboard")
    app_commands = app.add_subparsers(dest="app_command", required=True)
    app_install = app_commands.add_parser("install", help="write Orch.app")
    app_install.add_argument("--dest", type=Path,
                             default=Path.home() / "Applications" / "Orch.app")
    completion = commands.add_parser(
        "completion", help="print shell completion (bash, zsh, fish) for orch")
    completion.add_argument("shell", choices=("bash", "zsh", "fish"))
    mcp = commands.add_parser(
        "mcp", help="serve orch to other agent harnesses over MCP (stdio JSON-RPC)")
    mcp.add_argument("--install", choices=MCP_CLIENTS,
                     help="register `orch mcp` with that client instead of serving")
    watch = commands.add_parser(
        "watch", help="live architecture view in the terminal (read-only; --web for a browser)")
    watch.add_argument("--run", default="", help="pin one run (default: follow the newest)")
    watch.add_argument("--db", type=Path, default=None,
                       help="read this state.sqlite instead of the repository's orch state")
    watch.add_argument("--replay", action="store_true", help="replay the run in time order")
    watch.add_argument("--speed", type=float, default=8.0, help="replay speed multiplier")
    watch.add_argument("--plain", action="store_true", help="one text line per event, no curses")
    watch.add_argument("--once", action="store_true",
                       help="print what has happened so far and the status, then exit")
    watch.add_argument("--ascii", action="store_true", help="ASCII instead of Unicode drawing")
    watch.add_argument("--web", action="store_true", help="serve the browser view instead")
    watch.add_argument("--bind", default="127.0.0.1", help="web: listen address (non-loopback "
                       "requires a token)")
    watch.add_argument("--port", type=int, default=8780, help="web: port (0 = any free port)")
    watch.add_argument("--token", default="", help="web: bearer token (generated if needed)")
    watch.add_argument("--export", type=Path, default=None,
                       help="write one self-contained HTML file that replays offline")
    studio = commands.add_parser(
        "studio", help="interactive workbench API for the Orch Studio app (docs/STUDIO.md)")
    studio.add_argument("--bind", default="127.0.0.1", help="listen address (default loopback)")
    studio.add_argument("--port", type=int, default=8790, help="port (0 = any free port)")
    studio.add_argument("--token", default="", help="bearer token (generated by default)")
    studio.add_argument("--machine", action="store_true",
                        help="print one JSON line {url, token, pid, version, api}; "
                             "exit when stdin closes")
    studio.add_argument("--demo", action="store_true",
                        help="serve a scripted in-memory project (no repository, no spend)")
    return root


def studio_command(args: argparse.Namespace, repo: Path, data_dir: Path) -> int:
    from orchestragent import orch_studio_server
    if args.demo:
        from orchestragent.orch_studio_demo import DemoBackend
        backend: orch_studio_server.Backend = DemoBackend()
    else:
        from orchestragent.orch_studio import RepoBackend
        backend = RepoBackend(repo, data_dir)
    return orch_studio_server.serve(backend, args.bind, args.port, token=args.token,
                                    machine=args.machine, version=package_version())


def package_version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("orchestragent")
    except PackageNotFoundError:
        return "dev"


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
    if args.once and (args.web or args.replay):
        raise ValueError("--once prints the current state as text; not with --web or --replay")
    mode = choose(web=args.web, plain=args.plain or args.once, stdout_tty=sys.stdout.isatty(),
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
                     ascii_only=args.ascii, once=args.once)
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
        if args.command == "studio":
            return studio_command(args, repo, data_dir)
        if args.command == "models":
            from orchestragent import orch_models
            if args.models_command == "set":
                print(orch_models.set_role(args.role, args.value, data_dir))
            elif args.models_command == "reset":
                print(orch_models.reset(args.role, data_dir))
            else:
                print("\n".join(orch_models.table(data_dir)))
            return 0
        if args.command == "app":
            from orchestragent.orch_desktop import install_app
            from orchestragent.platform import is_macos
            if not is_macos():
                print("orch app: Orch.app is for macOS; on Linux use `orch watch` "
                      "(or `orch watch --web`)", file=sys.stderr)
            print(install_app(cast(Path, args.dest).expanduser()))
            return 0
        if args.command == "completion":
            from orchestragent.orch_completion import render
            print(render(args.shell, parser()), end="")
            return 0
        if args.command == "mcp":
            if args.install:
                from orchestragent.orch_desktop import install
                print(install(args.install))
                return 0
            from orchestragent.orch_mcp import serve
            return serve()
        if args.command == "providers":
            from orchestragent import byok
            if args.providers_command == "add":
                print(byok.add(data_dir, args.name, args.base_url, args.key_env,
                               args.max_tokens))
            elif args.providers_command == "remove":
                print(byok.remove(data_dir, args.name))
            elif args.providers_command == "test":
                entry = byok.registry(data_dir).get(args.name)
                if entry is None:
                    raise ValueError(f"no provider named {args.name}; orch providers add")
                key = byok.api_key(entry, lambda: Access(data_dir).load_secret(args.name))
                print(f"{args.name}/{args.model}: {byok.probe(entry, key, args.model)}")
            else:
                entries = byok.registry(data_dir)
                if not entries:
                    print("no providers registered; orch providers add NAME --base-url URL")
                for name, entry in entries.items():
                    source = (f"${entry['key_env']}" if entry.get("key_env")
                              else "orch keys " + name)
                    print(f"{name:<14} {entry['base_url']:<40} key: {source}")
            return 0
        if args.command == "keys" and args.provider != "dsh":
            from orchestragent import byok
            if args.provider not in byok.registry(data_dir):
                raise ValueError(f"unknown provider {args.provider}: register it first with "
                                 "orch providers add")
            try:
                value = (sys.stdin.readline() if args.key_stdin
                         else getpass.getpass(f"{args.provider} API key: ")).strip()
            except (EOFError, KeyboardInterrupt):
                print("\norch keys: cancelled; nothing was stored", file=sys.stderr)
                return 1
            Access(data_dir).save_secret(args.provider, value)
            print(f"{args.provider}: key stored on this machine")
            return 0
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
                    orch_verify.run, data_dir / "orch-doctor",
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
                if args.resume:
                    pending = PlanningStore(state_of(repo).store).session(args.resume)
                    if (args.base != "HEAD"
                            or args.test_jobs is not None and args.test_jobs != pending.test_jobs
                            or args.reviewed_boundary and not pending.reviewed_boundary):
                        raise ValueError("Resume uses the pinned base and planning mode")
                    if (args.integration_checks is not None
                            and args.integration_checks != pending.integration_checks):
                        raise ValueError("Resume uses the pinned integration test policy")
            clients = providers(data_dir)
            orchestrator = Orchestrator(repo, clients.call, orch_verify.run)
            try:
                if args.command == "plan":
                    if args.resume:
                        run = orchestrator.resume_plan(args.resume,
                                                       ack_unknown_calls=args.ack_unknown_call)
                    else:
                        run = orchestrator.plan(
                            cast(Path, args.spec).read_text(), args.base,
                            test_jobs=args.test_jobs,
                            reviewed_boundary=args.reviewed_boundary,
                            integration_checks=(args.integration_checks
                                                if args.integration_checks is not None else True),
                        )
                else:
                    run = pick(orchestrator.state, args.run_id)
                    run = orchestrator.execute(
                        run.id, args.jobs, args.verify_jobs,
                        max(0.0, args.rpm), round(max(0.0, args.max_cny) * 100),
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
        if args.command in {"take", "submit"}:
            orchestrator = Orchestrator(repo, no_model, orch_verify.run)
            run = pick(orchestrator.state, args.run)
            task = orchestrator.state.task(run.id, args.task)
            if args.command == "take":
                path = orchestrator.take(run.id, task.id)
                checks = [check.command for test in task.tests
                          if (check := orch_verify.parse(test)) is not None]
                print("\n".join([
                    f"{task.id} is yours: {path}",
                    f"edit only: {', '.join(task.files)}",
                    *(f"check: cd {path} && {command}" for command in checks),
                    f"then: orch submit {task.id}   (verifies, merges, unblocks dependents)",
                    *([f"last failure:\n{task.last_error.strip()[-1500:]}"]
                      if task.last_error else []),
                ]))
                return 0
            merged, report = orchestrator.submit(run.id, task.id)
            print(report if merged else f"not merged:\n{report.strip()[-4000:]}")
            return 0 if merged else 1
        if args.command == "audit":
            orchestrator = Orchestrator(repo, no_model, orch_verify.run)
            run = pick(orchestrator.state, args.run_id)
            with orchestrator.exclusive(run.id):
                findings = audit(orchestrator, run.id, run_tests=not args.no_tests)
            print("\n".join([f"audit {run.id}: {len(findings)} finding(s)",
                             *(f"  {line}" for line in findings)]))
            return 0 if not findings else 1
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
        if args.command == "context":
            task = state.task(run.id, args.task)
            context = RunContext.of(run, state.tasks(run.id))
            selected = {"owned_files": sorted(task.files),
                        "interface_files": list(context.interfaces_for(task)),
                        "test_files": sorted(context.tests_for(task))}
            if args.json:
                print(dumps({"run_id": run.id, "task_id": task.id,
                             "revision": run.verified_head, **selected,
                             "context_requests": task.context_requests,
                             "requested_selectors": task.context_selectors,
                             **({"interface_excerpts": context.interfaces_for(task),
                                 "test_excerpts": context.tests_for(task)}
                                if args.show else {})}))
            else:
                print(f"run {run.id} task {task.id} at {run.verified_head}\n"
                      + "\n".join(f"{kind}: {', '.join(paths) or '(none)'}"
                                  for kind, paths in selected.items())
                      + f"\ncontext requests: {task.context_requests}/3\n"
                      + f"requested: {', '.join(task.context_selectors) or '(none)'}")
                if args.show:
                    print(dumps({"interfaces": context.interfaces_for(task),
                                 "tests": context.tests_for(task)}))
            return 0
        if args.command == "status":
            lines = status_lines(state, run, full=args.all)
        elif args.command == "why":
            lines = why_lines(state, run, args.req)
        elif args.command == "hint":
            task = state.hint(run.id, args.task, args.text)
            lines = [f"{task.id} {task.status}; its next attempts follow your guidance",
                     f"continue with `orch run {run.id}`"]
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
