"""Remove Mock tasks and their events from a live service database.

Mock tasks only exist to exercise the state machine; on a Mac used for real
work they clutter the history. Live tasks and settings are untouched.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(".data/tasks.sqlite"))
    parser.add_argument("--apply", action="store_true", help="Delete instead of counting")
    args = parser.parse_args()
    if not args.db.is_file():
        print(f"missing database: {args.db}", file=sys.stderr)
        return 2
    connection = sqlite3.connect(args.db)
    try:
        rows = connection.execute("SELECT id, body FROM tasks").fetchall()
        mock = [
            row[0]
            for row in rows
            if (json.loads(row[1]).get("mode") or "mock") != "live"
        ]
        print(f"tasks={len(rows)} mock={len(mock)}")
        if not args.apply:
            print("dry run; pass --apply to delete")
            return 0
        with connection:
            for task_id in mock:
                connection.execute("DELETE FROM events WHERE task_id = ?", (task_id,))
                connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        print(f"deleted {len(mock)} mock tasks")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
