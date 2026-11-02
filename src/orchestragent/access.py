"""Explicit local API authorization; credentials stay in process memory, never SQLite."""

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from orchestragent.catalog import valid_model
from orchestragent.platform import is_macos
from orchestragent.serialization import dumps
from orchestragent.store import Conflict, Store

# API routes that may hold a CNY budget, with the per-call reservations each may make.
BUDGETED: dict[str, frozenset[int]] = {
    "dsh": frozenset({300, 1000}),
    "jev": frozenset({2}),
}
# The existing DSH guard's explicit pre-inference rejection list, shared by all transports.
REJECTED_STATUSES = frozenset({400, 401, 402, 403, 404, 413, 415, 422, 429})


@dataclass(frozen=True)
class _Credential:
    schema_version: int
    generation: str
    key: str = field(repr=False)


class Access:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.store = Store(root / "provider-access.sqlite")
        self._keys: dict[str, str] = {}
        self._generations: dict[str, str] = {}
        self._lock = threading.Lock()
        with self.store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS provider_authorization (
                provider TEXT PRIMARY KEY, cap_cents INTEGER NOT NULL,
                funding_kind TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS provider_reservations (
                run_id TEXT PRIMARY KEY, provider TEXT NOT NULL, cents INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS provider_spend (
                run_id TEXT PRIMARY KEY, provider TEXT NOT NULL, cents INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS provider_credentials (
                provider TEXT PRIMARY KEY, generation TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS provider_authentication (
                provider TEXT NOT NULL, model TEXT NOT NULL, transport TEXT NOT NULL,
                generation TEXT NOT NULL, outcome TEXT NOT NULL, observed_at REAL NOT NULL,
                PRIMARY KEY(provider, model, transport))""")

    @staticmethod
    def operation(provider: str, cap_cents: int, funding_kind: str) -> dict[str, object]:
        # 0 = no local cap (the default for orch keys); a cap is recorded, never enforced.
        if provider not in BUDGETED or type(cap_cents) is not int or not 0 <= cap_cents <= 50000:
            raise ValueError("API budget must be 0–500 CNY for dsh or jev")
        if funding_kind not in {"balance", "budget"}:
            raise ValueError("Confirm whether the quoted amount is balance or budget")
        operation: dict[str, object] = {
            "provider": provider,
            "cap_cents": cap_cents,
            "currency": "CNY",
            "funding_kind": funding_kind,
            "billing": "api",
            "scope": "local Orchestragent; no top-up, fallback or account change",
        }
        return {
            **operation,
            "operation_digest": hashlib.sha256(dumps(operation).encode()).hexdigest(),
        }

    def authorize(
        self, provider: str, cap_cents: int, funding_kind: str, digest: str, api_key: str
    ) -> None:
        operation = self.operation(provider, cap_cents, funding_kind)
        if digest != operation["operation_digest"]:
            raise Conflict("Authorization does not match the concrete operation")
        if (
            not isinstance(api_key, str)
            or not 10 <= len(api_key) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in api_key)
        ):
            raise ValueError("Enter the API key locally")
        generation = uuid.uuid4().hex if provider == "dsh" else ""
        with self._lock:
            with self.store.transaction() as db:
                spent = db.execute(
                    "SELECT COALESCE(SUM(cents),0) FROM provider_reservations WHERE provider=?",
                    (provider,),
                ).fetchone()[0]
                if 0 < cap_cents < spent:
                    raise Conflict("Budget cannot be below existing reservations")
                db.execute(
                    "INSERT INTO provider_authorization VALUES(?,?,?) "
                    "ON CONFLICT(provider) DO UPDATE SET cap_cents=excluded.cap_cents, "
                    "funding_kind=excluded.funding_kind",
                    (provider, cap_cents, funding_kind),
                )
                if generation:
                    db.execute("INSERT INTO provider_credentials VALUES(?,?) ON CONFLICT(provider) "
                               "DO UPDATE SET generation=excluded.generation",
                               (provider, generation))
                    db.execute("DELETE FROM provider_authentication WHERE provider=?", (provider,))
                self._keys[provider] = api_key
                self._generations[provider] = generation
                self._store_credential(provider, api_key, generation)

    def _store_credential(self, provider: str, api_key: str, generation: str = "") -> None:
        """Bind DSF generations inside the existing protected store, never in SQLite."""
        if not self._persist():
            return
        payload = dumps(asdict(_Credential(1, generation, api_key))) if generation else api_key
        if is_macos():
            self._keychain_store(provider, payload)
        else:
            self._file_store(provider, payload)

    @staticmethod
    def _persist() -> bool:
        if not is_macos():
            # A 0600 file is the normal store off macOS; only the keychain has
            # an opt-out for tests.
            return True
        return os.environ.get("ORCHESTRA_KEYCHAIN", "1") != "0"

    def _secret_path(self, provider: str) -> Path:
        return self.root / "secrets" / f"{provider}.key"

    def _file_store(self, provider: str, api_key: str) -> None:
        path = self._secret_path(provider)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(api_key, encoding="utf-8")
        path.chmod(0o600)

    def _file_load(self, provider: str) -> str:
        path = self._secret_path(provider)
        if not path.is_file():
            return ""
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return value if 10 <= len(value) <= 16384 else ""

    @staticmethod
    def _keychain_store(provider: str, api_key: str) -> None:
        """Persist the key in the login keychain so a restart need not ask again.

        The key never touches a file in this repository, a log, or a response.
        """
        try:
            subprocess.run(
                [
                    "/usr/bin/security",
                    "add-generic-password",
                    "-U",
                    "-s",
                    f"local.orchestragent.{provider}",
                    "-a",
                    provider,
                    "-w",
                    api_key,
                ],
                check=False,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def _keychain_load(self, provider: str) -> str:
        try:
            result = subprocess.run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    f"local.orchestragent.{provider}",
                    "-a",
                    provider,
                    "-w",
                ],
                check=False,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if result.returncode != 0:
            return ""
        value = result.stdout.decode("utf-8", "replace").strip()
        return value if 10 <= len(value) <= 16384 else ""

    def save_secret(self, name: str, value: str) -> None:
        """Store a bring-your-own key for a registered provider (Keychain on macOS, else a
        0600 file in the data directory). Never logged, never in the state database."""
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
            raise ValueError("provider names are lower-case letters, digits, - and _")
        if not 10 <= len(value) <= 16384 or any(not 33 <= ord(c) <= 126 for c in value):
            raise ValueError("Enter the API key locally (10-16384 visible characters)")
        if is_macos() and self._persist():
            self._keychain_store(f"byok-{name}", value)
        else:
            self._file_store(f"byok-{name}", value)

    def load_secret(self, name: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
            return ""
        return (self._keychain_load(f"byok-{name}") if is_macos() and self._persist()
                else self._file_load(f"byok-{name}"))

    def restore_keys(self) -> None:
        """Reload persisted keys for providers that already have a budget grant."""
        if not self._persist():
            return
        with self.store.transaction() as db:
            rows = db.execute("SELECT provider FROM provider_authorization").fetchall()
        for row in rows:
            provider = str(row[0])
            with self._lock, self.store.transaction() as db:
                value = self._keychain_load(provider) if is_macos() else self._file_load(provider)
                if not value:
                    continue
                generation = ""
                key = value
                if provider == "dsh":
                    try:
                        envelope = json.loads(value)
                    except ValueError:
                        envelope = None
                    if (isinstance(envelope, dict)
                            and set(envelope) == {"schema_version", "generation", "key"}
                            and type(envelope["schema_version"]) is int
                            and envelope["schema_version"] == 1
                            and isinstance(envelope["generation"], str)
                            and re.fullmatch(r"[a-f0-9]{32}", envelope["generation"])
                            and isinstance(envelope["key"], str)):
                        key, generation = envelope["key"], envelope["generation"]
                if (not 10 <= len(key) <= 4096
                        or (provider == "dsh" and any(not 33 <= ord(c) <= 126 for c in key))):
                    continue
                if provider == "dsh":
                    current = db.execute(
                        "SELECT generation FROM provider_credentials WHERE provider=?", (provider,)
                    ).fetchone()
                    if not generation or current is None or current[0] != generation:
                        # A legacy raw key (or interrupted store update) has no trustworthy
                        # binding. Upgrade the same key without inheriting old proof.
                        generation = uuid.uuid4().hex
                        db.execute("INSERT INTO provider_credentials VALUES(?,?) "
                                   "ON CONFLICT(provider) DO UPDATE SET "
                                   "generation=excluded.generation",
                                   (provider, generation))
                        db.execute("DELETE FROM provider_authentication WHERE provider=?",
                                   (provider,))
                        self._store_credential(provider, key, generation)
                self._keys[provider] = key
                self._generations[provider] = generation

    def credential_generation(self, provider: str) -> str:
        """Opaque non-secret identity of the currently loaded DSF credential."""
        with self._lock, self.store.transaction() as db:
            generation = self._generations.get(provider, "")
            current = db.execute("SELECT generation FROM provider_credentials WHERE provider=?",
                                 (provider,)).fetchone()
            if self._keys.get(provider) and current and current[0] == generation:
                return generation
            return ""

    def record_auth(self, provider: str, model: str, transport: str, generation: str,
                    outcome: str) -> bool:
        """Record authoritative actual-call evidence, ignoring replaced credentials."""
        if (provider != "dsh" or not model or not valid_model(provider, model)
                or transport not in {"api", "cli"} or outcome not in {"ok", "auth_required"}):
            raise ValueError("Invalid DSF authentication evidence route/outcome")
        with self._lock, self.store.transaction() as db:
            current = db.execute("SELECT generation FROM provider_credentials WHERE provider=?",
                                 (provider,)).fetchone()
            if (not generation or self._generations.get(provider) != generation
                    or current is None or current[0] != generation):
                return False
            db.execute("INSERT INTO provider_authentication VALUES(?,?,?,?,?,?) "
                       "ON CONFLICT(provider,model,transport) DO UPDATE SET "
                       "generation=excluded.generation, outcome=excluded.outcome, "
                       "observed_at=excluded.observed_at",
                       (provider, model, transport, generation, outcome, time.time()))
        return True

    def authentication(self, provider: str, model: str, transport: str) -> dict[str, object]:
        """Local proof for this loaded generation and route; never credential material."""
        if (provider != "dsh" or not model or not valid_model(provider, model)
                or transport not in {"api", "cli", "cli-account"}):
            raise ValueError("Invalid DSF authentication route")
        with self._lock, self.store.transaction() as db:
            generation = self._generations.get(provider, "")
            current = db.execute("SELECT generation FROM provider_credentials WHERE provider=?",
                                 (provider,)).fetchone()
            if not self._keys.get(provider) or current is None or current[0] != generation:
                generation = ""
            row = db.execute("SELECT outcome,observed_at FROM provider_authentication "
                             "WHERE provider=? AND model=? AND transport=? AND generation=?",
                             (provider, model, transport, generation)).fetchone()
        state = str(row[0]) if row else "unverified"
        return {"state": "verified" if state == "ok" else state, "verified": state == "ok",
                "generation": generation, "model": model, "transport": transport,
                "observed_at": row[1] if row else None}

    def key(self, provider: str) -> str:
        with self._lock:
            return self._keys.get(provider, "")

    def status(self, provider: str) -> dict[str, object]:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT cap_cents FROM provider_authorization WHERE provider=?", (provider,)
            ).fetchone()
            reserved = db.execute(
                "SELECT COALESCE(SUM(cents),0) FROM provider_reservations WHERE provider=?",
                (provider,),
            ).fetchone()[0]
            spent = db.execute(
                "SELECT COALESCE(SUM(cents),0) FROM provider_spend WHERE provider=?",
                (provider,),
            ).fetchone()[0]
        return {
            "authorized": row is not None,
            "spent_cents": spent,
            "credential_ready": bool(self.key(provider)),
            "cap_cents": row[0] if row else 0,
            "reserved_cents": reserved,
            "currency": "CNY",
        }

    def release(self, run_id: str) -> None:
        """A failed attempt never spent the money; free its reservation."""
        with self.store.transaction() as db:
            db.execute("DELETE FROM provider_reservations WHERE run_id=?", (run_id,))

    def settle(self, run_id: str, provider: str, cents: int) -> None:
        """A completed attempt consumes budget; keep it as cumulative spend."""
        with self.store.transaction() as db:
            db.execute("DELETE FROM provider_reservations WHERE run_id=?", (run_id,))
            db.execute(
                "INSERT INTO provider_spend(run_id, provider, cents) VALUES(?,?,?)"
                " ON CONFLICT(run_id) DO NOTHING",
                (run_id, provider, cents),
            )

    def reserve(self, provider: str, run_id: str, cents: int) -> bool:
        if cents not in BUDGETED.get(provider, frozenset()):
            raise ValueError("Unknown bounded provider reservation")
        with self.store.transaction() as db:
            if db.execute(
                "SELECT 1 FROM provider_reservations WHERE run_id=?", (run_id,)
            ).fetchone() or db.execute(
                "SELECT 1 FROM provider_spend WHERE run_id=?", (run_id,)
            ).fetchone():
                raise Conflict("This native attempt was already reserved; cannot replay")
            # A configured key is the only precondition. There is no local spending cap: the
            # provider's own balance decides (HTTP 402 -> quota_exhausted stops dispatch).
            # Reservations are kept only to account for in-flight exposure.
            if db.execute(
                "SELECT 1 FROM provider_authorization WHERE provider=?", (provider,)
            ).fetchone() is None:
                return False
            db.execute("INSERT INTO provider_reservations VALUES(?,?,?)", (run_id, provider, cents))
        return True
