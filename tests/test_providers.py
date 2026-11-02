from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from orchestragent.access import Access
from orchestragent.native import NativeClients, NativeResult
from orchestragent.providers import ProviderClients, settled_cents
from orchestragent.serialization import dumps
from orchestragent.store import Conflict

SCHEMA: dict[str, object] = {"type": "object"}
FAKE_KEY = "fake-provider-test-key"


def test_native_default_has_no_execution_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ORCHESTRA_NATIVE_TIMEOUT", raising=False)
    client = ProviderClients(tmp_path)
    try:
        assert client.timeout == 0
    finally:
        client.close()


@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
def test_native_timeout_must_be_finite_and_non_negative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    monkeypatch.setenv("ORCHESTRA_NATIVE_TIMEOUT", value)
    with pytest.raises(ValueError):
        ProviderClients(tmp_path)


@pytest.fixture
def clients(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ProviderClients]:
    client = ProviderClients(tmp_path / "providers")
    monkeypatch.setattr(
        NativeClients, "_run", lambda *a, **k: pytest.fail("No native process is allowed")
    )
    monkeypatch.setattr(
        client.dsh, "call", lambda *a, **k: pytest.fail("DSH transport must not be reached")
    )
    yield client
    client.close()


@pytest.fixture
def authorize(clients: ProviderClients) -> Callable[[int], None]:
    def grant(cap_cents: int) -> None:
        operation = clients.access.operation("dsh", cap_cents, "budget")
        clients.access.authorize(
            "dsh", cap_cents, "budget", str(operation["operation_digest"]), FAKE_KEY
        )

    return grant


@pytest.fixture
def verified_request_bound(clients: ProviderClients, monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise reservation arithmetic only; never claim the real DSH loop is bounded.
    monkeypatch.setattr(clients, "dsh_request_bound_verified", True)


@pytest.fixture
def dsh_transport(
    clients: ProviderClients, monkeypatch: pytest.MonkeyPatch, verified_request_bound: None
) -> Mock:
    transport = Mock(
        return_value=NativeResult(
            "ok", {"answer": 42}, "session-test", {"input_tokens": 13, "output_tokens": 7}
        )
    )
    monkeypatch.setattr(clients.dsh, "call", transport)
    return transport


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_subscription_routes_delegate_exact_request_without_api_reservation(
    clients: ProviderClients, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    expected = NativeResult("ok", {"answer": provider}, "native-session")
    native_call = Mock(return_value=expected)
    monkeypatch.setattr(NativeClients, "call", native_call)
    cancelled = Mock(return_value=False)
    result = clients.call(provider, "configured-model", "prompt", SCHEMA, "run", cancelled)
    native_call.assert_called_once_with(
        provider, "configured-model", "prompt", SCHEMA, "run", cancelled, effort=""
    )
    assert result is expected
    assert clients.access.status("dsh")["reserved_cents"] == 0


@pytest.mark.parametrize("provider", ["kimi"])
def test_unavailable_background_routes_never_launch(
    clients: ProviderClients, provider: str
) -> None:
    result = clients.call(provider, "configured-model", "prompt", SCHEMA, "run", lambda: False)
    assert (result.outcome, result.data, clients.access.status("dsh")["reserved_cents"]) == (
        "policy_blocked",
        {},
        0,
    )
    assert result.message


@pytest.mark.parametrize(
    ("has_key", "has_authorization", "expected"),
    [
        (False, False, "auth_required"),
        (False, True, "auth_required"),
        (True, False, "waiting_approval"),
    ],
)
def test_dsh_requires_both_live_credential_and_explicit_budget(
    clients: ProviderClients,
    authorize: Callable[[int], None],
    monkeypatch: pytest.MonkeyPatch,
    has_key: bool,
    has_authorization: bool,
    expected: str,
) -> None:
    if has_authorization:
        authorize(1000)
        clients.access = Access(clients.provider_root)  # A restart retains approval, not the key.
    if has_key:
        monkeypatch.setattr(clients.access, "key", lambda provider: FAKE_KEY)
    result = clients.call("dsh", "deepseek-flash", "prompt", SCHEMA, "run", lambda: False)
    assert (result.outcome, clients.access.status("dsh")["reserved_cents"]) == (expected, 0)
    assert FAKE_KEY not in dumps(asdict(result))


def test_request_bound_is_always_on_and_not_configurable(clients: ProviderClients) -> None:
    # The single-request ceiling is a transport property, not a policy switch.
    assert clients.dsh_request_bound_verified is True
    assert "dsh_request_bound_verified" not in {
        name for name in dir(ProviderClients) if name.startswith("set_")
    }
    assert ProviderClients.__init__.__defaults__ is None


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_a_small_recorded_cap_does_not_block_the_call(
    clients: ProviderClients, authorize: Callable[[int], None], dsh_transport: Mock, model: str,
) -> None:
    # No local spending gate; only the provider's own balance (HTTP 402) stops calls.
    authorize(1)
    result = clients.call("dsh", model, "prompt", SCHEMA, "run", lambda: False)
    assert (result.outcome, dsh_transport.call_count) == ("ok", 1)


def test_a_call_without_a_configured_key_never_reaches_the_transport(
    clients: ProviderClients, verified_request_bound: None,
) -> None:
    result = clients.call("dsh", "deepseek-flash", "prompt", SCHEMA, "run", lambda: False)
    assert result.outcome == "auth_required"


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_cancellation_before_transport_does_not_reserve(
    clients: ProviderClients, authorize: Callable[[int], None], model: str
) -> None:
    authorize(1000)
    result = clients.call("dsh", model, "prompt", SCHEMA, "run", lambda: True)
    assert (result.outcome, clients.access.status("dsh")["reserved_cents"]) == ("cancelled", 0)


@pytest.mark.parametrize(
    "model,reservation,settled", [("deepseek-flash", 300, 1), ("deepseek-v4-pro", 1000, 1000)]
)
def test_authorized_dsh_reserves_before_call_and_reports_distinct_api_usage(
    clients: ProviderClients,
    authorize: Callable[[int], None],
    dsh_transport: Mock,
    model: str,
    reservation: int,
    settled: int,
) -> None:
    authorize(reservation)
    original = cast(NativeResult, dsh_transport.return_value)
    observed_reservations: list[object] = []

    def fake_call(*args: object, **kwargs: object) -> NativeResult:
        observed_reservations.append(clients.access.status("dsh")["reserved_cents"])
        return original

    dsh_transport.side_effect = fake_call
    cancelled = Mock(return_value=False)
    result = clients.call("dsh", model, "prompt", SCHEMA, "run", cancelled)
    dsh_transport.assert_called_once_with(
        model, "prompt", SCHEMA, "run", cancelled, api_key=FAKE_KEY, effort=""
    )
    assert observed_reservations == [reservation]
    assert (result.outcome, result.data, result.session_id) == (
        "ok",
        {"answer": 42},
        "session-test",
    )
    assert result.usage == {
        "input_tokens": 13,
        "output_tokens": 7,
        "billing": "api",
        "currency": "CNY",
        "reserved_cents": reservation,
        "dsf_transport": "cli",
        "settled_cents": settled,
    }
    assert clients.access.status("dsh")["spent_cents"] == settled
    assert original.usage == {"input_tokens": 13, "output_tokens": 7}
    assert FAKE_KEY not in dumps(asdict(result)) + dumps(clients.access.status("dsh"))


@pytest.mark.parametrize("outcome", ["timed_out", "cancelled", "failed", "quota_exhausted"])
def test_uncertain_transport_result_keeps_exposure_and_does_not_retry(
    clients: ProviderClients, authorize: Callable[[int], None], dsh_transport: Mock, outcome: str
) -> None:
    authorize(300)
    dsh_transport.return_value = NativeResult(outcome, {}, message="Stopped")
    result = clients.call("dsh", "deepseek-flash", "prompt", SCHEMA, "run", lambda: False)
    assert (result.outcome, result.message, result.usage["reserved_cents"]) == (
        outcome,
        "Stopped",
        300,
    )
    assert clients.access.status("dsh")["reserved_cents"] == 300
    assert dsh_transport.call_count == 1


def test_replaying_same_attempt_never_reaches_transport_twice(
    clients: ProviderClients, authorize: Callable[[int], None], dsh_transport: Mock
) -> None:
    authorize(1000)
    clients.call("dsh", "deepseek-flash", "prompt", SCHEMA, "same-run", lambda: False)
    with pytest.raises(Conflict):
        clients.call("dsh", "deepseek-flash", "prompt", SCHEMA, "same-run", lambda: False)
    status = clients.access.status("dsh")
    assert (dsh_transport.call_count, status["spent_cents"], status["reserved_cents"]) == (
        1,
        1,
        0,
    )


@pytest.mark.parametrize(
    "provider,model", [("unknown", "model"), ("dsh", "auto"), ("dsh", "other")]
)
def test_unknown_routes_and_models_never_fall_back(
    clients: ProviderClients, provider: str, model: str
) -> None:
    result = clients.call(provider, model, "prompt", SCHEMA, "run", lambda: False)
    assert (result.outcome, clients.access.status("dsh")["reserved_cents"]) == ("failed", 0)


@pytest.mark.parametrize(
    ("model", "usage", "expected"),
    [
        ("deepseek-flash", {"input_tokens": 40_000, "output_tokens": 8_192}, 19),
        (
            "deepseek-flash",
            {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_input_tokens": 0},
            250,
        ),
        ("deepseek-flash", {"input_tokens": 0, "output_tokens": 0}, 1),
        ("deepseek-flash", {"input_tokens": 9_000_000, "output_tokens": 1}, 300),
        ("deepseek-flash", {"output_tokens": 10}, 300),
        ("deepseek-flash", {"input_tokens": -5, "output_tokens": 10}, 300),
        ("deepseek-flash", {"input_tokens": True, "output_tokens": 10}, 300),
        ("deepseek-v4-pro", {"input_tokens": 1, "output_tokens": 1}, 300),
    ],
)
def test_settlement_is_conservative_and_never_exceeds_reservation(
    model: str, usage: dict[str, object], expected: int
) -> None:
    assert settled_cents(model, usage, 300) == expected


@pytest.mark.parametrize(
    ("usage", "spent", "reserved"),
    [
        ({"input_tokens": 3000, "output_tokens": 50}, 1, 0),  # billed tokens, unusable answer
        ({"forwarded": 0}, 0, 0),  # the guard never let the request out
        ({"forwarded": 1}, 0, 300),  # reached the provider, cost unknown: keep exposure
        ({"forwarded": 1, "rejected": 429}, 0, 0),  # explicitly refused before inference
        ({"forwarded": 1, "rejected": 408}, 0, 300),  # a timeout is not a billing receipt
    ],
)
def test_failed_dsh_calls_settle_by_tokens_or_release_when_nothing_left(
    clients: ProviderClients,
    authorize: Callable[[int], None],
    dsh_transport: Mock,
    usage: dict[str, object],
    spent: int,
    reserved: int,
) -> None:
    authorize(300)
    dsh_transport.return_value = NativeResult("failed", {}, usage=usage, message="bad json")
    clients.call("dsh", "deepseek-flash", "prompt", SCHEMA, "run", lambda: False)
    status = clients.access.status("dsh")
    assert (status["spent_cents"], status["reserved_cents"]) == (spent, reserved)
