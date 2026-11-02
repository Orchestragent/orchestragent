"""Post-hoc acceptance repair on exact saved artifacts; never construct provider clients.

Commit evaluator and corrected case before running. Original reports remain immutable;
this output is not a new blind trial or a model rerun. Reuse the controller's sandbox.
"""

import argparse
import json
from pathlib import Path
from typing import Any

from orchestragent.envelope import digest
from orchestragent.live_workspace import LiveWorkspace
from orchestragent.orch_bench import Case, _accept
from orchestragent.orch_environment import PytestEnvironment
from orchestragent.orch_run import Orchestrator
from orchestragent.serialization import dumps
from orchestragent.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


def reassess(output: Path, revision: str, case_path: Path, destination: Path) -> dict[str, Any]:
    """Apply only the corrected oracle to the explicitly named, clean committed artifact."""
    report_path, repo = output / "report.json", output / "project"
    raw = report_path.read_text()
    saved = json.loads(raw)
    actual = Workspace.git(repo, "rev-parse", "HEAD")
    if (revision != actual or saved.get("result_revision", revision) != revision
            or Workspace.git(repo, "status", "--porcelain")):
        raise ValueError("Reassessment requires the exact clean committed artifact")
    if destination.resolve().is_relative_to(output.resolve()):
        raise ValueError("Reassessment output must be separate from the original trial")
    spec = (case_path / "spec.md").read_text()
    if case_path.name != saved["case"] or digest(spec) != saved["spec_digest"]:
        raise ValueError("Reassessment cannot change the original specification")
    acceptance = {p.name: p.read_text() for p in sorted(case_path.glob("test_*.py"))}
    if not acceptance or set(acceptance) != set(saved["acceptance_digests"]):
        raise ValueError("Reassessment must retain the original acceptance file set")
    environment = PytestEnvironment.load(output / "environment")
    if environment.fingerprint != saved["environment_fingerprint"]:
        raise ValueError("Reassessment requires the original pinned environment")

    def forbidden_call(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Offline artifact reassessment cannot call a model")

    destination.mkdir(parents=True, exist_ok=False)
    orch = Orchestrator(repo, forbidden_call, LiveWorkspace.run_unittests, environment=environment)
    trial = {"run_id": "oracle-reassessment", "result_revision": revision}
    try:
        passed, log = _accept(orch, trial, Case(case_path.name, spec, {}, acceptance),
                              LiveWorkspace.run_unittests)
        (destination / "acceptance.log").write_text(log)
    finally:
        orch.release_worktrees(trial["run_id"])
    if report_path.read_text() != raw or Workspace.git(repo, "rev-parse", "HEAD") != actual:
        raise RuntimeError("Original trial or artifact changed during reassessment")
    result = {
        "schema_version": 1, "assessment": "post_hoc_corrected_oracle",
        "original_report": str(report_path), "original_report_digest": digest(raw),
        "original_status": saved["status"], "engine_revision": saved["engine_revision"],
        "evaluator_revision": Workspace.git(ROOT, "rev-parse", "HEAD"),
        "artifact_revision": revision,
        "artifact_tree": Workspace.git(repo, "rev-parse", f"{revision}^{{tree}}"),
        "produced_artifact": revision != saved["evaluation_base"],
        "environment_fingerprint": environment.fingerprint, "spec_digest": digest(spec),
        "original_acceptance_digests": saved["acceptance_digests"],
        "corrected_acceptance_digests": {k: digest(v) for k, v in acceptance.items()},
        "acceptance_passed": passed, "model_calls": 0, "implementation_interventions": 0,
    }
    (destination / "assessment.json").write_text(dumps(result) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if Workspace.git(ROOT, "status", "--porcelain"):
        parser.error("Commit evaluator and case before reassessment")
    print(dumps(reassess(args.trial.resolve(), args.revision, args.case.resolve(),
                        args.output.resolve())))


if __name__ == "__main__":
    main()
