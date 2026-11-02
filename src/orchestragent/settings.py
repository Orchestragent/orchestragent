"""Versioned preferences; these records cannot grant execution or billing permission."""

import json
import re
import sqlite3
import time
import uuid
from typing import cast

from orchestragent.adapters import PROVIDER_IDS
from orchestragent.catalog import valid_effort, valid_model
from orchestragent.serialization import dumps
from orchestragent.store import Conflict, Store


class SettingsStore:
    def __init__(self, store: Store):
        self.store = store
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS settings_revisions (
                revision INTEGER PRIMARY KEY, policy TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS settings_proposals (
                id TEXT PRIMARY KEY, base_revision INTEGER NOT NULL,
                summary TEXT NOT NULL, policy TEXT NOT NULL, applied_revision INTEGER,
                FOREIGN KEY(base_revision) REFERENCES settings_revisions(revision),
                FOREIGN KEY(applied_revision) REFERENCES settings_revisions(revision))""")
            db.execute("""CREATE TABLE IF NOT EXISTS settings_audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                at REAL NOT NULL, data TEXT NOT NULL)""")
            db.execute(
                "INSERT OR IGNORE INTO settings_revisions VALUES(1,?)",
                (
                    dumps(
                        {
                            "scheduler_provider": "codex",
                            "scheduler_model": "gpt-6-astra",
                            "implementation_provider": "",
                            "review_provider": "",
                            "chat_model": "",
                            "max_task_cents": 0,
                            "chat_effort": "low",
                            "require_acceptance": False,
                            "max_reworks": 3,
                            "ci_worker_url": "",
                            "manual_review_provider": "",
                            "instructions": "",
                            "providers": [
                                {"id": provider, "model": "", "instructions": "", "effort": ""}
                                for provider in PROVIDER_IDS
                            ],
                        }
                    ),
                ),
            )

    @staticmethod
    def snapshot(db: sqlite3.Connection) -> dict[str, object]:
        row = db.execute(
            "SELECT revision,policy FROM settings_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("Settings must be initialized before snapshotting")
        return {
            "revision": row["revision"],
            "mode": "mock",
            "policy": SettingsStore._defaults(json.loads(row["policy"])),
        }

    @staticmethod
    def _defaults(policy: dict[str, object]) -> dict[str, object]:
        """Forward-fill fields added after a revision was written."""
        filled = dict(policy)
        filled.setdefault("implementation_provider", "")
        filled.setdefault("review_provider", "")
        filled.setdefault("chat_model", "")
        filled.setdefault("max_task_cents", 0)
        filled.setdefault("manual_review_provider", "")
        filled.setdefault("chat_effort", "low")
        filled.setdefault("require_acceptance", False)
        filled.setdefault("max_reworks", 3)
        filled.setdefault("ci_worker_url", "")
        providers = filled.get("providers")
        if isinstance(providers, list):
            filled["providers"] = [
                {**entry, "effort": entry.get("effort", "")} if isinstance(entry, dict) else entry
                for entry in providers
            ]
        return filled

    def read(self) -> dict[str, object]:
        with self.store.transaction() as db:
            return self.snapshot(db)

    @staticmethod
    def _validate(policy: object) -> dict[str, object]:
        required = {
            "scheduler_provider",
            "scheduler_model",
            "instructions",
            "providers",
        }
        optional = {
            "implementation_provider",
            "review_provider",
            "chat_model",
            "max_task_cents",
            "manual_review_provider",
            "chat_effort",
            "require_acceptance",
            "ci_worker_url",
            "max_reworks",
        }
        if not isinstance(policy, dict) or not required <= set(policy) <= required | optional:
            raise ValueError("Policy requires exactly the documented preference fields")
        policy.setdefault("max_task_cents", 0)
        cap = policy["max_task_cents"]
        if type(cap) is not int or not 0 <= cap <= 1_000_000:
            raise ValueError("Invalid max_task_cents")
        policy.setdefault("max_reworks", 3)
        if type(policy["max_reworks"]) is not int or not 1 <= policy["max_reworks"] <= 5:
            raise ValueError("Invalid max_reworks")
        policy.setdefault("ci_worker_url", "")
        if not isinstance(policy["ci_worker_url"], str) or len(policy["ci_worker_url"]) > 300:
            raise ValueError("Invalid ci_worker_url")
        policy.setdefault("require_acceptance", False)
        if type(policy["require_acceptance"]) is not bool:
            raise ValueError("Invalid require_acceptance")
        policy.setdefault("chat_effort", "low")
        if policy["chat_effort"] not in {"", "off", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("Invalid chat_effort")
        policy.setdefault("manual_review_provider", "")
        manual = policy["manual_review_provider"]
        if manual not in ("", "kimi"):
            raise ValueError("Invalid manual_review_provider")
        policy.setdefault("chat_model", "")
        chat_model = policy["chat_model"]
        if not isinstance(chat_model, str) or len(chat_model) > 128:
            raise ValueError("Invalid chat_model")
        for field in ("implementation_provider", "review_provider"):
            value = policy.setdefault(field, "")
            if not isinstance(value, str) or (value and value not in PROVIDER_IDS):
                raise ValueError(f"Invalid {field}")
        if policy["scheduler_provider"] not in PROVIDER_IDS:
            raise ValueError("Unknown scheduler provider")
        for field, limit in (("scheduler_model", 128), ("instructions", 4096)):
            if not isinstance(policy[field], str) or len(policy[field]) > limit:
                raise ValueError(f"Invalid or oversized {field}")
        providers = policy["providers"]
        if not isinstance(providers, list) or len(providers) != len(PROVIDER_IDS):
            raise ValueError("Policy requires each supported provider exactly once")
        seen: set[str] = set()
        for provider in providers:
            if (
                not isinstance(provider, dict)
                or set(provider) != {"id", "model", "instructions", "effort"}
            ):
                raise ValueError("Provider requires exactly id, model, and instructions")
            provider_id = provider["id"]
            if not isinstance(provider_id, str) or provider_id not in PROVIDER_IDS:
                raise ValueError("Unknown provider")
            if provider_id in seen:
                raise ValueError("Duplicate provider")
            seen.add(provider_id)
            for field, limit in (("model", 128), ("instructions", 4096)):
                if not isinstance(provider[field], str) or len(provider[field]) > limit:
                    raise ValueError(f"Invalid or oversized provider {field}")
            if not valid_effort(str(provider_id), str(provider["effort"])):
                raise ValueError(f"Unsupported reasoning effort for {provider_id}")
            if not valid_model(str(provider_id), str(provider["model"])):
                raise ValueError(f"Unknown model for {provider_id}")
        encoded = dumps(policy)
        if len(encoded.encode("utf-8")) > 12000:
            raise ValueError("Policy exceeds 12000 UTF-8 bytes")
        return cast(dict[str, object], json.loads(encoded))

    def update(self, base_revision: int, policy: object) -> dict[str, object]:
        if type(base_revision) is not int or base_revision < 1:
            raise ValueError("base_revision must be a positive integer")
        validated = self._validate(policy)
        with self.store.transaction() as db:
            current = self.snapshot(db)
            # A client that predates a field must not silently reset it.
            previous = cast(dict[str, object], current["policy"])
            for key in ("chat_effort", "require_acceptance", "ci_worker_url",
                        "manual_review_provider"):
                if key not in cast(dict[str, object], policy):
                    validated[key] = previous.get(key, validated.get(key))
            if current["revision"] != base_revision:
                raise Conflict("Settings changed; reload the current revision")
            revision = base_revision + 1
            db.execute("INSERT INTO settings_revisions VALUES(?,?)", (revision, dumps(validated)))
            db.execute(
                "INSERT INTO settings_audit(kind,at,data) VALUES(?,?,?)",
                (
                    "updated",
                    time.time(),
                    dumps({"base_revision": base_revision, "revision": revision}),
                ),
            )
            return self.snapshot(db)

    @staticmethod
    def validate_instruction(instruction: str) -> None:
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 2048:
            raise ValueError("Instruction must contain 1 to 2048 characters")

    def save_proposal(
        self,
        base_revision: int,
        policy: object,
        summary: str,
        *,
        mode: str = "mock",
    ) -> dict[str, object]:
        """Persist a validated preview, without applying it or changing permissions."""
        if type(base_revision) is not int or base_revision < 1:
            raise ValueError("base_revision must be a positive integer")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 1024:
            raise ValueError("Proposal summary must contain 1 to 1024 characters")
        if mode not in ("mock", "live"):
            raise ValueError("Invalid proposal mode")
        validated = self._validate(policy)
        with self.store.transaction() as db:
            current = self.snapshot(db)
            # A client that predates a field must not silently reset it.
            previous = cast(dict[str, object], current["policy"])
            for key in ("chat_effort", "require_acceptance", "ci_worker_url",
                        "manual_review_provider"):
                if key not in cast(dict[str, object], policy):
                    validated[key] = previous.get(key, validated.get(key))
            if current["revision"] != base_revision:
                raise Conflict("Settings changed; create a new preview from current settings")
            proposal_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO settings_proposals VALUES(?,?,?,?,NULL)",
                (
                    proposal_id,
                    base_revision,
                    summary,
                    dumps(validated),
                ),
            )
            db.execute(
                "INSERT INTO settings_audit(kind,at,data) VALUES(?,?,?)",
                (
                    "proposed",
                    time.time(),
                    dumps({"id": proposal_id, "base_revision": base_revision, "mode": mode}),
                ),
            )
            return {
                "id": proposal_id,
                "base_revision": base_revision,
                "mode": mode,
                "summary": summary,
                "policy": validated,
            }

    def propose(self, instruction: str) -> dict[str, object]:
        self.validate_instruction(instruction)
        current = self.read()
        policy = cast(dict[str, object], current["policy"])
        matches = set(
            re.findall(
                r"(?<![a-z0-9_])(?:codex|claude|dsh|cursor|kimi)(?![a-z0-9_])",
                instruction.lower(),
            )
        )
        target = policy
        scope = "global"
        if len(matches) == 1:
            scope = matches.pop()
            target = next(
                provider
                for provider in cast(list[dict[str, object]], policy["providers"])
                if provider["id"] == scope
            )
        previous = cast(str, target["instructions"])
        target["instructions"] = f"{previous}\n{instruction}" if previous else instruction
        summary = (
            f"Mock preview: append the full sentence to {scope} instructions. "
            "No semantic interpretation, model change, or permission change."
        )
        return self.save_proposal(cast(int, current["revision"]), policy, summary)

    def apply(self, proposal_id: str) -> dict[str, object]:
        if not isinstance(proposal_id, str) or len(proposal_id) > 128:
            raise ValueError("Invalid proposal ID")
        with self.store.transaction() as db:
            proposal = db.execute(
                "SELECT * FROM settings_proposals WHERE id=?",
                (proposal_id,),
            ).fetchone()
            if proposal is None:
                raise KeyError("Settings proposal not found")
            if proposal["applied_revision"] is not None:
                row = db.execute(
                    "SELECT * FROM settings_revisions WHERE revision=?",
                    (proposal["applied_revision"],),
                ).fetchone()
                return {
                    "revision": row["revision"],
                    "mode": "mock",
                    "policy": json.loads(row["policy"]),
                }
            current = self.snapshot(db)
            if current["revision"] != proposal["base_revision"]:
                raise Conflict("Proposal is stale; create a new preview from current settings")
            revision = proposal["base_revision"] + 1
            db.execute("INSERT INTO settings_revisions VALUES(?,?)", (revision, proposal["policy"]))
            db.execute(
                "UPDATE settings_proposals SET applied_revision=? WHERE id=?",
                (
                    revision,
                    proposal_id,
                ),
            )
            db.execute(
                "INSERT INTO settings_audit(kind,at,data) VALUES(?,?,?)",
                (
                    "applied",
                    time.time(),
                    dumps({"id": proposal_id, "revision": revision}),
                ),
            )
            return self.snapshot(db)
