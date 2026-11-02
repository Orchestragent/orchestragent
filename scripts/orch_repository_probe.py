"""Pinned existing-repository trial using the scheduler's own gates and usage ledger.

Commit this script and the case before measurement. Without --live no provider is
constructed. Held-out acceptance is installed only in a disposable verification tree.
"""

import argparse
import json
import os
import shlex
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from orchestragent.dsh import DshClient
from orchestragent.envelope import digest
from orchestragent.live import TEXT, object_schema
from orchestragent.live_workspace import PROJECT_CPU_SECONDS, PROJECT_TEST_TIMEOUT, LiveWorkspace
from orchestragent.native import NativeResult
from orchestragent.orch_bench import Case, _accept, _Admission
from orchestragent.orch_build import apply_edits
from orchestragent.orch_cli import providers
from orchestragent.orch_environment import PytestEnvironment
from orchestragent.orch_policy import PolicyStore
from orchestragent.orch_run import Orchestrator, orch_home
from orchestragent.orch_state import OrchState
from orchestragent.orch_usage import Call, report, summary, unresolved
from orchestragent.serialization import dumps
from orchestragent.store import Store
from orchestragent.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


def positive_control(
    case_path: Path, output: Path, repo: Path, base: str,
    environment: PytestEnvironment, case: Case,
) -> tuple[bool, str, str]:
    """Validate the oracle in a separate clone; reference code never enters the agent tree."""
    files = LiveWorkspace.validate_bundle([
        {"path": p.relative_to(case_path / "reference").as_posix(), "content": p.read_text()}
        for p in sorted((case_path / "reference").rglob("*.py"))
    ], require_tests=False)
    patches = case_path / "reference-edits.json"
    edits = json.loads(patches.read_text()) if patches.exists() else []
    if not isinstance(edits, list):
        raise ValueError("Reference edits must be a list")
    owned = [f["path"] for f in files] + [e["path"] for e in edits]
    if any(LiveWorkspace.is_test_file(p) for p in owned):
        raise ValueError("Reference code cannot replace tests")
    reference = output / "reference-project"
    Workspace.git(output, "clone", "--no-hardlinks", "--local", str(repo), str(reference))
    Workspace.git(reference, "checkout", "--detach", base)
    apply_edits(reference, owned, [*edits, *[
        {"path": f["path"], "search": "", "replace": f["content"]} for f in files]])
    if protected_changes(reference, base):
        raise ValueError("Reference code cannot replace collection/configuration controls")
    Workspace.git(reference, "add", "--", *owned)
    Workspace.git(reference, "commit", "-m", "bench: independent oracle positive control")
    revision = Workspace.git(reference, "rev-parse", "HEAD")
    checker = Orchestrator(reference, lambda *a, **kw: NativeResult("policy_blocked", {}),
                           LiveWorkspace.run_unittests, environment=environment)
    trial = {"run_id": "oracle-positive", "result_revision": revision}
    try:
        passed, log = _accept(checker, trial, case, LiveWorkspace.run_unittests)
        return passed, log, revision
    finally:
        checker.release_worktrees(trial["run_id"])


def protected_changes(repo: Path, base: str) -> list[str]:
    """Freeze tests and packaging configuration; ordinary documentation edits are allowed."""
    existing = set(Workspace.git(repo, "ls-tree", "-r", "--name-only", base).splitlines())
    changed = set(Workspace.git(repo, "diff", "--name-only", "--no-renames", base).splitlines())
    changed.update(Workspace.git(repo, "ls-files", "--others").splitlines())
    configuration = {"conftest.py", "pyproject.toml", "setup.py", "setup.cfg", "tox.ini",
                     "pytest.ini", "MANIFEST.in"}
    return [p for p in sorted(changed) if (p in existing and LiveWorkspace.is_test_file(p))
            or Path(p).name in configuration or Path(p).name.startswith("requirements")]


def resume_trial(output: Path, *, repair: bool = False, planning: bool = False) -> None:
    """Continue a checkpoint under the remaining active-time/API budget; retain pilot evidence."""
    path = output / "report.json"
    raw = path.read_text()
    previous = json.loads(raw)
    expected = "error" if planning else "parked" if repair else "paused"
    if previous.get("single_agent") or previous["status"] != expected:
        raise ValueError(f"Only a {expected} cluster trial can continue")
    started_at = path.stat().st_mtime - previous["wall_s"]
    engine = Workspace.git(ROOT, "rev-parse", "HEAD")
    backup = output / (f"report-before-planning-resume-{engine[:12]}.json" if planning else
                       f"report-before-repair-{engine[:12]}.json" if repair else
                       "report-before-budget-resume.json")
    if backup.exists():
        raise ValueError("This bounded trial has already been resumed")
    backup.write_text(raw)
    os.environ["ORCHESTRA_NATIVE_TIMEOUT"] = str(previous["provider_timeout_base_s"])
    clients = providers(ROOT / ".data")
    orch = Orchestrator(output / "project", clients.call, LiveWorkspace.run_unittests,
                        notify=lambda line: print(line, flush=True))
    all_calls = orch.state.usage.calls()
    run_ids = {call.run_id for call in all_calls}
    if planning:
        if len(run_ids) != 1 or orch.state.runs() or not previous.get("distributed"):
            clients.close()
            raise ValueError("Need one unfinished distributed planning session")
        run_id = run_ids.pop()
        orch.planning.session(run_id)
    else:
        run_id = previous["run"]["id"]
    trial = {"id": previous["case"], "run_id": run_id}
    # The call ledger includes planning/test design; execution events alone do not.
    spent = sum(value for call in orch.state.usage.calls(run_id)
                if type(value := call.usage.get("settled_cents")) is int)
    available = previous["max_cents"] - spent
    active = previous.get("active_wall_s", previous["wall_s"])
    time_cap = previous.get("active_time_cap_s", 1200)
    call_cap = previous["max_calls"] - len(all_calls)
    if (available <= 0 or active >= time_cap or call_cap <= 0
            or any(unresolved(c.outcome, c.usage) for c in all_calls)):
        clients.close()
        raise ValueError("No remaining trial budget or unresolved receipts; resume refused")
    case_path = ROOT / "examples" / "orch-bench" / previous["case"]
    case = Case(previous["case"], (case_path / "spec.md").read_text(), {},
                {p.name: p.read_text() for p in case_path.glob("test_*.py")})
    if (digest(case.spec) != previous["spec_digest"]
            or {k: digest(v) for k, v in case.acceptance.items()}
            != previous["acceptance_digests"]):
        clients.close()
        raise ValueError("Specification or acceptance changed since the trial")
    started = time.monotonic()
    orch.call = _Admission(call_cap, available,
                           deadline=started + time_cap - active).wrap(
        clients.call, orch, Store(output / "trial.sqlite"), trial)
    if repair:
        for task in orch.state.tasks(run_id):
            if task.status == "PARKED":
                orch.state.retry(run_id, task.id)
    timer = threading.Timer(time_cap - active, orch.cancel.set)
    timer.start()
    resume_phases = {}
    try:
        if planning:
            orch.resume_plan(run_id)  # exact receipts reused; unknown calls cannot be replayed
            resume_phases["plan_s"] = time.monotonic() - started
        executing = time.monotonic()
        run = orch.execute(run_id, jobs=previous["jobs"], mutants=0,
                           max_cents=previous["max_cents"])
        resume_phases["execute_s"] = time.monotonic() - executing
        trial["result_revision"] = run.verified_head
        accepting = time.monotonic()
        passed, log = _accept(orch, trial, case, LiveWorkspace.run_unittests)
        resume_phases["heldout_s"] = time.monotonic() - accepting
        (output / "acceptance.log").write_text(log)
        previous.update(status="failed" if run.status == "done" and not passed else run.status,
                        scheduler_status=run.status, acceptance_passed=passed, run=asdict(run))
        previous.pop("error", None)
        previous.pop("errors", None)
    except Exception as exc:
        previous.update(status="error", error=f"{type(exc).__name__}: {exc}",
                        errors=getattr(exc, "errors", []))
    finally:
        timer.cancel()
        calls, events = orch.state.usage.calls(run_id), orch.state.events(run_id)
        previous.update(usage=report(calls, "model"), tasks_usage=report(calls, "task")["groups"],
                        tasks=[asdict(t) for t in orch.state.tasks(run_id)],
                        calls_after_first_attempt=summary([c for c in calls if c.attempt > 1]),
                        event_counts=dict(Counter(e.kind for e in events)),
                        scheduler_metrics=[e.data for e in events if e.kind == "run.metrics"],
                        unresolved_call_ids=[c.id for c in calls if unresolved(c.outcome, c.usage)],
                        budget_resume=True, resume_call_cap=call_cap, active_time_cap_s=time_cap,
                        active_wall_s=active + time.monotonic() - started,
                        wall_s=time.time() - started_at,
                        resume_engine_revision=Workspace.git(ROOT, "rev-parse", "HEAD"))
        previous.setdefault("resume_history", []).append({
            "engine": engine, "kind": "planning" if planning else "repair" if repair else "budget",
            "prior_status": expected, "active_before_s": active, "phases": resume_phases})
        seen, repeated = set(), []
        for call in calls:
            key = (call.provider, call.model, call.prompt_hash)
            if call.prompt_hash and key in seen:
                repeated.append(call)
            seen.add(key)
        previous["identical_request_repeats"] = summary(repeated)
        planning_dir = orch.run_dir(run_id) / "planning"
        if planning_dir.is_dir():
            shutil.copytree(planning_dir, output / "planning", dirs_exist_ok=True)
        (output / "events.json").write_text(dumps([asdict(e) for e in events]) + "\n")
        try:
            revision = orch.state.run(run_id).verified_head
        except KeyError:
            revision = previous["evaluation_base"]
        (output / "result.patch").write_text(Workspace.git(
            orch.repo, "diff", previous["evaluation_base"], revision, "--", strip=False))
        path.write_text(dumps(previous) + "\n")
        orch.release_worktrees(run_id)
        clients.close()
        print(dumps({"status": previous["status"], "usage": summary(calls)}), flush=True)


def recheck_single(output: Path) -> None:
    """Re-run identical artifact gates after an evaluator correction; no model access."""
    path = output / "report.json"
    raw = path.read_text()
    result = json.loads(raw)
    if not result.get("single_agent"):
        raise ValueError("Recheck is for the native single-agent artifact")
    backup = output / "report-before-protected-scope-fix.json"
    if not backup.exists():
        backup.write_text(raw)
    repo = output / "project"
    if Workspace.git(repo, "status", "--porcelain"):
        raise ValueError("Recheck requires the exact clean committed artifact")
    case_path = ROOT / "examples" / "orch-bench" / result["case"]
    case = Case(result["case"], (case_path / "spec.md").read_text(), {},
                {p.name: p.read_text() for p in case_path.glob("test_*.py")})
    if {k: digest(v) for k, v in case.acceptance.items()} != result["acceptance_digests"]:
        raise ValueError("Acceptance changed since the trial")
    environment = PytestEnvironment.load(output / "environment")
    orch = Orchestrator(repo, lambda *a, **kw: NativeResult("policy_blocked", {}),
                        LiveWorkspace.run_unittests, environment=environment)
    trial = {"run_id": "single-recheck",
             "result_revision": Workspace.git(repo, "rev-parse", "HEAD")}
    try:
        passed, log = orch._project_tests(trial["run_id"], trial["result_revision"], environment)
        (output / "regression-recheck.log").write_text(log)
        accepted, log = _accept(orch, trial, case, LiveWorkspace.run_unittests)
        (output / "acceptance-recheck.log").write_text(log)
        protected = protected_changes(repo, result["evaluation_base"])
        result.update(project_tests_passed=passed, acceptance_passed=accepted,
                      modified_protected_files=protected, result_revision=trial["result_revision"],
                      evaluator_revision=Workspace.git(ROOT, "rev-parse", "HEAD"),
                      status="done" if passed and accepted and not protected
                      and result["agent_outcome"] == "ok" else "failed")
        path.write_text(dumps(result) + "\n")
        print(dumps({k: result[k] for k in ("status", "acceptance_passed", "result_revision")}))
    finally:
        orch.release_worktrees(trial["run_id"])


def agent_test(output: Path, nodeids: list[str] | None = None) -> None:
    """The native agent's pinned sandbox test command with optional validated nodeids."""
    manifest = json.loads((output / "manifest.json").read_text())
    (output / "agent-scratch").mkdir(exist_ok=True)
    execution = output / "project"
    passed, log = LiveWorkspace.run_pytest(
        execution, output / "agent-scratch", manifest["evaluation_base"], nodeids,
        environment=PytestEnvironment.load(output / "environment"),
    )
    (output / f"agent-test-{uuid.uuid4().hex}.json").write_text(
        dumps({"passed": passed, "log": log, "nodeids": nodeids}) + "\n")
    print(log)
    raise SystemExit(0 if passed else 1)


SINGLE_AGENT_OUTPUT_LIMIT = 512 * 1024 * 1024


def single_agent(orch, clients, case, output, base, trial, result, phases, timeout,
                 effort="high"):
    """One native subscription agent with real file tools and a fixed sandbox test command."""
    passed, log = orch._project_tests("single-opus", base, orch.environment_override)
    (output / "baseline.log").write_text(log)
    if not passed:
        raise ValueError("Single-agent baseline failed")
    if clients._status("claude", lambda: False).get("ready") is not True:
        raise ValueError("Confirmed Claude subscription required; no fallback")
    command = " ".join(shlex.quote(str(v)) for v in (
        "env", f"PYTHONPATH={ROOT / 'src'}", sys.executable, Path(__file__).resolve(),
        "--agent-test", output,
    ))
    prompt = (case.spec + "\n\nYou are the sole implementation agent. Work in this checkout. "
              "Read existing code, implement the feature, and run tests before finishing. "
              "Do not delegate, access network, commit, or change existing tests/configuration. "
              "Operate only inside this checkout; do not inspect other evaluation artifacts "
              "or try to locate held-out tests. Do not add collection or packaging controls. "
              "The controller runs additional held-out acceptance afterwards. Your Bash capability "
              "is the following sandbox test helper. With no arguments it runs all project "
              "pytest tests; append one or more repository-relative pytest nodeids to run a "
              "file, class or method (for example test_feature.py::TestFeature::test_case). "
              "Use selective tests for iteration and a full run before finishing. The controller "
              "independently runs the complete project gate afterwards:\n" + command)
    schema = object_schema({"summary": TEXT})
    argv = [str(clients.executables["claude"]), "--safe-mode", "--restricted",
            "--tools", "Read,Edit,Write,Glob,Grep,Bash", "--allowedTools",
            "Read,Edit,Write,Glob,Grep", f"Bash({command} *)",
            "--strict-mcp-config", "--mcp-config", dumps({"mcpServers": {}}),
            "--permission-prompts", "none", "--permission-mode", "dontAsk",
            "--no-session-persistence", "--print", "--verbose", "--output-format", "stream-json",
            "--json-schema", dumps(schema), "--model", "claude-opus-5-5", "--effort", effort,
            "--disable-slash-commands", "--no-chrome"]
    trial["run_id"] = "single-opus"
    request = dumps({"prompt": prompt, "schema": schema})
    receipt = Call("single-opus-" + uuid.uuid4().hex[:12], "single-opus", "claude",
                   "claude-opus-5-5", "single-agent", time.time(), scope="baseline",
                   effort=effort, prompt_hash=digest(request), request_bytes=len(request.encode()),
                   base_revision=base)
    started = time.monotonic()
    print("[single-agent] Opus reads, edits and tests the same repository", flush=True)
    # A long native session streams every tool input and result; the 2 MB default would
    # kill it mid-run and lose its usage, which is unfair to the baseline being measured.
    clients.output_limit = SINGLE_AGENT_OUTPUT_LIMIT
    with orch.state.usage.capture(receipt):
        outcome, code, stdout, stderr = clients._run(
            argv, orch.repo, prompt, timeout, orch.cancel.is_set)
        (output / "single-agent-stream.jsonl").write_bytes(stdout)
        result["single_agent_stream_bytes"] = len(stdout) + len(stderr)
        native = (clients._decode("claude", code, stdout, stderr) if outcome == "ok"
                  else NativeResult(outcome, {}, message="Native single agent did not finish"))
        receipt.outcome, receipt.usage, receipt.session_id = (
            native.outcome, native.usage, native.session_id)
    phases["single_agent_s"] = time.monotonic() - started
    tools_used = Counter()
    messages = set()
    tool_errors = 0
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message", {})
        if not isinstance(message, dict):
            continue
        if event.get("type") == "assistant" and message.get("id"):
            messages.add(message["id"])
        for block in message.get("content", []) if isinstance(message, dict) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tools_used[block.get("name", "unknown")] += 1
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_errors += bool(block.get("is_error"))
    result.update(agent_outcome=native.outcome, agent_tools=dict(tools_used),
                  observed_assistant_messages=len(messages), tool_errors=tool_errors)
    protected = protected_changes(orch.repo, base)
    result["modified_protected_files"] = protected
    Workspace.git(orch.repo, "add", "-A")
    Workspace.git(orch.repo, "commit", "--allow-empty", "-m", "bench: single agent result")
    trial["result_revision"] = Workspace.git(orch.repo, "rev-parse", "HEAD")
    passed, log = orch._project_tests("single-opus", trial["result_revision"],
                                    orch.environment_override)
    (output / "regression.log").write_text(log)
    accepted, log = _accept(orch, trial, case, LiveWorkspace.run_unittests)
    (output / "acceptance.log").write_text(log)
    result.update(project_tests_passed=passed, acceptance_passed=accepted,
                  status="done" if native.outcome == "ok" and passed and accepted and not protected
                  else "failed")


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--agent-test":
        agent_test(Path(sys.argv[2]).resolve(), sys.argv[3:] or None)
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--recheck-single":
        recheck_single(Path(sys.argv[2]).resolve())
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--resume-trial":
        resume_trial(Path(sys.argv[2]).resolve())
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--repair-trial":
        resume_trial(Path(sys.argv[2]).resolve(), repair=True)
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--resume-planning":
        resume_trial(Path(sys.argv[2]).resolve(), planning=True)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-path", default=".",
                        help="repository-relative library subtree for source-size reporting")
    parser.add_argument("--sandbox-overlay", type=Path,
                        help="reuse an already tracked repository-specific sandbox overlay")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--distributed", action="store_true")
    parser.add_argument("--reviewed-boundary", action="store_true",
                        help="experimental DSF-draft/Opus-review boundary; the formal "
                        "distributed path is Opus 5.5/high boundary planning with DSF tests")
    parser.add_argument("--integration-checks", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--single-agent", action="store_true")
    parser.add_argument("--single-effort", choices=("low", "medium", "high", "max"),
                        default="high", help="native single Opus reasoning effort (baseline)")
    parser.add_argument("--test-jobs", type=int, default=None,
                        help="test-design concurrency (default: orch's own default)")
    parser.add_argument("--cold", action="store_true",
                        help="prefix the spec with a fresh nonce so no provider prompt cache "
                             "from an earlier run can hit (within-run sharing still works)")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--max-calls", type=int, default=24)
    parser.add_argument("--max-cents", type=int, default=1200,
                        help="run reservation cap in fen; 0 sets no fixed cap while account "
                        "balance admission, call limits and unknown-call stops still apply")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--wall-limit", type=int, default=1200)
    args = parser.parse_args()
    if Workspace.git(ROOT, "status", "--porcelain", "--", str(Path(__file__)),
                     str(ROOT / "src/orchestragent"), str(args.case.resolve())):
        parser.error("Commit the probe, engine and case before measurement")
    if args.single_agent and (not args.live or args.distributed):
        parser.error("--single-agent requires --live and cannot use --distributed")
    if args.reviewed_boundary and not args.distributed:
        parser.error("--reviewed-boundary requires --distributed")
    if (len(args.revision) != 40 or any(c not in "0123456789abcdef" for c in args.revision)
            or min(args.jobs, args.max_calls, args.timeout, args.wall_limit) <= 0
            or args.max_cents < 0):
        parser.error("Full commit SHA, positive limits and a non-negative --max-cents required")
    source, output = args.source.resolve(), args.output.resolve()
    if Workspace.git(source, "rev-parse", "HEAD") != args.revision:
        parser.error("Source HEAD must match pinned revision")
    if Workspace.git(source, "status", "--porcelain"):
        parser.error("Source must be clean")
    reporting = source / args.source_path
    if (args.source_path != "." and not LiveWorkspace.safe_path(
            args.source_path + "/__source__.py") or not reporting.is_dir()
            or reporting.is_symlink() or not reporting.resolve().is_relative_to(source)):
        parser.error("Source reporting path must be an existing repository directory")
    output.mkdir(parents=True, exist_ok=False)
    repo = output / "project"
    Workspace.git(output, "clone", "--no-hardlinks", "--local", str(source), str(repo))
    Workspace.git(repo, "checkout", "--detach", args.revision)
    overlay = args.sandbox_overlay or args.case / "sandbox_conftest.py"
    base = args.revision
    if overlay.exists():
        if (repo / "conftest.py").exists():
            raise ValueError("Refuse to replace upstream root conftest.py")
        (repo / "conftest.py").write_text(overlay.read_text())
        Workspace.git(repo, "add", "conftest.py")
        Workspace.git(repo, "commit", "-m", "bench: declare unsupported sandbox tests")
        base = Workspace.git(repo, "rev-parse", "HEAD")
    spec = (args.case / "spec.md").read_text()
    nonce = uuid.uuid4().hex if args.cold else ""
    if nonce:
        spec = f"<!-- benchmark run nonce {nonce}: ignore -->\n" + spec
    canary = args.case / "sandbox_canary.py"
    acceptance = {p.name: p.read_text() for p in sorted(args.case.glob("test_*.py"))}
    if not acceptance:
        raise ValueError("At least one held-out acceptance file required")
    case = Case(args.case.name, spec, {}, acceptance)
    environment = PytestEnvironment.create(output / "environment")
    prefix = "" if args.source_path == "." else args.source_path.rstrip("/") + "/"
    sources = [repo / path for path in Workspace.git(
        repo, "ls-tree", "-r", "--name-only", base).splitlines()
        if path.startswith(prefix) and path.endswith(".py")
        and not LiveWorkspace.is_test_file(path) and not (repo / path).is_symlink()]
    result = {
        "schema_version": 1, "source_revision": args.revision,
        "evaluation_base": base,
        "evaluation_tree": Workspace.git(repo, "rev-parse", f"{base}^{{tree}}"),
        "sandbox_overlay_digest": digest(overlay.read_text()) if overlay.exists() else None,
        "sandbox_canary_digest": digest(canary.read_text()) if canary.exists() else None,
        "engine_revision": Workspace.git(ROOT, "rev-parse", "HEAD"),
        "case": case.id, "live": args.live, "jobs": args.jobs,
        "distributed": args.distributed,
        "reviewed_boundary": args.reviewed_boundary,
        "integration_checks": args.distributed and args.integration_checks,
        "single_agent": args.single_agent,
        "single_effort": args.single_effort if args.single_agent else None,
        "max_calls": args.max_calls, "max_cents": args.max_cents,
        "dsf_context_window_tokens": DshClient.context_window_tokens,
        "dsf_transport_payload_limit_bytes": DshClient.input_limit,
        "provider_timeout_base_s": args.timeout, "spec_digest": digest(spec),
        "cold_cache_nonce": nonce,
        "active_time_cap_s": args.wall_limit,
        "acceptance_digests": {k: digest(v) for k, v in acceptance.items()},
        "environment_fingerprint": environment.fingerprint,
        "source_files": len(sources),
        "source_path": args.source_path,
        "project_test_wall_limit_s": PROJECT_TEST_TIMEOUT,
        "project_test_cpu_limit_s": PROJECT_CPU_SECONDS,
        "source_lines": sum(len(p.read_text().splitlines()) for p in sources),
    }
    reference_sources = sorted((args.case / "reference").rglob("*.py"))
    if (args.case / "reference-edits.json").exists():
        reference_sources.append(args.case / "reference-edits.json")
    if any(p.is_symlink() for p in reference_sources):
        raise ValueError("Reference sources cannot be symlinks")
    result["reference_digests"] = {
        p.relative_to(args.case).as_posix(): digest(p.read_text()) for p in reference_sources}
    (output / "manifest.json").write_text(dumps(result) + "\n")
    clients = None
    orch = Orchestrator(repo, lambda *a, **kw: NativeResult("policy_blocked", {}),
                        LiveWorkspace.run_unittests, environment=environment,
                        notify=lambda line: print(line, flush=True))
    trial = {"id": case.id, "run_id": "preflight", "result_revision": base}
    started = time.monotonic()
    phases = {}
    timer = threading.Timer(args.wall_limit, orch.cancel.set)
    timer.start()
    try:
        if reference_sources:
            passed, log, revision = positive_control(
                args.case, output, repo, base, environment, case)
            (output / "positive_acceptance.log").write_text(log)
            result.update(positive_acceptance_passed=passed, positive_reference_revision=revision)
            if not passed:
                raise ValueError("Oracle positive control failed; provider dispatch refused")
        if canary.exists():
            capability_scratch = output / "capability-scratch"
            capability_scratch.mkdir()
            with tempfile.TemporaryDirectory(dir=output) as directory:
                runtime = Path(directory)
                (runtime / "test_capability.py").write_text(canary.read_text())
                passed, log = LiveWorkspace.run_unittests(
                    runtime, capability_scratch, base, ["test_capability"], timeout=5)
            (output / "capability.log").write_text(log)
            result["sandbox_capability_passed"] = passed
            if not passed:
                raise ValueError("Required project sandbox capability is unavailable")
        if not args.live:
            passed, log = orch._project_tests("preflight", base, environment)
            (output / "baseline.log").write_text(log)
            phases["baseline_s"] = time.monotonic() - started
            result["baseline_passed"] = passed
            negative, log = _accept(orch, trial, case, LiveWorkspace.run_unittests)
            (output / "base_acceptance.log").write_text(log)
            result["base_acceptance_passed"] = negative
            result["status"] = "preflight_passed" if passed and not negative else "failed"
        else:
            PolicyStore(OrchState(orch_home(ROOT) / "state.sqlite")).observe_spec(spec)
            os.environ["ORCHESTRA_NATIVE_TIMEOUT"] = str(args.timeout)
            clients = providers(ROOT / ".data")
            if args.single_agent:
                single_agent(orch, clients, case, output, base, trial, result, phases, args.timeout,
                             effort=args.single_effort)
                return
            admission = _Admission(args.max_calls, args.max_cents,
                                   deadline=started + args.wall_limit)
            store = Store(output / "trial.sqlite")
            with store.transaction() as db:
                db.execute("CREATE TABLE bench_trials(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
                db.execute("INSERT INTO bench_trials VALUES (?,?)", (trial["id"], dumps(trial)))
            orch.call = admission.wrap(clients.call, orch, store, trial)
            run = orch.plan(spec, base=base, attempts=2, distributed=args.distributed,
                            test_jobs=args.test_jobs, reviewed_boundary=args.reviewed_boundary,
                            integration_checks=args.integration_checks)
            phases["plan_including_baseline_s"] = time.monotonic() - started
            planned = time.monotonic()
            run = orch.execute(run.id, jobs=args.jobs, mutants=0, max_cents=args.max_cents)
            phases["execute_s"] = time.monotonic() - planned
            trial.update(run_id=run.id, result_revision=run.verified_head or base)
            accepting = time.monotonic()
            passed, log = _accept(orch, trial, case, LiveWorkspace.run_unittests)
            phases["heldout_s"] = time.monotonic() - accepting
            (output / "acceptance.log").write_text(log)
            result.update(status="failed" if run.status == "done" and not passed else run.status,
                          scheduler_status=run.status, acceptance_passed=passed, run=asdict(run))
    except Exception as exc:
        run_ids = {call.run_id for call in orch.state.usage.calls()}
        if trial["run_id"] == "preflight" and len(run_ids) == 1:
            trial["run_id"] = run_ids.pop()  # retain failed planning evidence, too
        result.update(status="error", error=f"{type(exc).__name__}: {exc}",
                      errors=getattr(exc, "errors", []))
        print(result["error"], flush=True)
    finally:
        timer.cancel()
        result["wall_s"] = time.monotonic() - started
        result["phases"] = phases
        calls = orch.state.usage.calls()
        if args.distributed and trial["run_id"] != "preflight":
            session = asdict(orch.planning.session(trial["run_id"]))
            result["integration_scope"] = session["integration_scope"]
            result["planning_session"] = {k: v for k, v in session.items() if k != "spec"}
        events = orch.state.events(trial["run_id"])
        for kept in ("planning", "responses"):  # responses: settled answers DSF could not use
            source = orch.run_dir(trial["run_id"]) / kept
            if source.is_dir():
                shutil.copytree(source, output / kept, dirs_exist_ok=True)
        seen = set()
        repeats = []
        for call in calls:
            key = (call.provider, call.model, call.prompt_hash)
            if call.prompt_hash and key in seen:
                repeats.append(call)
            seen.add(key)
        result["usage"] = report(calls, "model")
        result["unresolved_call_ids"] = [c.id for c in calls if unresolved(c.outcome, c.usage)]
        result["tasks_usage"] = report(calls, "task")["groups"]
        result["calls_after_first_attempt"] = summary([c for c in calls if c.attempt > 1])
        result["identical_request_repeats"] = summary(repeats)
        result["event_counts"] = dict(Counter(e.kind for e in events))
        result["scheduler_metrics"] = [e.data for e in events if e.kind == "run.metrics"]
        result["tasks"] = [asdict(t) for t in orch.state.tasks(trial["run_id"])]
        (output / "events.json").write_text(dumps([asdict(e) for e in events]) + "\n")
        revision = trial["result_revision"]
        result["result_revision"] = revision  # identify the exact artifact even on early failure
        (output / "result.patch").write_text(Workspace.git(
            repo, "diff", base, revision, "--", strip=False))
        (output / "report.json").write_text(dumps(result) + "\n")
        orch.release_worktrees(trial["run_id"])
        if clients is not None:
            clients.close()
        print(dumps({"status": result["status"], "wall_s": result["wall_s"],
                     "usage": summary(calls), "output": str(output)}), flush=True)


if __name__ == "__main__":
    main()
