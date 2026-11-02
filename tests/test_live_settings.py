from collections.abc import Callable
from functools import partial
from typing import cast

import pytest

from orchestragent.live_settings import (
    POLICY_SCHEMA,
    PROPOSAL_SCHEMA,
    LiveSettingsError,
    propose,
)
from orchestragent.native import NativeClients, NativeResult
from orchestragent.settings import SettingsStore
from orchestragent.store import Conflict


class FakeClients(NativeClients):
    def __init__(self, result: NativeResult):
        self.result = result
        self.calls: list[tuple[str, str, str, dict[str, object], str]] = []
        self.during_call: Callable[[], object] | None = None

    def call(
        self,
        provider: str,
        model: str,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        cancelled: Callable[[], bool],
        *,
        effort: str = "",
    ) -> NativeResult:
        self.calls.append((provider, model, prompt, schema, run_id))
        if self.during_call is not None:
            self.during_call()
        return self.result


@pytest.fixture
def clients(policy: dict[str, object]) -> FakeClients:
    policy["instructions"] = "优先检查边界条件"
    return FakeClients(NativeResult("ok", {"summary": "更新全局要求", "policy": policy}))


def test_live_preview_requires_apply_and_uses_current_scheduler(
    settings: SettingsStore,
    clients: FakeClients,
) -> None:
    before = settings.read()
    proposal = propose(settings, clients, "优先检查边界条件")
    assert settings.read() == before
    assert proposal["mode"] == "live"
    assert proposal["policy"] == clients.result.data["policy"]
    assert clients.calls[0][:2] == ("codex", "gpt-6-astra")
    assert "优先检查边界条件" in clients.calls[0][2]
    assert clients.calls[0][3] == PROPOSAL_SCHEMA
    applied = settings.apply(cast(str, proposal["id"]))
    assert applied["revision"] == 2
    assert applied["policy"] == proposal["policy"]


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_configured_native_route_is_used_without_fallback(
    settings: SettingsStore,
    policy: dict[str, object],
    clients: FakeClients,
    provider: str,
) -> None:
    policy.update(scheduler_provider=provider, scheduler_model="chosen-model")
    settings.update(1, policy)
    propose(settings, clients, "修改我的要求")
    assert clients.calls[0][:2] == (provider, "chosen-model")
    assert len(clients.calls) == 1


@pytest.mark.parametrize("provider,model", [("kimi", "model"), ("codex", " ")])
def test_unsupported_route_is_rejected_before_inference(
    settings: SettingsStore,
    policy: dict[str, object],
    clients: FakeClients,
    provider: str,
    model: str,
) -> None:
    policy.update(scheduler_provider=provider, scheduler_model=model)
    settings.update(1, policy)
    with pytest.raises(ValueError, match="scheduler and explicit model"):
        propose(settings, clients, "调整要求")
    assert clients.calls == []


@pytest.mark.parametrize("instruction", ["", " ", "x" * 2049])
def test_invalid_instruction_never_calls_native_client(
    settings: SettingsStore,
    clients: FakeClients,
    instruction: str,
) -> None:
    with pytest.raises(ValueError, match="Instruction"):
        propose(settings, clients, instruction)
    assert clients.calls == []


@pytest.mark.parametrize(
    "outcome",
    [
        "quota_exhausted",
        "rate_limited",
        "auth_required",
        "timed_out",
        "cancelled",
        "failed",
        "waiting_approval",
        "untrusted error text",
    ],
)
def test_native_failure_is_sanitized_and_never_retried(
    settings: SettingsStore,
    clients: FakeClients,
    outcome: str,
) -> None:
    clients.result = NativeResult(outcome, {}, message="raw diagnostic must not escape")
    with pytest.raises(LiveSettingsError) as error:
        propose(settings, clients, "调整要求")
    assert error.value.outcome == ("failed" if outcome == "untrusted error text" else outcome)
    assert "raw diagnostic" not in str(error.value)
    assert len(clients.calls) == 1
    assert settings.read()["revision"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", True),
        ("mode", "api"),
        ("summary", ""),
        ("summary", " " * 5),
        ("summary", 5),
        ("summary", "x" * 1025),
        ("policy", []),
    ],
)
def test_invalid_response_is_not_saved(
    settings: SettingsStore,
    clients: FakeClients,
    field: str,
    value: object,
) -> None:
    clients.result.data[field] = value
    with pytest.raises(ValueError):
        propose(settings, clients, "调整要求")
    assert settings.read()["revision"] == 1
    with settings.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM settings_proposals").fetchone()[0] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", True),
        ("api_key", "forbidden"),
        ("scheduler_provider", "kimi"),
        ("scheduler_model", ""),
        ("instructions", "x" * 4097),
        ("providers", []),
    ],
)
def test_generated_policy_reuses_local_preference_validation(
    settings: SettingsStore,
    clients: FakeClients,
    field: str,
    value: object,
) -> None:
    cast(dict[str, object], clients.result.data["policy"])[field] = value
    with pytest.raises(ValueError):
        propose(settings, clients, "调整要求")
    assert settings.read()["revision"] == 1


def test_concurrent_update_during_inference_conflicts_without_holding_sqlite_lock(
    settings: SettingsStore,
    policy: dict[str, object],
    clients: FakeClients,
) -> None:
    clients.during_call = partial(settings.update, 1, policy)
    with pytest.raises(Conflict):
        propose(settings, clients, "不要覆盖并发修改")
    assert len(clients.calls) == 1
    assert settings.read()["revision"] == 2
    with settings.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM settings_proposals").fetchone()[0] == 0


@pytest.mark.parametrize("schema", [PROPOSAL_SCHEMA, POLICY_SCHEMA])
def test_output_objects_require_exact_fields(schema: dict[str, object]) -> None:
    assert schema["additionalProperties"] is False
    assert set(cast(list[str], schema["required"])) == set(
        cast(dict[str, object], schema["properties"])
    )
