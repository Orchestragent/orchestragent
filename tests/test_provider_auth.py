from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from orchestragent import access as access_module
from orchestragent import deepseek_api
from orchestragent.access import Access
from orchestragent.native import NativeClients, NativeResult
from orchestragent.providers import ProviderClients
from orchestragent.serialization import dumps

KEY = "fixture-auth-material-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
REPLACEMENT = "replacement-auth-material-0123456789"
MODEL = "deepseek-flash"
SUCCESS = NativeResult("ok", {"answer": 42}, usage={
    "forwarded": 1, "input_tokens": 10, "output_tokens": 3,
})


def grant(access: Access, key: str = KEY) -> None:
    operation = access.operation("dsh", 10000, "budget")
    access.authorize("dsh", 10000, "budget", str(operation["operation_digest"]), key)


@dataclass
class AuthFixture:
    clients: ProviderClients
    call: Mock
    vault: dict[str, str]


@pytest.fixture
def auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AuthFixture]:
    vault: dict[str, str] = {}
    monkeypatch.setattr(Access, "_persist", staticmethod(lambda: True))
    monkeypatch.setattr(access_module, "is_macos", lambda: True)
    monkeypatch.setattr(Access, "_keychain_store", staticmethod(vault.__setitem__))
    monkeypatch.setattr(Access, "_keychain_load", lambda self, provider: vault.get(provider, ""))
    monkeypatch.setattr(NativeClients, "status", lambda self: {})
    monkeypatch.setenv("ORCHESTRA_DSH_ACCOUNT", "0")
    executable = tmp_path / "fake-dsh"
    executable.write_text("not executable or invoked")
    monkeypatch.setattr("orchestragent.providers.dsh_binary", lambda: executable)
    clients = ProviderClients(tmp_path / "providers")
    monkeypatch.setattr(clients.cursor, "status", lambda: {"ready": False})
    call = Mock(return_value=SUCCESS)
    monkeypatch.setattr(clients.dsh, "call", call)
    monkeypatch.setattr(deepseek_api, "call", call)
    grant(clients.access)
    yield AuthFixture(clients, call, vault)
    clients.close()


def authentication(
    auth: AuthFixture, model: str = MODEL, transport: str = "cli",
) -> dict[str, object]:
    return auth.clients.access.authentication("dsh", model, transport)


def test_configured_unverified_setup_allows_explicit_first_inference(auth: AuthFixture) -> None:
    before = cast(dict[str, object], auth.clients.status()["dsh"])
    assert before["configured"] is True and before["verified"] is False and before["ready"] is False
    assert "unverified" in str(before["message"])
    result = auth.clients.call("dsh", MODEL, "prompt", {}, "first", lambda: False)
    after = cast(dict[str, object], auth.clients.status()["dsh"])
    assert result.outcome == "ok" and auth.call.call_count == 1
    assert after["configured"] is after["verified"] is after["ready"] is True
    assert authentication(auth)["state"] == "verified"


@pytest.mark.parametrize(("model", "transport"), [
    ("deepseek-flash", "cli"), ("deepseek-flash", "api"),
    ("deepseek-v4-pro", "cli"), ("deepseek-v4-pro", "api"),
])
def test_authentication_is_bound_to_requested_model_and_transport(
    auth: AuthFixture, monkeypatch: pytest.MonkeyPatch, model: str, transport: str,
) -> None:
    monkeypatch.setenv("ORCHESTRA_DSF_TRANSPORT", transport)
    auth.clients.call("dsh", model, "prompt", {}, "proof", lambda: False)
    assert authentication(auth, model, transport)["verified"] is True
    other_model = "deepseek-v4-pro" if model == MODEL else MODEL
    other_transport = "api" if transport == "cli" else "cli"
    assert authentication(auth, other_model, transport)["verified"] is False
    assert authentication(auth, model, other_transport)["verified"] is False


@pytest.mark.parametrize(("outcome", "usage"), [
    ("failed", {"forwarded": 1}), ("timed_out", {"forwarded": 1}),
    ("rate_limited", {"forwarded": 1, "rejected": 429}),
    ("auth_required", {"forwarded": 1}), ("ok", {"forwarded": 0}),
    ("ok", {}), ("ok", {"forwarded": True}),
])
def test_unknown_transient_or_unforwarded_outcomes_never_invent_authentication(
    auth: AuthFixture, outcome: str, usage: dict[str, object],
) -> None:
    auth.call.return_value = NativeResult(outcome, {}, usage=usage)
    auth.clients.call("dsh", MODEL, "prompt", {}, "unverified", lambda: False)
    assert authentication(auth)["verified"] is False


@pytest.mark.parametrize("status", [401, 403])
def test_authoritative_rejection_invalidates_verified_route(auth: AuthFixture, status: int) -> None:
    auth.clients.call("dsh", MODEL, "prompt", {}, "verified", lambda: False)
    auth.call.return_value = NativeResult("auth_required", {},
                                         usage={"forwarded": 1, "rejected": status})
    auth.clients.call("dsh", MODEL, "prompt", {}, "rejected", lambda: False)
    state = authentication(auth)
    assert state["verified"] is False and state["state"] == "auth_required"
    assert cast(dict[str, object], auth.clients.status()["dsh"])["ready"] is False
    assert auth.clients.access.status("dsh")["reserved_cents"] == 0


def test_transient_failure_preserves_prior_positive_authentication(auth: AuthFixture) -> None:
    auth.clients.call("dsh", MODEL, "prompt", {}, "verified", lambda: False)
    auth.call.return_value = NativeResult("failed", {}, usage={"forwarded": 1})
    auth.clients.call("dsh", MODEL, "prompt", {}, "unknown", lambda: False)
    assert authentication(auth)["verified"] is True
    assert auth.clients.access.status("dsh")["reserved_cents"] == 300


@pytest.mark.parametrize("platform", ["macos", "linux"])
def test_legacy_raw_key_upgrades_without_reentry_and_proof_survives_restart(
    auth: AuthFixture, monkeypatch: pytest.MonkeyPatch, platform: str,
) -> None:
    access = auth.clients.access
    if platform == "linux":
        monkeypatch.setattr(access_module, "is_macos", lambda: False)
        access._file_store("dsh", KEY)
    else:
        auth.vault["dsh"] = KEY
    restored = Access(access.root)
    restored.restore_keys()
    assert restored.key("dsh") == KEY
    assert restored.authentication("dsh", MODEL, "cli")["verified"] is False
    auth.clients.access = restored
    auth.clients.call("dsh", MODEL, "prompt", {}, "restored-first-call", lambda: False)
    reopened = Access(access.root)
    reopened.restore_keys()
    assert reopened.key("dsh") == KEY
    assert reopened.authentication("dsh", MODEL, "cli")["verified"] is True
    assert reopened.credential_generation("dsh") == restored.credential_generation("dsh")


def test_replacement_invalidates_old_proof_without_persisting_secret_or_digest(
    auth: AuthFixture,
) -> None:
    access = auth.clients.access
    auth.clients.call("dsh", MODEL, "prompt", {}, "verified", lambda: False)
    old_generation = access.credential_generation("dsh")
    grant(access, REPLACEMENT)
    assert access.credential_generation("dsh") != old_generation
    assert authentication(auth)["verified"] is False
    with access.store.transaction() as db:
        persisted = "\n".join(db.iterdump())
    public = dumps(auth.clients.status()) + persisted
    assert KEY not in public and REPLACEMENT not in public
    assert "fixture-auth-material" not in public and "replacement-auth-material" not in public


def test_already_stale_client_never_dispatches_cached_old_key(auth: AuthFixture) -> None:
    other = Access(auth.clients.access.root)
    other.restore_keys()
    grant(other, REPLACEMENT)
    result = auth.clients.call("dsh", MODEL, "prompt", {}, "stale-client", lambda: False)
    assert result.outcome == "auth_required" and result.usage == {"forwarded": 0}
    assert auth.call.call_count == 0
    assert auth.clients.access.status("dsh")["reserved_cents"] == 0
    assert cast(dict[str, object], auth.clients.status()["dsh"])["configured"] is False


@pytest.mark.parametrize("phase", ["key-read", "reservation"])
def test_rotation_during_dispatch_preparation_never_reaches_transport(
    auth: AuthFixture, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    access = auth.clients.access
    if phase == "key-read":
        original_key = access.key
        reads = 0

        def rotating_key(provider: str) -> str:
            nonlocal reads
            reads += 1
            key = original_key(provider)
            if reads == 3:  # after the two existing local key/readiness checks
                grant(access, REPLACEMENT)
            return key

        monkeypatch.setattr(access, "key", rotating_key)
    else:
        original_reserve = access.reserve

        def rotating_reservation(provider: str, run_id: str, cents: int) -> bool:
            reserved = original_reserve(provider, run_id, cents)
            grant(access, REPLACEMENT)
            return reserved

        monkeypatch.setattr(access, "reserve", rotating_reservation)
    result = auth.clients.call("dsh", MODEL, "prompt", {}, "raced", lambda: False)
    assert result.outcome == "auth_required" and auth.call.call_count == 0
    assert access.status("dsh")["reserved_cents"] == 0


@pytest.mark.parametrize("outcome", ["ok", "auth_required"])
def test_stale_inflight_results_cannot_bless_or_revoke_replacement_key(
    auth: AuthFixture, outcome: str,
) -> None:
    access = auth.clients.access
    captured = access.credential_generation("dsh")

    def rotate(*args: object, **kwargs: object) -> NativeResult:
        assert kwargs["api_key"] == KEY
        grant(access, REPLACEMENT)
        generation = access.credential_generation("dsh")
        assert access.record_auth("dsh", MODEL, "cli", generation, "ok")
        return SUCCESS if outcome == "ok" else NativeResult(
            "auth_required", {}, usage={"forwarded": 1, "rejected": 401})

    auth.call.side_effect = rotate
    auth.clients.call("dsh", MODEL, "prompt", {}, "old-inflight", lambda: False)
    assert access.credential_generation("dsh") != captured
    assert authentication(auth)["verified"] is True
    assert access.record_auth("dsh", MODEL, "cli", captured, "auth_required") is False


def test_account_route_does_not_inherit_api_key_authentication(auth: AuthFixture) -> None:
    auth.clients.call("dsh", MODEL, "prompt", {}, "api-proof", lambda: False)
    auth.clients.dsh.account = True
    state = cast(dict[str, object], auth.clients.status()["dsh"])
    assert state["verified"] is False
    assert cast(dict[str, object], state["authentication"])["transport"] == "cli-account"
    auth.clients.call("dsh", MODEL, "prompt", {}, "account-call", lambda: False)
    assert auth.clients.access.authentication("dsh", MODEL, "cli-account")["verified"] is False