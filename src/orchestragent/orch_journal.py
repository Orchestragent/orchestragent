"""Exact-request response checkpoints sharing the provider ledger's settlement transaction."""

import json
import sqlite3
from dataclasses import asdict, replace
from typing import Literal

from orchestragent.envelope import digest
from orchestragent.native import NativeResult
from orchestragent.orch_usage import Call, settled_json_failure, unresolved, usage_fields
from orchestragent.serialization import dumps
from orchestragent.store import Conflict, Store


class CallJournal:
    def __init__(self, store: Store, kind: Literal["planning", "review"]):
        if kind not in {"planning", "review"}:
            raise ValueError("Unknown response journal domain")
        self.store, self.kind = store, kind
        self.calls_table, self.ack_table = f"orch_{kind}_calls", f"orch_{kind}_ack"
        parent = "orch_planning_sessions" if kind == "planning" else "orch_runs"
        with store.transaction() as db:
            db.executescript(f"""
                CREATE TABLE IF NOT EXISTS {self.calls_table} (
                    run_id TEXT NOT NULL REFERENCES {parent}(id),
                    phase TEXT NOT NULL, request TEXT NOT NULL, call_id TEXT NOT NULL,
                    response TEXT, PRIMARY KEY(run_id, phase));
                CREATE TABLE IF NOT EXISTS {self.ack_table} (
                    run_id TEXT NOT NULL, call_id TEXT PRIMARY KEY, at INTEGER NOT NULL);
            """)

    @staticmethod
    def phase(call: Call) -> str:
        return dumps([call.purpose, call.task_id, call.sample, call.attempt])

    def acknowledge(self, run_id: str, call_ids: list[str]) -> None:
        """Record explicit authorization without rewriting any unknown provider receipt."""
        with self.store.transaction() as db:
            for call_id in call_ids:
                row = db.execute(f"SELECT c.body FROM orch_calls c JOIN {self.calls_table} p "
                                 "ON c.id=p.call_id WHERE p.run_id=? AND c.id=?",
                                 (run_id, call_id)).fetchone()
                if row is None:
                    raise Conflict(f"{call_id} is not an active call of this {self.kind} session")
                call = Call(**json.loads(row["body"]))
                if not unresolved(call.outcome, call.usage):
                    raise Conflict(f"{call_id} is already settled; no repeat authorization needed")
                db.execute(f"INSERT OR IGNORE INTO {self.ack_table} VALUES (?,?,unixepoch())",
                           (run_id, call_id))

    def claim(self, call: Call) -> tuple[NativeResult, str] | None:
        """Reuse settled responses; never guess whether an unfinished dispatch was billed."""
        phase = self.phase(call)
        request = digest(dumps([call.provider, call.model, call.effort,
                                call.base_revision, call.prompt_hash]))
        with self.store.transaction() as db:
            row = db.execute(f"SELECT * FROM {self.calls_table} WHERE run_id=? AND phase=?",
                             (call.run_id, phase)).fetchone()
            if row is not None:
                if row["request"] != request:
                    raise Conflict(f"{self.kind.title()} request changed; preserve this session "
                                   "and start a new operation")
                saved = db.execute("SELECT body FROM orch_calls WHERE id=?",
                                   (row["call_id"],)).fetchone()
                if saved is not None:
                    receipt = Call(**json.loads(saved["body"]))
                    acknowledged = db.execute(f"SELECT 1 FROM {self.ack_table} "
                                              "WHERE run_id=? AND call_id=?",
                                              (call.run_id, receipt.id)).fetchone()
                    if unresolved(receipt.outcome, receipt.usage) and not acknowledged:
                        action = ("--ack-unknown-call ID" if self.kind == "planning" else
                                  "orch retry RUN TASK --ack-unknown")
                        raise Conflict(f"Unresolved {self.kind} call {receipt.id}; "
                                       "inspect orch cost "
                                       f"and use {action} to authorize another request")
                    if row["response"] is not None:
                        result = NativeResult(**json.loads(row["response"]))
                        # Definite provider refusals may be retried by an explicit resume.
                        # Settled malformed JSON stays cached so the normal bounded repair
                        # loop can advance to its next phase without buying it again.
                        if result.outcome == "ok" or settled_json_failure(
                            result.outcome, result.message, result.usage,
                        ):
                            return result, receipt.id
                    elif receipt.outcome == "ok":
                        raise Conflict(f"Settled {self.kind} response missing; inspect evidence")
                # No receipt means the process stopped before capture admitted dispatch.
            db.execute(f"INSERT INTO {self.calls_table} VALUES (?,?,?,?,NULL) "
                       "ON CONFLICT(run_id, phase) DO UPDATE SET "
                       "call_id=excluded.call_id, response=NULL",
                       (call.run_id, phase, request, call.id))
        return None

    def finish(self, db: sqlite3.Connection, call: Call, result: NativeResult | None) -> None:
        """Called inside Ledger.capture's final transaction, after receipt settlement."""
        if result is None:
            return
        saved = replace(result, usage=usage_fields(result.usage))
        cursor = db.execute(f"UPDATE {self.calls_table} SET response=? "
                            "WHERE run_id=? AND phase=? AND call_id=?",
                            (dumps(asdict(saved)), call.run_id, self.phase(call), call.id))
        if cursor.rowcount != 1:
            raise Conflict(f"{self.kind.title()} phase no longer owns this provider response")
