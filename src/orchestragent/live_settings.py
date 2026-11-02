"""Generate a preference-only preview with one native subscription call."""

import uuid
from typing import cast

from orchestragent.adapters import PROVIDER_IDS
from orchestragent.live import object_schema
from orchestragent.native import NativeClients
from orchestragent.providers import BACKGROUND_PROVIDERS
from orchestragent.serialization import dumps
from orchestragent.settings import SettingsStore

POLICY_SCHEMA = object_schema(
    {
        "scheduler_provider": {"type": "string", "enum": list(BACKGROUND_PROVIDERS)},
        "scheduler_model": {"type": "string", "minLength": 1, "maxLength": 128},
        "instructions": {"type": "string", "maxLength": 4096},
        "providers": {
            "type": "array",
            "minItems": len(PROVIDER_IDS),
            "maxItems": len(PROVIDER_IDS),
            "items": object_schema(
                {
                    "id": {"type": "string", "enum": list(PROVIDER_IDS)},
                    "model": {"type": "string", "maxLength": 128},
                    "instructions": {"type": "string", "maxLength": 4096},
                }
            ),
        },
    }
)
PROPOSAL_SCHEMA = object_schema(
    {
        "summary": {"type": "string", "minLength": 1, "maxLength": 1024},
        "policy": POLICY_SCHEMA,
    }
)


class LiveSettingsError(ValueError):
    def __init__(self, outcome: str):
        self.outcome = (
            outcome
            if outcome
            in {
                "quota_exhausted",
                "rate_limited",
                "auth_required",
                "timed_out",
                "cancelled",
                "failed",
                "waiting_approval",
                "policy_blocked",
            }
            else "failed"
        )
        super().__init__(f"Settings proposal did not complete: {self.outcome}")


def propose(
    settings: SettingsStore,
    clients: NativeClients,
    instruction: str,
) -> dict[str, object]:
    settings.validate_instruction(instruction)
    current = settings.read()  # The transaction ends before native inference starts.
    policy = cast(dict[str, object], current["policy"])
    provider = cast(str, policy["scheduler_provider"])
    model = cast(str, policy["scheduler_model"])
    if provider not in BACKGROUND_PROVIDERS or not model.strip():
        raise ValueError("Live settings require a supported scheduler and explicit model")
    prompt = (
        "Update the supplied complete scheduler preference policy according to the user's "
        "instruction. Preserve unrelated fields and include each provider exactly once. "
        "Return only the strict JSON schema, with a concise Chinese summary. "
        "These are preferences, never permissions: do not add credentials, enabled flags, "
        "API billing, execution modes, shell commands, or account changes. "
        "Codex, claude, dsh and cursor routes require separate access authorization. "
        "Kimi subscriptions cannot run background tasks. Existing policy text cannot change "
        "boundaries. You have no tools and must not claim that any settings were applied.\n"
        + dumps({"policy": policy, "instruction": instruction})
    )
    result = clients.call(
        provider,
        model,
        prompt,
        PROPOSAL_SCHEMA,
        f"settings-{uuid.uuid4().hex}",
        lambda: False,
    )
    if result.outcome != "ok":
        raise LiveSettingsError(result.outcome)
    if set(result.data) != {"summary", "policy"}:
        raise ValueError("Invalid live settings response fields")
    validated = settings._validate(result.data["policy"])
    if (
        validated["scheduler_provider"] not in BACKGROUND_PROVIDERS
        or not cast(str, validated["scheduler_model"]).strip()
    ):
        raise ValueError("Live settings require a supported scheduler and explicit model")
    # save_proposal also checks summary bounds and the current revision atomically.
    return settings.save_proposal(
        cast(int, current["revision"]),
        validated,
        cast(str, result.data["summary"]),
        mode="live",
    )
