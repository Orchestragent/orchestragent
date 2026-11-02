"""Recheck one saved architecture review; not a project trial or a planning restart.

Default is offline. --live permits one subscription Opus review, no DSF or fallback.
Commit this script before execution; preserve the original trial and its receipts.
"""

import argparse
import json
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from orchestragent.envelope import digest
from orchestragent.live_workspace import LiveWorkspace
from orchestragent.native import NativeResult
from orchestragent.orch_bench import _Admission
from orchestragent.orch_cli import providers
from orchestragent.orch_environment import PytestEnvironment
from orchestragent.orch_plan import coupling_text
from orchestragent.orch_planning import PlanningSession
from orchestragent.orch_run import CLAUDE_PLAN, Orchestrator, orch_home
from orchestragent.orch_thin import (
    BOUNDARY_REVIEW_SCHEMA,
    boundary_errors,
    boundary_review_prompt,
    canonical_boundary,
    review_verdict,
)
from orchestragent.orch_usage import report
from orchestragent.serialization import dumps
from orchestragent.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if Workspace.git(ROOT, "status", "--porcelain"):
        parser.error("Commit evaluator before measurement")
    original, output = args.trial.resolve(), args.output.resolve()
    saved = json.loads((original / "report.json").read_text())
    drafts = [c for c in saved["usage"]["calls"]
              if c["purpose"] == "boundary-draft" and c["outcome"] == "ok"]
    call = max(drafts, key=lambda c: c["attempt"])
    source = original / "project"
    base = saved["evaluation_base"]
    if (Workspace.git(source, "status", "--porcelain")
            or Workspace.git(source, "rev-parse", "HEAD") != base):
        parser.error("Require the original clean planning-only source tree")
    with sqlite3.connect(f"file:{orch_home(source) / 'state.sqlite'}?mode=ro", uri=True) as db:
        raw = db.execute("SELECT response FROM orch_planning_calls WHERE run_id=? AND call_id=?",
                         (call["run_id"], call["id"])).fetchone()[0]
        session = PlanningSession(**json.loads(db.execute(
            "SELECT body FROM orch_planning_sessions WHERE id=?", (call["run_id"],)
        ).fetchone()[0]))
    if (session.planner != CLAUDE_PLAN.label or session.effort != CLAUDE_PLAN.effort
            or session.policy_id or session.strategy != "opus-dsf"):
        parser.error("Probe supports only the confirmed unmodified Opus subscription route")
    boundary = canonical_boundary(json.loads(raw)["data"])
    case_path = ROOT / "examples" / "orch-bench" / saved["case"]
    spec = (case_path / "spec.md").read_text()
    if digest(spec) != saved["spec_digest"] or output.is_relative_to(original):
        parser.error("Preserve the original specification and trial directory")
    existing = frozenset(Workspace.git(source, "ls-tree", "-r", "--name-only", base).splitlines())
    errors = boundary_errors(boundary, existing, existing_edits=True)
    if errors:
        parser.error("Saved boundary no longer passes structural checks: " + dumps(errors))
    output.mkdir(parents=True, exist_ok=False)
    repo = output / "project"
    Workspace.git(output, "clone", "--no-hardlinks", "--local", str(source), str(repo))
    Workspace.git(repo, "checkout", "--detach", base)
    environment = PytestEnvironment.load(original / "environment")
    orch = Orchestrator(repo, lambda *a, **kw: NativeResult("policy_blocked", {}),
                        LiveWorkspace.run_unittests,
                        environment=environment)
    orch.bind_environment(str(environment.root), environment.fingerprint)
    run_id = "probe-" + uuid.uuid4().hex[:12]
    orch.planning.create(replace(session, id=run_id))
    orch._durable_planning.add(run_id)
    trial = {"id": "boundary-review-probe", "run_id": run_id}
    with orch.state.store.transaction() as db:
        db.execute("CREATE TABLE bench_trials(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        db.execute("INSERT INTO bench_trials VALUES (?,?)", (trial["id"], dumps(trial)))
    prompt = boundary_review_prompt(spec, orch.index.at(base).planning_map(spec), boundary,
                                    coupling_text(orch.index.cochange(base)))
    (output / "boundary.json").write_text(dumps(boundary) + "\n")
    result = {
        "assessment": "targeted_boundary_review", "live": args.live,
        "engine_revision": Workspace.git(ROOT, "rev-parse", "HEAD"),
        "original_report_digest": digest((original / "report.json").read_text()),
        "original_draft_call_id": call["id"], "original_engine_revision": saved["engine_revision"],
        "boundary_digest": digest(dumps(boundary)), "base_revision": base,
        "spec_digest": digest(spec), "environment_fingerprint": environment.fingerprint,
        "model_call_cap": 1, "api_spend_authorized": False, "project_trial": False,
        "status": "prepared",
    }
    clients = None
    started = time.monotonic()
    try:
        if args.live:
            os.environ["ORCHESTRA_NATIVE_TIMEOUT"] = "180"
            clients = providers(ROOT / ".data")
            orch.call = _Admission(1, 0, deadline=started + 180).wrap(
                clients.call, orch, orch.state.store, trial)
            response = orch._call_model(
                run_id, CLAUDE_PLAN, prompt, BOUNDARY_REVIEW_SCHEMA, run_id,
                purpose="boundary-review", attempt=1, scope="planning", base=base)
            result.update(status=response.outcome, response=asdict(response))
            if response.outcome == "ok":
                result["issues"] = review_verdict(response.data)
    except Exception as error:
        result.update(status="error", error=type(error).__name__)
        raise
    finally:
        result.update(wall_s=time.monotonic() - started,
                      usage=report(orch.state.usage.calls(run_id), "model"))
        (output / "report.json").write_text(dumps(result) + "\n")
        if clients is not None:
            clients.close()
    print(dumps({k: result[k] for k in ("status", "wall_s", "usage")}))


if __name__ == "__main__":
    main()
