"""One bounded Cursor check: subscription-pool model, ask mode, JSON result only."""

import sys
from pathlib import Path

from orchestragent.cursor import CursorClient
from orchestragent.native import NativeClients
from orchestragent.serialization import object_schema

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = object_schema(
    {"answer": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}}
)


def main() -> int:
    client = CursorClient(NativeClients(ROOT / ".data" / "native"), ROOT / ".data")
    print("status", client.status())
    result = client.call(
        "composer-2.5",
        "Return JSON with answer set to 'ok' and files set to an empty list. Nothing else.",
        SCHEMA,
        "cursor-smoke",
        lambda: False,
    )
    print("outcome", result.outcome, "session", bool(result.session_id), "usage", result.usage)
    print("data", result.data)
    print("message", result.message[:200])
    return 0 if result.outcome == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
