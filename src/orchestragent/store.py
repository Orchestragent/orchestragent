"""SQLite transactions keep task state and its audit event inseparable."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from orchestragent.models import Approval, Artifact, Event, Step, Task
from orchestragent.serialization import dumps


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.draining = False
        self.active_operations = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                    status TEXT NOT NULL, created_at REAL NOT NULL, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    kind TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_task ON events(task_id, seq);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(body: str) -> Task:
        data = json.loads(body)
        data["steps"] = [Step(**step) for step in data["steps"]]
        data["artifacts"] = [Artifact(**item) for item in data["artifacts"]]
        data["approval"] = Approval(**data["approval"]) if data["approval"] else None
        return Task(**data)

    @contextmanager
    def activity(self) -> Iterator[bool]:
        """Admit work atomically with the restart gate; count it through cleanup."""
        with self.transaction():
            admitted = not self.draining
            if admitted:
                self.active_operations += 1
        try:
            yield admitted
        finally:
            if admitted:
                with self.transaction():
                    self.active_operations -= 1

    def drain(self, enabled: bool) -> None:
        """Freeze admission only after every conversation and task is idle."""
        with self.transaction() as db:
            if enabled:
                tasks = db.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status IN ('queued', 'running')"
                ).fetchone()[0]
                has_chat = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_messages'"
                ).fetchone()
                chats = db.execute(
                    "SELECT COUNT(*) FROM chat_messages WHERE status IN ('pending', 'running')"
                ).fetchone()[0] if has_chat else 0
                if self.active_operations or tasks or chats:
                    raise Conflict("Work is still in flight; refusing to drain")
            self.draining = enabled

    def get(self, db: sqlite3.Connection, task_id: str) -> Task:
        row = db.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError("Task not found")
        return self.decode(row["body"])

    def all(self, db: sqlite3.Connection) -> list[Task]:
        return [
            self.decode(row["body"])
            for row in db.execute("SELECT body FROM tasks ORDER BY created_at DESC, id")
        ]

    @staticmethod
    def save(db: sqlite3.Connection, task: Task) -> None:
        db.execute(
            "INSERT INTO tasks VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "status=excluded.status, body=excluded.body",
            (task.id, task.request_id, task.status, task.created_at, dumps(asdict(task))),
        )

    @staticmethod
    def event(db: sqlite3.Connection, task: Task, kind: str, data: dict[str, object]) -> None:
        db.execute(
            "INSERT INTO events(task_id,kind,at,data) VALUES(?,?,?,?)",
            (task.id, kind, task.updated_at, dumps(data)),
        )

    def read(self, task_id: str) -> Task:
        with self.transaction() as db:
            return self.get(db, task_id)

    def list_tasks(self) -> list[Task]:
        with self.transaction() as db:
            return self.all(db)

    def events(self, task_id: str, after: int = 0) -> list[Event]:
        with self.transaction() as db:
            self.get(db, task_id)
            return [
                Event(row["seq"], row["task_id"], row["kind"], row["at"], json.loads(row["data"]))
                for row in db.execute(
                    "SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 200",
                    (task_id, after),
                )
            ]
