from copy import deepcopy
from typing import cast

import pytest

from orchestragent.adapters import PROVIDER_IDS
from orchestragent.serialization import dumps
from orchestragent.settings import SettingsStore
from orchestragent.store import Conflict


def test_defaults_persist_without_reinitializing(settings: SettingsStore) -> None:
    original = settings.read()
    assert SettingsStore(settings.store).read() == original
    assert original == {
        "revision": 1,
        "mode": "mock",
        "policy": {
            "scheduler_provider": "codex",
            "scheduler_model": "gpt-6-astra",
            "implementation_provider": "",
            "review_provider": "",
            "chat_model": "",
            "max_task_cents": 0,
            "manual_review_provider": "",
            "chat_effort": "low",
            "require_acceptance": False,
            "max_reworks": 3,
            "ci_worker_url": "",
            "instructions": "",
            "providers": [
                {"id": name, "model": "", "instructions": "", "effort": ""}
                for name in PROVIDER_IDS
            ],
        },
    }


def test_updates_are_cas_and_return_detached_snapshots(
    settings: SettingsStore,
    policy: dict[str, object],
) -> None:
    policy["instructions"] = "review thoroughly"
    updated = settings.update(1, policy)
    policy["instructions"] = "mutated after save"
    assert updated["revision"] == 2
    assert cast(dict[str, object], settings.read()["policy"])["instructions"] == "review thoroughly"
    with pytest.raises(Conflict):
        settings.update(1, policy)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enabled", True),
        ("token", "never stored"),
        ("budget", 500),
        ("shell", "echo nope"),
        ("scheduler_provider", "unknown"),
        ("scheduler_provider", ["codex"]),
        ("scheduler_model", "x" * 129),
        ("instructions", "x" * 4097),
        ("instructions", 2),
        ("providers", []),
    ],
)
def test_policy_validation_rejects_extra_authority_and_invalid_values(
    settings: SettingsStore,
    policy: dict[str, object],
    field: str,
    value: object,
) -> None:
    policy[field] = value
    with pytest.raises(ValueError):
        settings.update(1, policy)
    assert settings.read()["revision"] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "unknown"),
        ("id", "claude"),
        ("enabled", True),
        ("token", "secret"),
        ("model", "x" * 129),
        ("instructions", 42),
    ],
)
def test_provider_validation(
    settings: SettingsStore,
    policy: dict[str, object],
    providers: list[dict[str, object]],
    field: str,
    value: object,
) -> None:
    providers[0][field] = value
    with pytest.raises(ValueError):
        settings.update(1, policy)


@pytest.mark.parametrize("base_revision", [True, 0, -1])
def test_invalid_revision(
    settings: SettingsStore,
    policy: dict[str, object],
    base_revision: int,
) -> None:
    with pytest.raises(ValueError):
        settings.update(base_revision, policy)


@pytest.mark.parametrize(
    ("instruction", "target"),
    [
        ("Codex should keep this whole sentence", "codex"),
        ("Use Claude Code for thoughtful review", "claude"),
        ("DSH DSH should retain this literal sentence", "dsh"),
        ("Prefer cursor for UI tasks", "cursor"),
        ("kimi settings only", "kimi"),
        ("codex and claude collaborate", "global"),
        ("请优先检查测试", "global"),
        ("请让Codex先检查测试", "codex"),
        ("codexlike is not a provider name", "global"),
    ],
)
def test_proposals_are_literal_previews(
    settings: SettingsStore,
    instruction: str,
    target: str,
) -> None:
    proposal = settings.propose(instruction)
    proposed = cast(dict[str, object], proposal["policy"])
    actual = {"global": proposed["instructions"]} | {
        cast(str, p["id"]): p["instructions"]
        for p in cast(list[dict[str, object]], proposed["providers"])
    }
    assert actual == {name: instruction if name == target else "" for name in actual}
    assert (settings.read()["revision"], proposed["scheduler_model"]) == (1, "gpt-6-astra")
    assert "No semantic interpretation" in cast(str, proposal["summary"])


def test_apply_is_idempotent_even_after_later_updates(
    settings: SettingsStore,
    policy: dict[str, object],
) -> None:
    proposal = settings.propose("add instructions")
    applied = settings.apply(cast(str, proposal["id"]))
    settings.update(2, policy)
    assert settings.apply(cast(str, proposal["id"])) == applied
    with settings.store.transaction() as db:
        count = db.execute("SELECT count(*) FROM settings_audit WHERE kind='applied'").fetchone()[0]
        assert count == 1


def test_stale_proposal_cannot_overwrite_new_settings(
    settings: SettingsStore,
    policy: dict[str, object],
) -> None:
    proposal = settings.propose("do not drop this sentence")
    settings.update(1, policy)
    with pytest.raises(Conflict):
        settings.apply(cast(str, proposal["id"]))
    assert settings.read()["revision"] == 2


def test_preview_cannot_be_mutated_into_permission(settings: SettingsStore) -> None:
    proposal = settings.propose("codex: change the model to imaginary")
    original = deepcopy(proposal["policy"])
    cast(dict[str, object], proposal["policy"])["enabled"] = True
    assert settings.apply(cast(str, proposal["id"]))["policy"] == original


@pytest.mark.parametrize("instruction", ["", "  ", "x" * 2049])
def test_bad_proposal_input(settings: SettingsStore, instruction: str) -> None:
    with pytest.raises(ValueError):
        settings.propose(instruction)


def test_policy_size_and_missing_fields(
    settings: SettingsStore,
    policy: dict[str, object],
    providers: list[dict[str, object]],
) -> None:
    providers[0]["instructions"] = "汉" * 4096
    with pytest.raises(ValueError, match="12000"):
        settings.update(1, policy)
    del policy["instructions"]
    with pytest.raises(ValueError):
        settings.update(1, policy)


def test_append_and_snapshot_share_transaction(settings: SettingsStore) -> None:
    first = settings.propose("codex first")
    settings.apply(cast(str, first["id"]))
    second = settings.propose("codex second")
    result = settings.apply(cast(str, second["id"]))
    policy = cast(dict[str, object], result["policy"])
    providers = cast(list[dict[str, object]], policy["providers"])
    assert providers[0]["instructions"] == "codex first\ncodex second"
    with settings.store.transaction() as db:
        assert settings.snapshot(db) == result


def test_missing_proposal(settings: SettingsStore) -> None:
    with pytest.raises(KeyError):
        settings.apply("missing")


def test_policy_without_optional_fields_is_accepted(settings: SettingsStore) -> None:
    policy = dict(cast(dict[str, object], settings.read()["policy"]))
    for field in ("implementation_provider", "review_provider", "chat_model", "max_task_cents"):
        policy.pop(field, None)
    updated = settings.update(1, policy)
    saved = cast(dict[str, object], updated["policy"])
    assert (saved["max_task_cents"], saved["chat_model"]) == (0, "")


def test_invalid_task_budget_is_rejected(settings: SettingsStore) -> None:
    policy = dict(cast(dict[str, object], settings.read()["policy"]))
    for bad in ("", -1, 1_000_001, 1.5):
        with pytest.raises(ValueError):
            settings.update(1, {**policy, "max_task_cents": bad})


def test_old_revisions_get_new_fields_on_read(settings: SettingsStore) -> None:
    policy = dict(cast(dict[str, object], settings.read()["policy"]))
    for field in ("manual_review_provider", "max_task_cents", "chat_model"):
        policy.pop(field, None)
    with settings.store.transaction() as db:
        db.execute("INSERT INTO settings_revisions VALUES(2,?)", (dumps(policy),))
    read = cast(dict[str, object], settings.read()["policy"])
    assert (read["manual_review_provider"], read["max_task_cents"]) == ("", 0)


def test_unknown_model_or_effort_is_rejected(settings: SettingsStore) -> None:
    base = dict(cast(dict[str, object], settings.read()["policy"]))
    providers = [dict(entry) for entry in cast(list[dict[str, object]], base["providers"])]
    providers[2]["model"] = "DeepSeek-V4.1-Flash-1017"
    with pytest.raises(ValueError):
        settings.update(1, {**base, "providers": providers})
    providers[2]["model"] = "deepseek-flash"
    providers[0]["effort"] = "minimal"
    with pytest.raises(ValueError):
        settings.update(1, {**base, "providers": providers})
    providers[0]["effort"] = "xhigh"
    updated = settings.update(1, {**base, "providers": providers})
    policy = cast(dict[str, object], updated["policy"])
    saved = cast(list[dict[str, object]], policy["providers"])
    first = saved[0]
    assert first["effort"] == "xhigh"
