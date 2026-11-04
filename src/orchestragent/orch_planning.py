"""Durable distributed-planning sessions, bound to exact requests and provider receipts."""

import json
from dataclasses import asdict, dataclass, fields

from orchestragent.orch_journal import CallJournal
from orchestragent.serialization import dumps
from orchestragent.store import Store


@dataclass(frozen=True)
class PlanningSession:
    id: str
    spec: str
    base: str
    planner: str
    effort: str
    attempts: int = 3
    test_jobs: int = 4
    environment_root: str = ""
    environment_fingerprint: str = ""
    designer: str = ""
    designer_effort: str = ""
    reviewed_boundary: bool = False
    integration_checks: bool = False  # legacy sessions never gain new paid shards on resume
    integration_scope: str = "terminal"  # legacy resumes must not acquire additional paid work

    def __post_init__(self) -> None:
        if (not isinstance(self.integration_scope, str)
                or self.integration_scope not in {"terminal", "joins"}):
            raise ValueError("Unknown pinned integration scope")


class PlanningStore(CallJournal):
    def __init__(self, store: Store):
        with store.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS orch_planning_sessions "
                       "(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        super().__init__(store, "planning")

    def create(self, session: PlanningSession) -> None:
        with self.store.transaction() as db:
            db.execute("INSERT INTO orch_planning_sessions VALUES (?,?)",
                       (session.id, dumps(asdict(session))))

    def session(self, run_id: str) -> PlanningSession:
        with self.store.transaction() as db:
            row = db.execute("SELECT body FROM orch_planning_sessions WHERE id=?",
                             (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"No resumable planning session {run_id}")
        body = json.loads(row["body"])
        known = {item.name for item in fields(PlanningSession)}
        return PlanningSession(**{key: value for key, value in body.items() if key in known})
