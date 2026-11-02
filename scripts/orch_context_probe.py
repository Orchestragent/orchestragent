"""Pinned-context diagnostics: offline planning audit or bounded DSF protocol smoke."""

import argparse
from collections.abc import Callable
from pathlib import Path

from orchestragent.live_workspace import LiveWorkspace
from orchestragent.native import NativeResult
from orchestragent.orch_cli import providers
from orchestragent.orch_context import planning_sources, requested_sources
from orchestragent.orch_index import Index
from orchestragent.orch_plan import lint
from orchestragent.orch_run import DSF, Draft, Orchestrator, orch_home
from orchestragent.orch_usage import report
from orchestragent.serialization import dumps
from orchestragent.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
PLAN: dict[str, object] = {
    "summary": "Exercise revision-pinned context retrieval",
    "requirements": [{"id": "R1", "text": "solve(x) returns x plus helper.offset()"}],
    "files": [
        {"path": "solver.py", "content": (
            "def solve(x: int) -> int:\n    raise NotImplementedError\n"
        )},
        {"path": "test_solver.py", "content": (
            "import unittest\nfrom solver import solve\n"
            "class SolverTest(unittest.TestCase):\n"
            "    def test_r1_offset(self):\n        self.assertEqual(solve(5), 12)\n"
        )},
    ],
    "tasks": [{"id": "T-001", "goal": "Implement solve using helper.offset",
               "reqs": ["R1"], "files": ["solver.py"], "depends_on": [],
               "tests": ["test_solver.SolverTest.test_r1_offset"],
               "contract": "First request helper.py::offset via NEED_FILES. Once its source is "
                           "visible, implement solve(x) using helper.offset(). "
                           "Do not edit helper.py."}],
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--planning-file", action="append", default=[])
    parser.add_argument("--excerpt", action="append", default=[], metavar="PATH::SYMBOL",
                        help="also measure source excerpt bytes at the committed engine revision")
    args = parser.parse_args()
    if args.planning_file:
        if args.live or args.repo is None or args.revision is None:
            parser.error("Offline planning audit needs --repo/--revision and cannot use --live")
        if Workspace.git(ROOT, "status", "--porcelain", "--", str(Path(__file__)),
                         str(ROOT / "src/orchestragent")):
            parser.error("Commit the audit and context implementation before measurement")
        output = args.output.resolve()
        if not output.is_relative_to(ROOT):
            parser.error("output must be inside this repository")
        output.mkdir(parents=True, exist_ok=False)
        index = Index(args.repo.resolve(), output / "index.sqlite")
        snapshot = index.at(args.revision)
        if any(path not in snapshot.files for path in args.planning_file):
            parser.error("Planning audit files must exist at the pinned revision")
        reachable = set(args.planning_file)
        for path in args.planning_file:
            reachable.update(snapshot.reach(path))
        before = index.read(args.revision, reachable, suffix=".py")
        after = planning_sources(index, args.revision, args.planning_file)
        after_bytes = sum(len(s.encode()) for s in after.values())
        result = {"assessment": "static_planning_context", "model_calls": 0,
                  "script_revision": Workspace.git(ROOT, "rev-parse", "HEAD"),
                  "source_revision": args.revision, "files": args.planning_file,
                  "before": {"source_files": len(before),
                             "bytes": sum(len(s.encode()) for s in before.values())},
                  "after": {"source_views": len(after),
                            "bytes": after_bytes,
                            "selectors": sorted(after)}}
        (output / "report.json").write_text(dumps(result) + "\n")
        print(dumps({k: v for k, v in result.items() if k != "after"}) + "\n"
              + dumps({"after_bytes": after_bytes}))
        return
    if not args.live:
        parser.error("--live explicitly enables up to four budgeted DSF calls")
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        parser.error("output must be inside this repository")
    engine = Workspace.git(ROOT, "rev-parse", "HEAD")
    index = Index(ROOT, orch_home(ROOT) / "index.sqlite")
    excerpts = []
    for selector in args.excerpt:
        selected = requested_sources(index, ROOT, engine, [selector])[selector]
        path = selector.split("::", 1)[0]
        source = index.read(engine, [path])[path]
        excerpts.append({"selector": selector, "source_bytes": len(source.encode()),
                         "excerpt_bytes": len(selected.encode())})
    output.mkdir(parents=True, exist_ok=False)
    repo = output / "repo"
    repo.mkdir()
    Workspace.git(repo, "init", "-b", "main")
    (repo / "helper.py").write_text("def offset() -> int:\n    return 7\n")
    Workspace.git(repo, "add", "helper.py")
    Workspace.git(repo, "commit", "-m", "Protocol fixture seed")
    errors = lint(PLAN, frozenset({"helper.py"}))
    if errors:
        raise ValueError(errors)
    clients = providers(ROOT / ".data")
    calls = 0

    def call(provider: str, model: str, prompt: str, schema: dict[str, object], run_id: str,
             cancelled: Callable[[], bool], *, effort: str = "") -> NativeResult:
        nonlocal calls
        if (provider, model) != (DSF.provider, DSF.model) or calls >= 4:
            return NativeResult("policy_blocked", {}, usage={"forwarded": 0},
                                message="Protocol probe allows at most four DSF calls")
        calls += 1
        return clients.call(provider, model, prompt, schema, run_id, cancelled, effort=effort)

    orch = Orchestrator(repo, call, LiveWorkspace.run_unittests)
    try:
        base = Workspace.git(repo, "rev-parse", "HEAD")
        run = orch._checkpoint("context-probe", "Protocol smoke", base, PLAN,
                               Draft(PLAN, 0, []), [])
        result = orch.execute(run.id, jobs=2, max_cents=50)
        task = orch.state.task(run.id, "T-001")
        events = orch.state.events(run.id)
        artifact = {
            "kind": "live-protocol-smoke-not-performance-benchmark",
            "engine_revision": engine,
            "excerpts": excerpts, "excerpt_metric": "source bytes only, not prompt tokens",
            "planner": "fixed fixture; no planner model invocation", "run_id": run.id,
            "status": result.status, "protocol_passed": result.status == "done"
            and task.context_requests > 0, "context_requests": task.context_requests,
            "attempts": task.attempts, "failure_rung": task.level,
            "context_events": [event.data for event in events if event.kind == "context.request"],
            "cache_policy": next((event.data for event in reversed(events)
                                  if event.kind == "run.cache"), None),
            "usage": report(orch.state.usage.calls(run.id), "model"),
        }
        (output / "report.json").write_text(dumps(artifact) + "\n")
        print(dumps(artifact))
    finally:
        orch.release_worktrees("context-probe")
        clients.close()


if __name__ == "__main__":
    main()
