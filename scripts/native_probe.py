"""One explicit subscription-model probe; save sanitized receipts, never raw output."""

import argparse
import time
import uuid
from pathlib import Path

from orchestragent.envelope import digest
from orchestragent.native import NativeClients
from orchestragent.orch_usage import Call, Ledger, usage_fields
from orchestragent.serialization import dumps, object_schema
from orchestragent.store import Store
from orchestragent.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if Workspace.git(ROOT, "status", "--porcelain", "--", "scripts/native_probe.py"):
        parser.error("Commit the probe script before execution")
    args.output.mkdir(parents=True, exist_ok=False)
    client = NativeClients(args.output / "native", timeout=300)
    ledger = Ledger(Store(args.output / "calls.sqlite"), time.time)
    prompt = 'Return {"answer":"ok"} only.'
    schema = object_schema({"answer": {"type": "string", "enum": ["ok"]}})
    call_id = f"probe-{uuid.uuid4().hex}"
    request = dumps({"prompt": prompt, "schema": schema})
    receipt = Call(call_id, call_id, "claude", args.model, "model-probe", time.time(),
                   effort=args.effort, scope="probe", prompt_hash=digest(request),
                   request_bytes=len(request.encode()))
    try:
        with ledger.capture(receipt):
            result = client.call("claude", args.model, prompt, schema, call_id,
                                 lambda: False, effort=args.effort)
            receipt.outcome, receipt.usage = result.outcome, usage_fields(result.usage)
            receipt.session_id = result.session_id
        verified = (result.outcome == "ok" and result.data == {"answer": "ok"}
                    and result.usage.get("served_model") == args.model)
        report = {
            "requested_model": args.model, "effort": args.effort,
            "outcome": result.outcome, "verified": verified,
            "usage": usage_fields(result.usage),
            "source_revision": Workspace.git(ROOT, "rev-parse", "HEAD"),
        }
        output = dumps(report) + "\n"
        (args.output / "result.json").write_text(output)
        print(output, end="")
        return 0 if verified else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
