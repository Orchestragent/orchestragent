from dataclasses import replace
from unittest.mock import Mock

import pytest

from orchestragent.adapters import (
    ClaudeCodeAdapter,
    CodexAdapter,
    MockAdapter,
    ProviderDisabledError,
    StepContext,
)


@pytest.fixture
def context() -> StepContext:
    return StepContext("task-1", "ignored mock prompt", "implement", "happy", 1, "worker-1", "", "")


@pytest.fixture
def adapter() -> MockAdapter:
    return MockAdapter()


@pytest.mark.parametrize(
    ("scenario", "attempt", "approved", "outcome", "retry_after"),
    [
        ("happy", 1, False, "ok", 0.0),
        ("approval", 1, False, "waiting_approval", 0.0),
        ("approval", 1, True, "ok", 0.0),
        ("rate_limit", 1, False, "rate_limited", 1.0),
        ("rate_limit", 2, False, "ok", 0.0),
        ("quota", 1, False, "quota_exhausted", 0.0),
        ("quota", 2, False, "quota_exhausted", 0.0),
        ("auth", 1, False, "auth_required", 0.0),
        ("timeout", 1, False, "timed_out", 0.0),
        ("failure", 1, False, "failed", 0.0),
    ],
)
def test_fault_outcomes(
    adapter: MockAdapter, context: StepContext, scenario: str, attempt: int,
    approved: bool, outcome: str, retry_after: float,
) -> None:
    result = adapter.run(replace(context, scenario=scenario, attempt=attempt, approved=approved))
    assert (result.outcome, result.retry_after, result.cost_usd) == (outcome, retry_after, 0.0)


@pytest.mark.parametrize("stage", ["plan", "test", "summarize"])
@pytest.mark.parametrize("scenario", MockAdapter.scenarios)
def test_faults_do_not_escape_their_stage(
    adapter: MockAdapter, context: StepContext, stage: str, scenario: str,
) -> None:
    assert adapter.run(replace(context, stage=stage, scenario=scenario)).outcome == "ok"


@pytest.mark.parametrize(
    ("scenario", "revision", "artifact", "outcome"),
    [
        ("happy", "abc123", MockAdapter.artifact, "ok"),
        ("happy", "", MockAdapter.artifact, "failed"),
        ("happy", "abc123", MockAdapter.faulty_artifact, "failed"),
        ("happy", "abc123", "", "failed"),
        ("review_failure", "abc123", MockAdapter.artifact, "failed"),
        ("test_failure", "abc123", MockAdapter.faulty_artifact, "ok"),
        ("test_failure", "abc123", "arbitrary code", "failed"),
    ],
)
def test_review_is_independent_and_pinned(
    adapter: MockAdapter, context: StepContext, scenario: str, revision: str,
    artifact: str, outcome: str,
) -> None:
    result = adapter.run(replace(
        context, stage="review", scenario=scenario, revision=revision, artifact=artifact,
    ))
    assert (result.outcome, result.session_id != context.session_id) == (outcome, True)


def test_test_failure_explicitly_defers_correctness(
    adapter: MockAdapter, context: StepContext,
) -> None:
    artifact = adapter.run(replace(context, scenario="test_failure")).artifact
    result = adapter.run(replace(
        context, stage="review", scenario="test_failure", revision="fixed", artifact=artifact,
    ))
    assert "functional correctness is deferred to actual tests" in result.summary


def test_mock_is_deterministic_and_does_not_interpret_prompt(
    adapter: MockAdapter, context: StepContext,
) -> None:
    assert adapter.run(context) == adapter.run(replace(context, prompt="delete everything"))


@pytest.mark.parametrize("provider", [CodexAdapter, ClaudeCodeAdapter])
def test_real_providers_refuse_before_io(
    provider: type[CodexAdapter] | type[ClaudeCodeAdapter],
    context: StepContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden = Mock(side_effect=AssertionError("real provider I/O attempted"))
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)
    with pytest.raises(ProviderDisabledError, match="disabled"):
        provider().run(context)
    forbidden.assert_not_called()


@pytest.mark.parametrize(("stage", "scenario"), [("unknown", "happy"), ("plan", "unknown")])
def test_unknown_inputs_fail_closed(
    adapter: MockAdapter, context: StepContext, stage: str, scenario: str,
) -> None:
    assert adapter.run(replace(context, stage=stage, scenario=scenario)).outcome == "failed"
