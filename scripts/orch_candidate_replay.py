"""Replay candidate structure checks on saved Git artifacts; no models or execution.

Commit this script and source before running. Historical token/time savings cannot
be inferred from rejected candidates; this measures detection and final-code acceptance.
"""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from orchestragent.orch_build import implementation_errors, interface_errors, parse_trailers
from orchestragent.orch_index import Index
from orchestragent.orch_plan import plan_files
from orchestragent.serialization import dumps
from orchestragent.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trial", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    Workspace.git(ROOT, "ls-files", "--error-unmatch", str(Path(__file__).relative_to(ROOT)))
    if Workspace.git(ROOT, "status", "--porcelain", "--", "src", "scripts"):
        raise ValueError("Commit source and replay script first")
    if args.output.exists():
        raise ValueError("Replay output already exists")
    report = json.loads((args.trial / "report.json").read_text())
    run = report["run"]
    repo = args.trial / "project"
    tasks = {task["id"]: task for task in report["tasks"]}
    required = plan_files(run["plan"])
    commits = sorted(set(Workspace.git(
        repo, "for-each-ref", "--format=%(objectname)", f"refs/orch/{run['id']}/",
    ).splitlines()))
    records = []
    with TemporaryDirectory(dir=args.trial, prefix="candidate-replay-") as temporary:
        index = Index(repo, Path(temporary) / "index.sqlite")
        for commit, final in [*[(item, False) for item in commits], (run["verified_head"], True)]:
            identity = parse_trailers(Workspace.git(repo, "show", "-s", "--format=%B", commit))
            tid = identity.get("Orch-Task", "")
            if not final and (identity.get("Orch-Run") != run["id"] or tid not in tasks):
                continue
            paths = sorted({p for task in tasks.values() if final or task["id"] == tid
                            for p in task["files"] if p.endswith(".py")})
            before = index.read(run["base"] if final else commit + "^", paths)
            current = index.read(commit, paths)
            errors = {p: [*interface_errors(required.get(p, ""), current.get(p, "")),
                          *implementation_errors(before.get(p, ""), current.get(p, ""),
                                                 required.get(p, ""))] for p in paths}
            records.append({"commit": commit, "task": tid, "final": final,
                            "attempt": identity.get("Orch-Attempt", ""),
                            "errors": {p: messages for p, messages in errors.items() if messages}})
    candidates = [record for record in records if not record["final"]]
    result = {"engine_revision": Workspace.git(ROOT, "rev-parse", "HEAD"),
              "historical_engine": report["engine_revision"], "run_id": run["id"],
              "model_calls": 0, "candidates": len(candidates),
              "rejected": sum(bool(record["errors"]) for record in candidates),
              "final_accepted": all(not record["errors"] for record in records if record["final"]),
              "records": records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(dumps(result) + "\n")
    print(dumps({key: value for key, value in result.items() if key != "records"}))


if __name__ == "__main__":
    main()
