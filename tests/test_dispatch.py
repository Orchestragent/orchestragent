from unittest.mock import Mock

import pytest

from orchestragent.adapters import PROVIDER_IDS, MockAdapter, ProviderDisabledError, StepContext
from orchestragent.dispatch import Dispatcher


@pytest.fixture
def context() -> StepContext:
    return StepContext("task", "test", "implement", "happy", 1, "mock", "", "")


def test_dispatch_returns_shared_result(context: StepContext) -> None:
    assert Dispatcher().run(context) == MockAdapter().run(context)


@pytest.mark.parametrize("provider", PROVIDER_IDS)
def test_every_real_vendor_refuses_without_io_or_fallback(
    provider: str,
    context: StepContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden = Mock(side_effect=AssertionError("unauthorized execution"))
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr(MockAdapter, "run", forbidden)
    with pytest.raises(ProviderDisabledError, match="disabled"):
        Dispatcher().dispatch(provider, context)
    forbidden.assert_not_called()


def test_unknown_route_does_not_fall_back(context: StepContext) -> None:
    with pytest.raises(ValueError, match="Unknown provider"):
        Dispatcher().dispatch("unexpected", context)
