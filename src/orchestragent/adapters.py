"""Deterministic, zero-cost mock steps and deliberately disabled vendor boundaries."""

from dataclasses import dataclass, field
from typing import Protocol

from orchestragent.models import SCENARIOS, STAGES

PROVIDER_IDS = ("codex", "claude", "dsh", "cursor", "kimi")


@dataclass(frozen=True)
class StepContext:
    task_id: str
    prompt: str
    stage: str
    scenario: str
    attempt: int
    session_id: str
    revision: str
    artifact: str
    approved: bool = False


@dataclass(frozen=True)
class StepResult:
    outcome: str = "ok"
    summary: str = ""
    artifact: str = ""
    session_id: str = ""
    retry_after: float = 0.0
    cost_usd: float | None = 0.0
    provider: str = "mock"
    model: str = ""
    usage: dict[str, object] = field(default_factory=dict)


class Adapter(Protocol):
    def run(self, context: StepContext) -> StepResult: ...


class ProviderDisabledError(RuntimeError):
    """A real-provider action has not been approved or implemented."""


class CodexAdapter:
    """Protocol discovery only; no process, authentication, or network access."""

    enabled = False
    protocol = "Codex app-server JSON-RPC over stdio"
    documented_capabilities = (
        "thread/start and thread/resume",
        "turn/start, streamed items, and approvals",
        "Codex-managed ChatGPT sign-in; separate API-key billing",
        "account/rateLimits/read with provider-classified limits",
    )

    def run(self, context: StepContext) -> StepResult:
        raise ProviderDisabledError(
            "Codex is disabled: approve a concrete native CLI/app-server action and "
            "confirm its entitlement and billing mode before implementation. "
            "No process started and no credentials accessed."
        )


class ClaudeCodeAdapter:
    """Native CLI is the candidate boundary, not subscription-token SDK reuse."""

    enabled = False
    protocol = "Claude Code native CLI print mode with stream-json output"
    documented_capabilities = (
        "noninteractive print mode and streamed output",
        "session resume and explicit tool permissions",
        "native subscription login, subject to credential precedence",
        "Agent SDK subscription reuse is a separate restricted integration",
    )

    def run(self, context: StepContext) -> StepResult:
        raise ProviderDisabledError(
            "Claude Code is disabled: approve a concrete native CLI action and "
            "confirm its entitlement and billing mode before implementation. "
            "No process started and no credentials accessed."
        )


class MockAdapter:
    """A fixed add-function demo; prompts are never executed or interpreted."""

    artifact = "def add(a: int, b: int) -> int:\n    return a + b\n"
    faulty_artifact = "def add(a: int, b: int) -> int:\n    return a - b\n"
    stages = STAGES
    scenarios = SCENARIOS

    def run(self, context: StepContext) -> StepResult:
        session = (
            f"mock-review-{context.task_id}"
            if context.stage == "review"
            else context.session_id or f"mock-worker-{context.task_id}"
        )
        if context.stage not in self.stages or context.scenario not in self.scenarios:
            return StepResult("failed", "Unknown mock stage or scenario.", session_id=session)
        if context.stage == "implement":
            if context.scenario == "approval" and not context.approved:
                return StepResult(
                    "waiting_approval",
                    "Approval required for the displayed mock operation.",
                    session_id=session,
                )
            if context.scenario == "rate_limit" and context.attempt == 1:
                return StepResult(
                    "rate_limited",
                    "Mock transient rate limit; retry after the deadline.",
                    session_id=session,
                    retry_after=1.0,
                )
            faults = {
                "quota": ("quota_exhausted", "Mock quota exhausted; no billing fallback."),
                "auth": ("auth_required", "Mock authentication required; no automatic login."),
                "timeout": ("timed_out", "Mock provider timeout; explicit resume required."),
                "failure": ("failed", "Injected mock provider failure."),
            }
            if context.scenario in faults:
                outcome, summary = faults[context.scenario]
                return StepResult(outcome, summary, session_id=session)
            return StepResult(
                summary="Prepared the deterministic add-function artifact.",
                artifact=(
                    self.faulty_artifact if context.scenario == "test_failure" else self.artifact
                ),
                session_id=session,
            )
        if context.stage == "review":
            if not context.revision:
                return StepResult("failed", "Review requires a fixed revision.", session_id=session)
            expected = self.faulty_artifact if context.scenario == "test_failure" else self.artifact
            if context.artifact != expected:
                return StepResult(
                    "failed", "Review rejected the supplied artifact.", session_id=session
                )
            if context.scenario == "review_failure":
                return StepResult(
                    "failed", "Injected independent review failure.", session_id=session
                )
            scope = (
                "Structural review only; functional correctness is deferred to actual tests."
                if context.scenario == "test_failure"
                else "The supplied artifact matches the expected addition implementation."
            )
            return StepResult(
                summary=f"Reviewed fixed revision {context.revision}. {scope}", session_id=session
            )
        summaries = {
            "plan": "Plan: implement add, independently review a fixed revision, then test it.",
            "test": "The controller must run artifact tests at the reviewed revision.",
            "summarize": "Mock workflow finished; consult the recorded review and test evidence.",
        }
        return StepResult(summary=summaries[context.stage], session_id=session)
