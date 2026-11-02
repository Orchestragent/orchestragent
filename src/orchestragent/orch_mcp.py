"""`orch mcp`: Orchestragent as a Model Context Protocol server over stdio.

Another agent harness (Claude Code, Cursor, Codex, ...) hands a whole build to orch:
`orch_start` plans and runs it in the background, `orch_wait` / `orch_status` follow it,
`orch_report` returns the outcome and the branch to review. Runs take minutes, so every
tool returns promptly; nothing here blocks longer than `orch_wait`'s bounded timeout.

Standard library only: newline-delimited JSON-RPC 2.0 on stdin/stdout, logs on stderr.
Each tool shells out to the same `orch` CLI a person would type, so behaviour, state and
safety checks are identical. No secret, prompt or spec text is echoed back beyond what
the CLI itself prints.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestragent.serialization import dumps

Json = dict[str, Any]

SERVER_NAME = "orch"
SERVER_VERSION = "0.1.0"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
WAIT_LIMIT_S = 600
OUTPUT_LIMIT = 20_000
JOB_ID = re.compile(r"[a-f0-9]{12}")
RUN_ID = re.compile(r"r[a-f0-9]{8}")

_REPO = {"type": "string", "description": "absolute path of the target git repository"}
_RUN = {"type": "string", "description": "orch run id such as r1a2b3c4d (default: newest)"}
_JOB = {"type": "string", "description": "job id returned by orch_start"}

TOOLS: list[Json] = [
    {
        "name": "orch_start",
        "description": (
            "Hand a whole multi-part build to orch: Opus plans task boundaries, independent "
            "frozen tests are designed per task, parallel builders implement them in sandboxed "
            "worktrees, and merges are gated by the tests. Runs in the background (typically "
            "5-20 minutes) and returns a job id at once; follow it with orch_wait or "
            "orch_status, then read orch_report. The result lands on branch orch/<run_id>; "
            "the repository's checked-out branch is not modified."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "spec": {"type": "string", "description": (
                    "complete requirements, numbered R1, R2, ... with exact APIs and "
                    "behaviour; the authoritative input")},
                "base": {"type": "string", "description": "git revision to build on (HEAD)"},
                "jobs": {"type": "integer", "minimum": 1, "maximum": 16,
                         "description": "parallel builders (default 4)"},
            },
            "required": ["repo", "spec"],
            "additionalProperties": False,
        },
    },
    {
        "name": "orch_wait",
        "description": (
            "Block until an orch job finishes or `timeout_s` passes (max 600), then return "
            "its state. Prefer this over polling orch_status in a loop."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"repo": _REPO, "job": _JOB,
                           "timeout_s": {"type": "integer", "minimum": 1,
                                         "maximum": WAIT_LIMIT_S}},
            "required": ["repo", "job"],
            "additionalProperties": False,
        },
    },
    {
        "name": "orch_status",
        "description": "Current state of an orch job and/or run: phase, tasks, progress, spend.",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": _REPO, "job": _JOB, "run": _RUN},
            "required": ["repo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "orch_report",
        "description": (
            "Final report of a run: status, per-task outcome, the result branch and its diff "
            "stat against the base. Review or merge that branch yourself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"repo": _REPO, "run": _RUN},
            "required": ["repo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "orch_cost",
        "description": "Tokens, cache hits and spend of a run, by task or by model.",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": _REPO, "run": _RUN,
                           "by": {"type": "string", "enum": ["task", "model", "purpose"]}},
            "required": ["repo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "orch_cancel",
        "description": "Stop a running orch job. Finished work stays on its branch.",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": _REPO, "job": _JOB},
            "required": ["repo", "job"],
            "additionalProperties": False,
        },
    },
    {
        "name": "orch_doctor",
        "description": "Check that orch's prerequisites (models, keys, sandbox) are ready.",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": _REPO},
            "additionalProperties": False,
        },
    },
]


class ToolError(ValueError):
    """A caller mistake reported as a tool result with isError, not a protocol error."""


def orch_command() -> list[str]:
    """The CLI this server drives: the same interpreter and package as the server."""
    return [sys.executable, "-m", "orchestragent.orch_cli"]


def cli_env() -> dict[str, str]:
    env = dict(os.environ)
    package = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = package + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH")
                                   else "")
    return env


def repository(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ToolError("repo must be an absolute path to a git repository")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ToolError("repo must be an absolute path")
    if not path.is_dir():
        raise ToolError(f"repo {path} does not exist")
    probe = subprocess.run(["git", "-C", str(path), "rev-parse", "--git-common-dir"],
                           capture_output=True, text=True, check=False)
    if probe.returncode:
        raise ToolError(f"{path} is not a git repository")
    return path.resolve()


def mcp_home(repo: Path) -> Path:
    common = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, check=True).stdout.strip()
    home = Path(common) / "orch" / "mcp"
    home.mkdir(parents=True, exist_ok=True)
    return home


def clip(text: str) -> str:
    return text if len(text) <= OUTPUT_LIMIT else "...\n" + text[-OUTPUT_LIMIT:]


def run_cli(repo: Path | None, *args: str, timeout: float = 120) -> tuple[int, str]:
    command = [*orch_command(), *(["--repo", str(repo)] if repo else []), *args]
    try:
        done = subprocess.run(command, capture_output=True, text=True, env=cli_env(),
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, f"orch {' '.join(args)} timed out after {timeout:.0f}s"
    return done.returncode, (done.stdout + done.stderr).strip()


# --- jobs ------------------------------------------------------------------------------


def job_dir(repo: Path, job: object) -> Path:
    if not isinstance(job, str) or not JOB_ID.fullmatch(job):
        raise ToolError("job must be the id returned by orch_start")
    path = mcp_home(repo) / "jobs" / job
    if not path.is_dir():
        raise ToolError(f"no orch job {job} in {repo}")
    return path


def job_state(path: Path) -> Json:
    meta = json.loads((path / "job.json").read_text())
    exit_file = path / "exit"
    log = (path / "log").read_text(errors="replace") if (path / "log").exists() else ""
    runs = RUN_ID.findall(log)
    state: Json = {
        "job": path.name,
        "started_at": meta["started_at"],
        "elapsed_s": round(time.time() - float(meta["started_at"]), 1),
        "run": runs[0] if runs else "",
    }
    if exit_file.exists():
        code = int(exit_file.read_text().strip() or "1")
        state.update(state="finished", exit_code=code, elapsed_s=round(
            exit_file.stat().st_mtime - float(meta["started_at"]), 1))
    elif alive(int(meta["pid"])):
        state["state"] = "running"
    else:
        state.update(state="stopped", exit_code=None)
    state["log_tail"] = "\n".join(log.splitlines()[-15:])
    return state


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start(arguments: Json) -> str:
    repo = repository(arguments.get("repo"))
    spec = arguments.get("spec")
    if not isinstance(spec, str) or len(spec.strip()) < 20:
        raise ToolError("spec must be the complete requirements text")
    base = arguments.get("base") or "HEAD"
    if not isinstance(base, str) or not re.fullmatch(r"[\w./^~@{}-]{1,200}", base):
        raise ToolError("base must be a git revision")
    jobs = arguments.get("jobs", 4)
    if type(jobs) is not int or not 1 <= jobs <= 16:
        raise ToolError("jobs must be an integer from 1 to 16")
    job = uuid.uuid4().hex[:12]
    path = mcp_home(repo) / "jobs" / job
    path.mkdir(parents=True)
    (path / "spec.md").write_text(spec if spec.endswith("\n") else spec + "\n")
    cli = " ".join(_quote(part) for part in [*orch_command(), "--repo", str(repo)])
    # Plan, then run exactly the run that plan created: its id is the first one plan logs.
    log_file = _quote(str(path / "log"))
    script = (
        f"{cli} plan --distributed --base {_quote(base)} {_quote(str(path / 'spec.md'))}"
        f" && run_id=$(grep -oE 'run r[a-f0-9]{{8}}' {log_file} | head -n 1 | cut -c5-)"
        f" && [ -n \"$run_id\" ] && {cli} run --jobs {jobs} \"$run_id\";"
        f" echo $? > {_quote(str(path / 'exit'))}"
    )
    with (path / "log").open("wb") as log:
        process = subprocess.Popen(
            ["/bin/sh", "-c", script], stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, env=cli_env(), cwd=str(repo), start_new_session=True,
        )
    (path / "job.json").write_text(dumps({"pid": process.pid, "started_at": time.time(),
                                          "base": base, "jobs": jobs}))
    return (f"orch job {job} started in {repo} (planning, then running with {jobs} builders).\n"
            f"Follow it with orch_wait(repo, job={job!r}); the result branch is orch/<run_id>.")


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def wait(arguments: Json, sleep: Callable[[float], None] = time.sleep) -> str:
    repo = repository(arguments.get("repo"))
    path = job_dir(repo, arguments.get("job"))
    limit = arguments.get("timeout_s", 300)
    if type(limit) is not int or not 1 <= limit <= WAIT_LIMIT_S:
        raise ToolError(f"timeout_s must be 1..{WAIT_LIMIT_S}")
    deadline = time.monotonic() + limit
    state = job_state(path)
    while state["state"] == "running" and time.monotonic() < deadline:
        sleep(min(5.0, max(0.1, deadline - time.monotonic())))
        state = job_state(path)
    return dumps(state)


def status(arguments: Json) -> str:
    repo = repository(arguments.get("repo"))
    parts: list[str] = []
    run = arguments.get("run") or ""
    if arguments.get("job"):
        state = job_state(job_dir(repo, arguments["job"]))
        parts.append("job: " + dumps(state))
        run = run or state["run"]
    if run and not RUN_ID.fullmatch(str(run)):
        raise ToolError("run must look like r1a2b3c4d")
    if run or not arguments.get("job"):
        code, text = run_cli(repo, "status", *([str(run)] if run else []))
        parts.append(text if text else f"(orch status exited {code})")
        code, text = run_cli(repo, "progress", *(["--run", str(run)] if run else []))
        if code == 0 and text:
            parts.append(text)
    return clip("\n\n".join(parts))


def report(arguments: Json) -> str:
    repo = repository(arguments.get("repo"))
    run = arguments.get("run") or ""
    if run and not RUN_ID.fullmatch(str(run)):
        raise ToolError("run must look like r1a2b3c4d")
    code, text = run_cli(repo, "status", "--all", *([str(run)] if run else []))
    match = re.search(r"run (r[a-f0-9]{8})\s+(\S+)", text)
    parts = [text]
    if match:
        rid = match.group(1)
        report_file = mcp_home(repo).parent / rid / "REPORT.md"
        if report_file.exists():
            parts.append(report_file.read_text())
        base = subprocess.run(["git", "-C", str(repo), "merge-base", "HEAD", f"orch/{rid}"],
                              capture_output=True, text=True, check=False).stdout.strip()
        if base:
            stat = subprocess.run(["git", "-C", str(repo), "diff", "--stat", base,
                                   f"orch/{rid}"], capture_output=True, text=True,
                                  check=False).stdout.strip()
            parts.append(f"result branch: orch/{rid}\n" + (stat or "(no changes)"))
    return clip("\n\n".join(p for p in parts if p))


def cost(arguments: Json) -> str:
    repo = repository(arguments.get("repo"))
    run = arguments.get("run") or ""
    if run and not RUN_ID.fullmatch(str(run)):
        raise ToolError("run must look like r1a2b3c4d")
    by = arguments.get("by", "task")
    if by not in {"task", "model", "purpose"}:
        raise ToolError("by must be task, model or purpose")
    return clip(run_cli(repo, "cost", "--by", str(by), *(["--run", str(run)] if run else []))[1])


def cancel(arguments: Json) -> str:
    repo = repository(arguments.get("repo"))
    path = job_dir(repo, arguments.get("job"))
    state = job_state(path)
    if state["state"] != "running":
        return f"job {path.name} is not running ({state['state']})"
    pid = int(json.loads((path / "job.json").read_text())["pid"])
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return f"job {path.name} stopped; resume later with `orch resume` if needed"


def doctor(arguments: Json) -> str:
    repo = repository(arguments["repo"]) if arguments.get("repo") else None
    return clip(run_cli(repo, "doctor", "--allow-unverified-auth")[1])


HANDLERS: dict[str, Callable[[Json], str]] = {
    "orch_start": start, "orch_wait": wait, "orch_status": status, "orch_report": report,
    "orch_cost": cost, "orch_cancel": cancel, "orch_doctor": doctor,
}


# --- protocol ----------------------------------------------------------------------------


def handle(message: Json) -> Json | None:
    """One JSON-RPC message in, its response out (None for notifications)."""
    method = message.get("method")
    ident = message.get("id")
    if ident is None:
        return None  # notifications (initialized, cancelled, ...) need no answer
    try:
        if method == "initialize":
            asked = (message.get("params") or {}).get("protocolVersion")
            result: Json = {
                "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Use orch for builds that span several modules or requirements: write a "
                    "complete numbered spec, call orch_start, then orch_wait until it "
                    "finishes, then orch_report and review the orch/<run_id> branch."),
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            if name not in HANDLERS:
                return _error(ident, -32602, f"unknown tool {name!r}")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return _error(ident, -32602, "arguments must be an object")
            try:
                text, failed = HANDLERS[name](arguments), False
            except (ToolError, json.JSONDecodeError, OSError,
                    subprocess.SubprocessError) as error:
                text, failed = f"{type(error).__name__}: {error}", True
            result = {"content": [{"type": "text", "text": text}], "isError": failed}
        else:
            return _error(ident, -32601, f"method {method!r} not found")
    except Exception as error:  # never let one bad request kill the server
        return _error(ident, -32603, f"{type(error).__name__}: {error}")
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident: object, code: int, text: str) -> Json:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": text}}


def serve(stdin: Any = None, stdout: Any = None) -> int:
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply: Json | None = _error(None, -32700, "parse error")
        else:
            reply = (handle(message) if isinstance(message, dict)
                     else _error(None, -32600, "invalid request"))
        if reply is not None:
            sink.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sink.flush()
    return 0
