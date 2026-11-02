"""Bounded native-subscription cache comparison. Commit before running --live.

Eight calls: flat versus explicit context block, each with A,A,B,B. A and B share
source context but have different final questions. No API or session persistence.
"""

import argparse
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from orchestragent.envelope import digest
from orchestragent.native import ContextPrompt, NativeClients
from orchestragent.orch_usage import Call, Ledger, report, unresolved
from orchestragent.serialization import TEXT, dumps, object_schema
from orchestragent.store import Store
from orchestragent.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = object_schema({"answer": TEXT})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--live", action="store_true")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--schema-switch", action="store_true",
                       help="Compare role-specific versus shared context across two schemas")
    modes.add_argument("--shared-schema", action="store_true",
                       help="Four calls switching operation with shared context and schema")
    modes.add_argument("--task-prefix", action="store_true",
                       help="Four calls: two operations on task A, then task B; the task "
                       "block ends the shared context, so call 3 shows what survives a task change")
    args = parser.parse_args()
    prefix = "Read the following source. Answer only the final question as JSON.\n"
    for name in ("orch_build.py", "orch_ladder.py"):
        prefix += f"\n## {name}\n" + (ROOT / "src/orchestragent" / name).read_text()
    questions = [("What is PARK? Answer with the integer as a string.", "6")] * 2
    questions += [("What is MAX_SAMPLES? Answer with the integer as a string.", "4")] * 2
    tasks = [(ROOT / "src/orchestragent" / name).read_text()
             for name in ("orch_review.py", "orch_journal.py")]
    if args.task_prefix:
        questions = [questions[0], questions[2]] * 2  # operation switch within each task
    if not args.live:
        print(dumps({"calls": 4 if args.shared_schema or args.task_prefix else 8,
                     "source_prefix_bytes": len(prefix.encode()), "live": False}))
        return
    Workspace.git(ROOT, "ls-files", "--error-unmatch", str(Path(__file__).relative_to(ROOT)))
    if Workspace.git(ROOT, "status", "--porcelain", "--", "src", "scripts"):
        raise ValueError("Commit source and measurement script before execution")
    args.output.mkdir(parents=True, exist_ok=False)
    ledger = Ledger(Store(args.output / "calls.sqlite"), time.time)
    revision = Workspace.git(ROOT, "rev-parse", "HEAD")
    run_id = "cache-" + str(time.time_ns())
    validation: list[dict[str, object]] = []
    version = ""
    try:
        arms = (("shared-schema",) if args.shared_schema else ("task-prefix",)
                if args.task_prefix else
                ("roles", "shared") if args.schema_switch else ("flat", "context"))
        for arm in arms:
            client = NativeClients(args.output / arm, timeout=90)
            try:
                if not version:
                    version = subprocess.check_output(
                        [str(client.executables["claude"]), "--version"],
                        env=client._environment(), text=True, timeout=10,
                    ).strip()
                for number, (question, expected) in enumerate(questions, 1):
                    schema = SCHEMA
                    field = "answer"
                    prompt = prefix + "\n## QUESTION\n" + question
                    if arm == "context":
                        prompt = ContextPrompt(prefix, "\n## QUESTION\n" + question)
                    if args.schema_switch or args.shared_schema:
                        field = "answer" if number <= 2 else "result"
                        schema = object_schema({field: TEXT})
                        if args.shared_schema:
                            schema = object_schema({"answer": TEXT, "result": TEXT})
                        role = "Return the answer." if number <= 2 else "Return the result."
                        shared = f"Cache probe arm: {arm}.\n" + prefix
                        prompt = ContextPrompt(
                            (role + "\n" + shared) if arm == "roles" else shared,
                            role + "\n## QUESTION\n" + question,
                        )
                    if args.task_prefix:
                        field = "answer"
                        schema = object_schema({"answer": TEXT, "result": TEXT})
                        prompt = ContextPrompt(
                            f"Cache probe arm: {arm}.\n" + prefix
                            + "\n## TASK BLOCK\n" + tasks[number > 2],
                            ("Return the answer." if number % 2 else "Return the answer now.")
                            + "\n## QUESTION\n" + question,
                        )
                    call = Call(
                        f"{run_id}-{arm}-{number}", run_id, "claude", "claude-opus-5-5",
                        arm, time.time(), attempt=number, effort="medium",
                        prompt_hash=digest(dumps({"prompt": prompt, "schema": schema})),
                        prefix_hash=digest(prompt.shared if isinstance(prompt, ContextPrompt)
                                           else prefix), request_bytes=len(prompt.encode()),
                    )
                    with ledger.capture(call):
                        result = client.call("claude", call.model, prompt, schema, call.id,
                                             lambda: False, effort=call.effort)
                        call.outcome, call.usage = result.outcome, result.usage
                        call.session_id = result.session_id
                    answer = str(result.data.get(field, "")).strip()
                    correct = result.outcome == "ok" and answer == expected
                    validation.append({"id": call.id, "correct": correct, "answer": answer[:128]})
                    print(dumps({"arm": arm, "number": number, "correct": correct,
                                 "usage": call.usage}), flush=True)
                    if result.outcome != "ok" or unresolved(call.outcome, call.usage):
                        raise RuntimeError("Probe stopped after failed/unknown inference")
            finally:
                client.close()
    finally:
        calls = ledger.calls(run_id)
        (args.output / "report.json").write_text(dumps({
            "engine_revision": revision, "cli_version": version, "validation": validation,
            "context_block": True,
            "schema_switch": args.schema_switch,
            "shared_schema": args.shared_schema,
            "task_prefix": args.task_prefix,
            "usage": report(calls, "purpose"), "calls": [asdict(c) for c in calls],
            "limitations": "One ordered microprobe; not a full project benchmark.",
        }) + "\n")


if __name__ == "__main__":
    main()
