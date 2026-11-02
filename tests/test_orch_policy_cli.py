import json
from dataclasses import replace
from pathlib import Path

import pytest

import orchestragent.orch_cli as orch_cli
from orchestragent.orch_policy import PolicyStore
from orchestragent.orch_run import Orchestrator

BASELINE_RULE = "FROZEN_BASELINE_RULE_SENTINEL: validate the complete planner result."
CANDIDATE_RULE = "UNFROZEN_CANDIDATE_RULE_SENTINEL: revise the planner result."


@pytest.fixture
def cli_policy_setup(orchestrator: Orchestrator) -> tuple[PolicyStore, str, str]:
    registry = orchestrator.policies
    active = registry.state.add_rule(BASELINE_RULE, "offline-test", kind="planner")
    assert active is not None
    registry.state.save_rule(replace(active, status="active"))
    candidate = registry.state.add_rule(CANDIDATE_RULE, "offline-test", kind="planner")
    assert candidate is not None
    return registry, active.id, candidate.id


def test_policy_cli_stages_without_activation_and_refuses_unaccepted_rollback(
    orch_repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    cli_policy_setup: tuple[PolicyStore, str, str],
) -> None:
    registry, baseline_rule_id, candidate_id = cli_policy_setup
    monkeypatch.setattr(orch_cli, "providers", lambda *_: pytest.fail("provider construction"))

    assert orch_cli.main(["--repo", str(orch_repo), "policy", "enable"]) == 0
    baseline = json.loads(capsys.readouterr().out)
    baseline_id = baseline["id"]
    assert [(row["id"], row["text"]) for row in baseline["rules"]] == [
        (baseline_rule_id, BASELINE_RULE),
    ]

    assert orch_cli.main([
        "--repo", str(orch_repo), "policy", "snapshot", "--include", candidate_id,
        "--exclude", baseline["rules"][0]["id"],
    ]) == 0
    staged = json.loads(capsys.readouterr().out)
    assert staged["parent"] == baseline_id
    assert [row["id"] for row in staged["rules"]] == [candidate_id]
    assert staged["rules"][0]["status"] == "active"

    assert orch_cli.main(["--repo", str(orch_repo), "policy", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["active"]["id"] == baseline_id
    assert [row["action"] for row in status["history"]] == ["enabled"]

    assert orch_cli.main(["--repo", str(orch_repo), "policy", "rollback", staged["id"]]) == 1
    assert "never an accepted active policy" in capsys.readouterr().err
    current = registry.current()
    assert current is not None and current.id == baseline_id
    assert orch_cli.main(["--repo", str(orch_repo), "policy", "rollback", baseline_id]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["id"] == baseline_id
    assert [row["action"] for row in registry.history()] == ["enabled", "rolled_back"]
    assert orch_cli.main(["--repo", str(orch_repo), "policy", "show", staged["id"]]) == 0
    assert json.loads(capsys.readouterr().out) == staged
