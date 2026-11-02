import pytest

from orchestragent.orch_audit import audit
from orchestragent.orch_environment import PytestEnvironment
from orchestragent.orch_run import Orchestrator
from orchestragent.orch_state import Run


@pytest.mark.parametrize("condition", ["passed", "failed", "unavailable", "missing", "stale"])
def test_audit_requires_project_gate_evidence_and_rechecks_pinned_environment(
    orchestrator: Orchestrator, finished: Run, dependency_environment: PytestEnvironment,
    monkeypatch: pytest.MonkeyPatch, condition: str,
) -> None:
    fingerprint = dependency_environment.fingerprint
    root = dependency_environment.root
    orchestrator.state.update_run(finished.id, "test.environment", {},
                                  environment_root=str(root), environment_fingerprint=fingerprint)
    for kind, revision in (("plan.project_baseline", finished.base),
                           ("run.project_final", finished.verified_head)):
        if condition != "missing":
            orchestrator.state.record(finished.id, kind, {
                "passed": True, "revision": revision,
                "environment_fingerprint": "other" if condition == "stale" else fingerprint,
            })

    def project_gate(run_id: str, revision: str,
                     environment: PytestEnvironment) -> tuple[bool, str]:
        assert (run_id, revision, environment) == (
            finished.id, finished.verified_head, PytestEnvironment(root, fingerprint))
        if condition == "unavailable":
            raise ValueError("bundle changed")
        return condition != "failed", "project gate failure evidence"

    monkeypatch.setattr(orchestrator, "_project_tests", project_gate)
    findings = audit(orchestrator, finished.id)
    assert bool(findings) is (condition != "passed")
    assert all("project" in finding for finding in findings)
