"""Single-model coding baseline on an isolated benchmark branch."""

import uuid
from pathlib import Path

from orchestragent.dsh import DshClient
from orchestragent.live import TEXT, object_schema
from orchestragent.live_workspace import LiveWorkspace
from orchestragent.orch_context import ScopedContext
from orchestragent.orch_run import CLAUDE_PLAN, STOP, Orchestrator, Route
from orchestragent.serialization import dumps

FILE_ARRAY = {"type": "array", "items": object_schema({"path": TEXT, "content": TEXT})}
FILES_SCHEMA = object_schema({"files": FILE_ARRAY})
SCOPED_SCHEMA = object_schema({
    "files": FILE_ARRAY,
    "need_files": {"type": "array", "items": TEXT},
})
SOURCE_LIMIT = 32_000
FEEDBACK_LIMIT = 4_000


def _prompt(
    spec: str, base: str, revision: str, sources: dict[str, str], feedback: str,
) -> str:
    visible: dict[str, str] = {}
    omitted: list[str] = []
    used = 0
    for path, source in sorted(sources.items()):
        size = len(path.encode()) + len(source.encode())
        if used + size <= SOURCE_LIMIT:
            visible[path] = source
            used += size
        else:
            omitted.append(path)
    context = dumps({
        "base_revision": base,
        "current_revision": revision,
        "visible_python_sources": visible,
        "omitted_paths": omitted,
        "previous_attempt_feedback": feedback[-FEEDBACK_LIMIT:],
    })
    return (
        "Implement the full specification in this repository snapshot. Return complete file "
        "contents for every file to add or replace, including unittest test_*.py coverage. "
        "Use only the visible sources below and the specification; do not assume hidden tests "
        "or external packages. Return only the requested files object.\n\n"
        f"SPECIFICATION:\n{spec}\n\nVISIBLE CONTEXT:\n{context}"
    )


def baseline(
    orchestrator: Orchestrator, spec: str, *, attempts: int = 2, route: Route = CLAUDE_PLAN,
    context_mode: str = "full", context_bytes: int = 16000,
) -> tuple[str, str, str]:
    """Run one explicit model; return (run id, last safe revision, status)."""
    if type(attempts) is not int or attempts < 1:
        raise ValueError("attempts must be a positive integer")
    if context_mode not in {"full", "scoped"}:
        raise ValueError("context_mode must be full or scoped")
    if type(context_bytes) is not int or not 2048 <= context_bytes <= 32768:
        raise ValueError("context_bytes must be an integer in 2048..32768")
    run_id = f"single-{uuid.uuid4().hex[:8]}"
    base = orchestrator.git(orchestrator.repo, "rev-parse", "HEAD")
    last_safe = base
    branch = f"orch/{run_id}/single"
    try:
        if orchestrator.cancel.is_set():
            return run_id, last_safe, "paused"
        with orchestrator.worktree(run_id, "single", base) as tree:
            orchestrator.git(tree, "checkout", "-b", branch)
            current = base
            feedback = ""
            scoped = (ScopedContext(orchestrator.index, tree, base, spec, context_bytes)
                      if context_mode == "scoped" else None)
            schema = SCOPED_SCHEMA if scoped else FILES_SCHEMA
            for attempt in range(1, attempts + 1):
                orchestrator.git(tree, "checkout", "--force", branch)
                orchestrator.git(tree, "clean", "-ffdxq")
                if orchestrator.cancel.is_set():
                    return run_id, last_safe, "paused"
                try:
                    prompt = (scoped.prompt(current, feedback, schema) if scoped else
                              _prompt(spec, base, current, orchestrator.sources(current), feedback))
                except ValueError as error:
                    orchestrator.notify(f"[single] context rejected: {error}")
                    return run_id, last_safe, "failed"
                if (route.provider == "dsh"
                        and len(DshClient.task_text(prompt, schema).encode())
                        > DshClient.input_limit):
                    orchestrator.notify("[single] DSF request exceeds input_limit; shorten spec "
                                        "or use scoped context")
                    return run_id, last_safe, "failed"
                if orchestrator.cancel.is_set():
                    return run_id, last_safe, "paused"
                call_id = f"{run_id}-a{attempt}-{uuid.uuid4().hex[:8]}"
                try:
                    result = orchestrator._call_model(
                        run_id, route, prompt, schema, call_id,
                        purpose="single", attempt=attempt, scope="execution", base=base,
                    )
                except Exception as error:
                    if scoped:
                        orchestrator.notify(f"[single] Model call failed: {type(error).__name__}")
                    return run_id, last_safe, "failed"
                if result.outcome in STOP or orchestrator.cancel.is_set():
                    return run_id, last_safe, "paused"
                if result.outcome != "ok":
                    return run_id, last_safe, "failed"
                try:
                    if scoped:
                        if set(result.data) != {"files", "need_files"}:
                            raise ValueError("Response must contain files and need_files")
                        edits, reads = result.data["files"], result.data["need_files"]
                        if (not isinstance(edits, list) or not isinstance(reads, list)
                                or bool(edits) == bool(reads)):
                            raise ValueError("Return exactly one nonempty files or "
                                             "need_files action")
                        if reads:
                            scoped.request(reads, current)
                            if attempt == attempts:
                                orchestrator.notify("[single] File request exhausted call limit; "
                                                    "increase attempts to allow an edit")
                            feedback = ""
                            continue
                    elif set(result.data) != {"files"}:
                        raise ValueError("Response must contain only files")
                    files = LiveWorkspace.validate_bundle(
                        result.data["files"], require_tests=not (scoped and scoped.seed_tests),
                    )
                    if scoped:
                        scoped.validate_edits(files, current)
                except (TypeError, ValueError) as error:
                    feedback = f"Attempt {attempt}: invalid file bundle: {str(error)[:500]}"
                    if scoped:
                        orchestrator.notify(f"[single] {feedback}")
                    continue

                try:
                    root = tree.resolve()
                    targets: list[tuple[Path, str]] = []
                    for file in files:
                        target = tree / file["path"]
                        if (target.is_symlink()
                                or not target.resolve().is_relative_to(root)):
                            raise ValueError("Generated path escapes worktree")
                        targets.append((target, file["content"]))
                    for target, content in targets:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(content)
                    orchestrator.git(tree, "add", "--", *[file["path"] for file in files])
                    if orchestrator.git(tree, "diff", "--cached", "--name-only"):
                        orchestrator.git(
                            tree, "commit", "-m", f"Single-model baseline attempt {attempt}",
                            author=route.label,
                        )
                    current = orchestrator.git(tree, "rev-parse", "HEAD")
                    passed, detail = orchestrator.verify(
                        tree, orchestrator.scratch(run_id), current, None,
                    )
                except Exception as error:
                    feedback = (
                        f"Attempt {attempt}: file write or test verification failed: "
                        f"{type(error).__name__}: {str(error)[:500]}"
                    )
                    if scoped:
                        orchestrator.notify(f"[single] {feedback}")
                    continue
                if passed:
                    last_safe = current
                    return run_id, last_safe, "done"
                repair = ("Repair code; original tests remain frozen." if scoped else
                          "Repair the code and tests.")
                feedback = (
                    f"Attempt {attempt}: generated unittest suite failed. {repair} "
                    f"Runner output:\n{detail[-FEEDBACK_LIMIT:]}"
                )
        return run_id, last_safe, "failed"
    except Exception as error:
        if context_mode == "scoped":
            orchestrator.notify(f"[single] Local execution failed: {type(error).__name__}")
        return run_id, last_safe, "failed"
    finally:
        orchestrator.release_worktrees(run_id)
