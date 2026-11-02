"""Project memory: durable notes that let a later vendor continue without the session.

Retrieval is lexical and local. A personal workspace has hundreds of stage notes,
not millions of documents, so a scored scan over recent rows beats any index we
would have to keep consistent, and it needs no tokenizer assumptions about CJK.
"""

import re
import sqlite3
import time
from dataclasses import dataclass

from orchestragent.serialization import dumps
from orchestragent.store import Store

STOP = frozenset({"the", "and", "for", "with", "that", "this", "from", "一个", "我们"})
TERM = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]")


@dataclass(frozen=True)
class Note:
    project: str
    kind: str
    title: str
    body: str
    task_id: str = ""
    provider: str = ""
    model: str = ""


class MemoryStore:
    scan_limit = 500
    context_bytes = 6000

    def __init__(self, store: Store) -> None:
        self.store = store
        with store.transaction() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL)"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS memory_project ON memory(project, id DESC)")

    def remember(self, note: Note, db: sqlite3.Connection | None = None) -> int:
        """Write one note. Pass an open connection to join the caller's transaction."""
        if db is not None:
            return self._insert(db, note)
        with self.store.transaction() as connection:
            return self._insert(connection, note)

    def _insert(self, db: sqlite3.Connection, note: Note) -> int:
        # One note per (project, task, stage): a reworked stage replaces its stale copy.
        if note.task_id:
            db.execute(
                "DELETE FROM memory WHERE project = ? AND task_id = ? AND kind = ?"
                " AND title LIKE ?",
                (note.project, note.task_id, note.kind, f"{note.title.split(':', 1)[0]}:%"),
            )
        cursor = db.execute(
            "INSERT INTO memory(project, kind, title, body, task_id, provider, model,"
            " created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                note.project,
                note.kind,
                note.title[:200],
                note.body[:8000],
                note.task_id,
                note.provider,
                note.model,
                time.time(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def recall(self, project: str, query: str, limit: int = 5) -> list[dict[str, object]]:
        terms = {term.lower() for term in TERM.findall(query)} - STOP
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT id, kind, title, body, provider, model, created_at FROM memory"
                " WHERE project = ? ORDER BY id DESC LIMIT ?",
                (project, self.scan_limit),
            ).fetchall()
        scored: list[tuple[float, tuple[object, ...]]] = []
        rows = [tuple(row) for row in rows]
        for index, row in enumerate(rows):
            text = f"{row[2]} {row[3]}".lower()
            hits = sum(1 for term in terms if term in text)
            if not hits:
                continue
            recency = 1.0 / (1.0 + index / 40.0)
            kind_bonus = 0.5 if row[1] in {"decision", "review"} else 0.0
            scored.append((hits + recency + kind_bonus, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        notes: list[dict[str, object]] = []
        for _, row in scored[:limit]:
            notes.append(
                {
                    "kind": str(row[1]),
                    "title": str(row[2]),
                    "body": str(row[3])[:1200],
                    "provider": str(row[4]),
                    "model": str(row[5]),
                }
            )
        return notes

    def context(self, project: str, query: str, limit: int = 5) -> str:
        notes = self.recall(project, query, limit)
        trimmed = []
        used = 0
        for note in notes:
            size = len(str(note.get("body", "")).encode())
            if used + size > self.context_bytes:
                break
            trimmed.append(note)
            used += size
        if not trimmed:
            return ""
        return dumps({"project_memory": trimmed})

    def backfill(self) -> int:
        """Seed notes from tasks recorded before the memory store existed."""
        import json

        with self.store.transaction() as db:
            existing = db.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
            if existing:
                return 0
            rows = db.execute("SELECT id, body FROM tasks").fetchall()
        added = 0
        for task_id, body in rows:
            value = json.loads(body)
            if value.get("mode") != "live":
                continue
            project = str(value.get("project") or "default")
            prompt = str(value.get("prompt", ""))
            for step in value.get("steps", []):
                if not isinstance(step, dict) or step.get("status") != "completed":
                    continue
                summary = str(step.get("summary", "") or "")
                if not summary:
                    continue
                self.remember(
                    Note(
                        project=str(project or "default"),
                        kind="stage",
                        title=f"{step.get('role')}: {str(prompt)[:60]}",
                        body=summary,
                        task_id=str(task_id),
                        provider=str(step.get("provider", "") or ""),
                        model=str(step.get("model", "") or ""),
                    )
                )
                added += 1
        return added

    def compact(self, keep_per_project: int = 200) -> int:
        """Bound growth: drop the oldest notes beyond the cap for each project."""
        removed = 0
        with self.store.transaction() as db:
            projects = [
                row[0] for row in db.execute("SELECT DISTINCT project FROM memory").fetchall()
            ]
            for project in projects:
                rows = db.execute(
                    "SELECT id FROM memory WHERE project = ? ORDER BY id DESC LIMIT -1 OFFSET ?",
                    (project, keep_per_project),
                ).fetchall()
                for row in rows:
                    db.execute("DELETE FROM memory WHERE id = ?", (row[0],))
                    removed += 1
        return removed

    def forget(self, project: str, keyword: str) -> int:
        """The agent may drop notes it judges wrong, scoped to one project."""
        term = keyword.strip()
        if not term:
            return 0
        with self.store.transaction() as db:
            cursor = db.execute(
                "DELETE FROM memory WHERE project = ? AND (title LIKE ? OR body LIKE ?)",
                (project, f"%{term}%", f"%{term}%"),
            )
            return int(cursor.rowcount)

    def stats(self, project: str = "") -> dict[str, object]:
        with self.store.transaction() as db:
            if project:
                total = db.execute(
                    "SELECT COUNT(*) FROM memory WHERE project = ?", (project,)
                ).fetchone()[0]
            else:
                total = db.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
            projects = db.execute("SELECT COUNT(DISTINCT project) FROM memory").fetchone()[0]
        return {"notes": int(total), "projects": int(projects)}

    def recent(self, project: str = "", limit: int = 20) -> list[dict[str, object]]:
        with self.store.transaction() as db:
            if project:
                rows = db.execute(
                    "SELECT kind, title, body, provider, model, created_at FROM memory"
                    " WHERE project = ? ORDER BY id DESC LIMIT ?",
                    (project, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT kind, title, body, provider, model, created_at FROM memory"
                    " ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            {
                "kind": row[0],
                "title": row[1],
                "body": row[2][:400],
                "provider": row[3],
                "model": row[4],
                "at": row[5],
            }
            for row in rows
        ]
