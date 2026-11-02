"""Provider heartbeat: a cheap liveness check that never spends quota."""

import time

from orchestragent.providers import ProviderClients
from orchestragent.store import Store


class HealthMonitor:
    """Records one row per provider per probe; `status()` is the readiness probe.

    For native CLIs that is the CLI's own `auth status`; for API routes it is the
    local authorization and credential presence. No inference is requested.
    """

    history = 20

    def __init__(self, store: Store) -> None:
        self.store = store
        with store.transaction() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS provider_health (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                at REAL NOT NULL,
                ok INTEGER NOT NULL,
                auth TEXT NOT NULL DEFAULT '',
                latency_ms INTEGER NOT NULL DEFAULT 0,
                detail TEXT NOT NULL DEFAULT '')"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS health_provider ON provider_health(provider, id DESC)"
            )

    def probe(self, clients: ProviderClients) -> dict[str, object]:
        started = time.monotonic()
        status = clients.status()
        elapsed = int((time.monotonic() - started) * 1000)
        now = time.time()
        with self.store.transaction() as db:
            for provider, entry in status.items():
                if not isinstance(entry, dict):
                    continue
                ready = entry.get("ready") is True
                db.execute(
                    "INSERT INTO provider_health(provider, at, ok, auth, latency_ms, detail)"
                    " VALUES(?,?,?,?,?,?)",
                    (
                        provider,
                        now,
                        1 if ready else 0,
                        str(entry.get("auth", ""))[:40],
                        elapsed,
                        str(entry.get("message", ""))[:200],
                    ),
                )
        return status

    def latest(self) -> list[dict[str, object]]:
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT provider, at, ok, auth, latency_ms, detail FROM provider_health"
                " WHERE id IN (SELECT MAX(id) FROM provider_health GROUP BY provider)"
                " ORDER BY provider"
            ).fetchall()
        return [
            {
                "provider": row[0],
                "at": row[1],
                "ok": bool(row[2]),
                "auth": row[3],
                "latency_ms": row[4],
                "detail": row[5],
            }
            for row in rows
        ]

    def failing_streak(self, provider: str) -> int:
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT ok FROM provider_health WHERE provider = ? ORDER BY id DESC LIMIT 3",
                (provider,),
            ).fetchall()
        streak = 0
        for row in rows:
            if row[0]:
                break
            streak += 1
        return streak
