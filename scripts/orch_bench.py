"""Benchmark orch against a single native agent on a case in any language.

A case directory holds `spec.md`, hidden acceptance files and `acceptance.json`:

    {"command": "python3 -m unittest -q test_acceptance", "files": ["test_acceptance.py"]}

The acceptance files are never shown to the models. After a run they are copied into a
disposable verification worktree of the result and the command runs there in the
toolchain sandbox; the exit code decides. The same check grades both arms.

    python3 scripts/orch_bench.py --source REPO --revision SHA --case examples/bench/x \\
        --output .data/bench/run-1 [--single-agent --effort high] [--cold]
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import shlex
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestragent import orch_verify  # noqa: E402
from orchestragent.envelope import digest  # noqa: E402
from orchestragent.native import NativeResult  # noqa: E402
from orchestragent.orch_cli import providers  # noqa: E402
from orchestragent.orch_run import Orchestrator, PlanError  # noqa: E402
from orchestragent.orch_usage import Call, summary  # noqa: E402
from orchestragent.serialization import TEXT, dumps, object_schema  # noqa: E402
from orchestragent.workspace import Workspace  # noqa: E402

STREAM_LIMIT = 512 * 1024 * 1024  # a long native session streams every tool call


def load_case(case: Path) -> tuple[str, dict[str, str], str]:
    spec = (case / "spec.md").read_text()
    declared = json.loads((case / "acceptance.json").read_text())
    files = {name: (case / name).read_text() for name in declared["files"]}
    if not files or not str(declared.get("command", "")).strip():
        raise ValueError("acceptance.json needs a command and at least one file")
    return spec, files, str(declared["command"])


def accept(repo: Path, revision: str, files: dict[str, str], command: str,
           timeout: float) -> tuple[bool, str]:
    """Run the hidden acceptance against `revision` in a disposable worktree."""
    tree = repo.parent / f"acceptance-{uuid.uuid4().hex[:6]}"
    Workspace.git(repo, "worktree", "add", "--detach", str(tree), revision)
    try:
        orch_verify.link_ignored(repo, tree)
        for name, content in files.items():
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        result = orch_verify.run_command(tree, command, timeout)
        return result.ok, f"exit={result.code}\n{result.output[-20000:]}"
    finally:
        Workspace.git(repo, "worktree", "remove", "--force", str(tree))


def single_agent(repo: Path, spec: str, clients: Any, effort: str, timeout: float,
                 output: Path) -> dict[str, object]:
    """Native Claude Code alone, in the same repository, with the same sandbox for tests."""
    helper = " ".join(shlex.quote(str(v)) for v in (
        sys.executable, Path(__file__).resolve(), "--sandbox-run", repo))
    prompt = (spec + "\n\nYou are the sole implementation agent. Work in this checkout: read "
              "the existing code, implement the feature in whatever languages it needs, and "
              "run tests before finishing. Do not access the network, commit, or change "
              "existing tests or configuration; do not look for held-out tests. Hidden "
              "acceptance runs afterwards. Your Bash capability is this sandbox helper, which "
              "runs one shell command from the repository root, offline: "
              f"{helper} '<command>'\n" + orch_verify.SANDBOX_NOTE)
    schema = object_schema({"summary": TEXT})
    claude = clients.executables["claude"]
    argv = [str(claude), "--safe-mode", "--restricted",
            "--tools", "Read,Edit,Write,Glob,Grep,Bash", "--allowedTools",
            "Read,Edit,Write,Glob,Grep", f"Bash({helper} *)",
            "--strict-mcp-config", "--mcp-config", dumps({"mcpServers": {}}),
            "--permission-prompts", "none", "--permission-mode", "dontAsk",
            "--no-session-persistence", "--print", "--verbose", "--output-format", "stream-json",
            "--json-schema", dumps(schema), "--model", "claude-opus-5-5", "--effort", effort,
            "--disable-slash-commands", "--no-chrome"]
    clients.output_limit = STREAM_LIMIT
    outcome, code, stdout, stderr = clients._run(
        argv, repo, prompt, timeout, lambda: False)
    (output / "single-agent-stream.jsonl").write_bytes(stdout)
    native = (clients._decode("claude", code, stdout, stderr) if outcome == "ok"
              else NativeResult(outcome, {}, message="did not finish"))
    call = Call("single-" + uuid.uuid4().hex[:12], "single", "claude", "claude-opus-5-5",
                "single-agent", time.time(), scope="baseline", effort=effort,
                prompt_hash=digest(prompt), request_bytes=len(prompt.encode()))
    call.outcome, call.usage = native.outcome, native.usage
    return {"agent_outcome": native.outcome, "usage": summary([call])}


def sandbox_run(repo: Path, command: str) -> int:
    result = orch_verify.run_command(repo, command, 900)
    sys.stdout.write(result.output)
    return 0 if result.ok else (result.code or 1)


def main() -> int:
    faulthandler.enable(all_threads=True)  # a native crash prints every thread's stack
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--case", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--single-agent", action="store_true")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--cold", action="store_true",
                        help="prefix the spec with a nonce so no provider prompt cache applies")
    parser.add_argument("--timeout", type=float, default=2400)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--sandbox-run", nargs=2, metavar=("REPO", "COMMAND"))
    args = parser.parse_args()
    if args.sandbox_run:
        return sandbox_run(Path(args.sandbox_run[0]), args.sandbox_run[1])
    spec, acceptance, command = load_case(args.case)
    if args.cold:
        spec = f"<!-- benchmark run nonce {uuid.uuid4().hex}: ignore -->\n" + spec
    output = args.output.resolve()
    output.mkdir(parents=True)
    repo = output / "project"
    Workspace.git(output, "clone", "--no-hardlinks", "--local", str(args.source), str(repo))
    Workspace.git(repo, "checkout", "--detach", args.revision)
    base = Workspace.git(repo, "rev-parse", "HEAD")
    # Installed dependencies are untracked (node_modules, .venv, ...): link them, at any
    # depth, exactly as orch links them into its own worktrees.
    orch_verify.link_ignored(args.source.resolve(), repo)
    clients = providers(ROOT / ".data")
    result: dict[str, object] = {
        "case": args.case.name, "base": base, "arm": "single" if args.single_agent else "orch",
        "effort": args.effort if args.single_agent else "",
        "spec_digest": digest(spec), "acceptance_command": command,
        "acceptance_digests": {k: digest(v) for k, v in acceptance.items()},
    }
    started = time.monotonic()
    try:
        if args.single_agent:
            result.update(single_agent(repo, spec, clients, args.effort, args.timeout, output))
            Workspace.git(repo, "add", "-A", "--", ".", *orch_verify.excludes(repo))
            Workspace.git(repo, "commit", "--allow-empty", "-qm", "bench: single agent result")
            revision = Workspace.git(repo, "rev-parse", "HEAD")
        else:
            orch = Orchestrator(repo, clients.call, orch_verify.run,
                                notify=lambda message: print(message, flush=True))
            try:
                planned = orch.plan(spec)
            except PlanError as error:  # record why, and grade nothing
                result.update(status="plan_failed", plan_error=str(error),
                              plan_errors=error.errors[-8:], wall_s=0, acceptance_passed=False,
                              usage=summary(orch.state.usage.calls()))
                (output / "report.json").write_text(dumps(result))
                print(dumps({"arm": "orch", "plan_failed": str(error),
                             "errors": error.errors[-3:]}))
                return 1
            run = orch.execute(planned.id, args.jobs)
            result.update(run_id=run.id, status=run.status, gate_failure=orch.gate_failure,
                          usage=summary(orch.state.usage.calls(run.id)))
            revision = Workspace.git(repo, "rev-parse", f"orch/{run.id}")
    finally:
        clients.close()
    result["wall_s"] = round(time.monotonic() - started, 1)
    passed, log = accept(repo, revision, acceptance, command, 900)
    (output / "acceptance.log").write_text(log)
    result.update(result_revision=revision, acceptance_passed=passed)
    (output / "report.json").write_text(dumps(result))
    print(dumps({k: result[k] for k in ("arm", "acceptance_passed", "wall_s")}))
    shutil.rmtree(output / "tmp", ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
