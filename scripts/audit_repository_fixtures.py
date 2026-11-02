"""Replay static fixture/frontier gates against a saved repository trial; no model calls."""

import argparse
import json
from pathlib import Path

from orchestragent.envelope import digest
from orchestragent.live_workspace import LiveWorkspace
from orchestragent.orch_graph import replacement_frontier
from orchestragent.orch_testlint import fixture_errors
from orchestragent.serialization import dumps
from orchestragent.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trial", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sources = [Path(__file__), ROOT / "src/orchestragent/orch_graph.py",
               ROOT / "src/orchestragent/orch_testlint.py"]
    if Workspace.git(ROOT, "status", "--porcelain", "--", *(str(p) for p in sources)):
        raise ValueError("Commit the audit script and static gates before replay")
    saved = json.loads((args.trial / "report.json").read_text())
    events = json.loads((args.trial / "events.json").read_text())
    repo = args.trial / "project"
    tag = f"orch/{saved['run']['id']}/plan"
    added = Workspace.git(repo, "diff", "--name-only", saved["evaluation_base"], tag).splitlines()
    diagnostics = {}
    for name in added:
        if name.endswith(".py") and LiveWorkspace.is_test_file(name):
            text = Workspace.git(repo, "show", f"{tag}:{name}", strip=False)
            diagnostics[name] = {"source_digest": digest(text), "errors": fixture_errors(text)}
    tasks = {task["id"]: task for task in saved["tasks"]}
    groups: dict[tuple[str, ...], set[str]] = {}
    for event in events:
        if event["kind"] == "task.replanned":
            into = tuple(event["data"]["into"])
            groups.setdefault(into, set()).update(tasks[event["task_id"]]["files"])
    frontiers = [{"replacement_ids": list(into), "original_files": sorted(original),
                  "errors": replacement_frontier(original,
                      {tid: tasks[tid]["depends_on"] for tid in into},
                      {tid: tasks[tid]["files"] for tid in into})}
                 for into, original in groups.items()]
    output = {"validator_revision": Workspace.git(ROOT, "rev-parse", "HEAD"),
              "validator_sources": {p.relative_to(ROOT).as_posix(): digest(p.read_text())
                                    for p in sources},
              "trial_digest": digest((args.trial / "report.json").read_text()),
              "plan_revision": Workspace.git(repo, "rev-parse", tag),
              "fixtures": diagnostics, "replacement_frontiers": frontiers,
              "scope": "static replay of original frozen files and recorded replacement graph; "
                       "not a new execution or delivery result"}
    with args.output.open("x") as stream:
        stream.write(dumps(output) + "\n")


if __name__ == "__main__":
    main()
