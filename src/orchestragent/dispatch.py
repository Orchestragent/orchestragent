"""One dispatch boundary for all vendors; routing never changes billing implicitly."""

from orchestragent.adapters import (
    PROVIDER_IDS,
    Adapter,
    ClaudeCodeAdapter,
    CodexAdapter,
    MockAdapter,
    ProviderDisabledError,
    StepContext,
    StepResult,
)


class Dispatcher:
    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {
            "mock": MockAdapter(),
            "codex": CodexAdapter(),
            "claude": ClaudeCodeAdapter(),
        }

    def run(self, context: StepContext) -> StepResult:
        """The MVP control plane always dispatches to the zero-cost Mock route."""
        return self.dispatch("mock", context)

    def dispatch(self, provider: str, context: StepContext) -> StepResult:
        if provider in self._adapters:
            return self._adapters[provider].run(context)
        if provider in PROVIDER_IDS:
            raise ProviderDisabledError(f"{provider} is disabled; no fallback was attempted")
        raise ValueError("Unknown provider; no fallback was attempted")
