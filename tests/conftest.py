import os
import re
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from orchestragent import orch_verify
from orchestragent.native import NativeResult
from orchestragent.orch_run import Orchestrator
from orchestragent.orch_sandbox import Result
from orchestragent.orch_state import Run
from orchestragent.orch_usage import Call
from orchestragent.orch_verify import Check
from orchestragent.workspace import Workspace


@pytest.fixture
def large_owned_source() -> str:
    return ("def helper():\n    return 0\n\n"
            "def add(a: int, b: int) -> int:\n    return a - b + helper()\n\n"
            "def unrelated():\n    return " + repr("context ballast " * 8000) + "\n")


@pytest.fixture
def unfinished_call(orchestrator: Orchestrator, planned: Run) -> Iterator[Call]:
    orchestrator.state.transition(planned.id, "T-001", "READY", "task.ready", {})
    orchestrator.state.transition(planned.id, "T-001", "RUNNING", "task.started", {}, attempts=1)
    receipt = Call("lost-receipt", planned.id, "dsh", "deepseek-flash", "build", 1,
                   task_id="T-001", attempt=1)
    # The durable row has been written; simulate a process dying before any final receipt.
    with orchestrator.state.usage.capture(receipt):
        yield receipt


@pytest.fixture(autouse=True)
def _keychain_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never read or write the real login keychain, and must not depend on
    whether this machine has the DSH CLI (tests of the direct API transport opt in)."""
    monkeypatch.setenv("ORCHESTRA_KEYCHAIN", "0")
    monkeypatch.setenv("ORCHESTRA_DSF_TRANSPORT", "cli")


@pytest.fixture(autouse=True)
def _red_judgment_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scripted runs count their model calls; the red-reason judgment (one extra call per
    design round) is off unless a test turns it on (tests/test_orch_red_judgment.py)."""
    from orchestragent import orch_run
    monkeypatch.setattr(orch_run, "RED_JUDGMENT", False)


@dataclass
class Clock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


# --- orchestration fixtures -----------------------------------------------------
# The scripted "models" chose Python and unittest for this project; orch itself only sees
# stub files, frozen test files and the shell command each test designer declared.
ADDER_STUB = (
    '"""Addition."""\n\n\ndef add(a: int, b: int) -> int:\n'
    '    """Return a + b."""\n    raise NotImplementedError\n'
)
DOUBLER_STUB = (
    '"""Doubling built on add."""\n\nfrom adder import add\n\n\n'
    'def double(a: int) -> int:\n    """Return 2 * a using add."""\n'
    "    raise NotImplementedError\n"
)
ADD_TESTS = (
    "import unittest\n\nfrom adder import add\n\n\n"
    "class AddTest(unittest.TestCase):\n    def test_r1_adds(self) -> None:\n"
    "        self.assertEqual(add(2, 3), 5)\n"
)
DOUBLE_TESTS = (
    "import unittest\n\nfrom doubler import double\n\n\n"
    "class DoubleTest(unittest.TestCase):\n    def test_r2_doubles(self) -> None:\n"
    "        self.assertEqual(double(4), 8)\n"
)
ADD_CHECK = Check("python3 -m unittest -q test_add", ("test_add.py",))
DOUBLE_CHECK = Check("python3 -m unittest -q test_double", ("test_double.py",))
ADD_TEST = ADD_CHECK.id
DOUBLE_TEST = DOUBLE_CHECK.id


def design(path: str, content: str, command: str) -> NativeResult:
    """A test designer's answer: frozen test files plus the command that runs them."""
    return NativeResult("ok", {"files": [{"path": path, "content": content}],
                               "command": command})


def component_design(*shards: tuple[str, str, str, str]) -> NativeResult:
    """One component designer's answer: (task, path, content, command) per task."""
    return NativeResult("ok", {"shards": [
        {"task": task, "files": [{"path": path, "content": content}], "command": command}
        for task, path, content, command in shards]})


def integration(*checks: tuple[list[str], str, str, str]) -> NativeResult:
    """The planner's integration checks: (tasks, path, content, command) each."""
    return NativeResult("ok", {"checks": [
        {"tasks": tasks, "files": [{"path": path, "content": content}], "command": command}
        for tasks, path, content, command in checks]})


ADD_SHARD = ("T-001", "test_add.py", ADD_TESTS, ADD_CHECK.command)
DOUBLE_SHARD = ("T-002", "test_double.py", DOUBLE_TESTS, DOUBLE_CHECK.command)
ADD_DESIGN = design("test_add.py", ADD_TESTS, ADD_CHECK.command)
DOUBLE_DESIGN = design("test_double.py", DOUBLE_TESTS, DOUBLE_CHECK.command)
# The math plan declares no components: one implicit component at the root designs both
# tasks' tests in a single answer. T-002 depends on T-001 only, so no integration design.
MATH_DESIGNS: dict[str, list[NativeResult]] = {
    "dsh:design:root": [component_design(ADD_SHARD, DOUBLE_SHARD)]}


def edit_result(
    path: str, replace: str, status: str = "DONE", search: str = "raise NotImplementedError"
) -> NativeResult:
    """A builder answer with one search/replace edit and fixed usage."""
    return NativeResult(
        "ok",
        {
            "status": status,
            "summary": "",
            "reason": "needs a frozen file" if status != "DONE" else "",
            "edits": [{"path": path, "search": search, "replace": replace}],
        },
        usage={"input_tokens": 100, "output_tokens": 20, "settled_cents": 1},
    )


GOOD_ADD = edit_result("adder.py", "return a + b")
BAD_ADD = edit_result("adder.py", "return a - b")
GOOD_DOUBLE = edit_result("doubler.py", "return add(a, a)")


def supervise(action: str, **fields: object) -> NativeResult:
    """A supervisor's answer (orch_supervisor.SUPERVISOR_SCHEMA)."""
    data: dict[str, object] = {"action": action, "explanation": f"scripted {action}",
                               "guidance": "", "test_edits": [], "contract": "", "tasks": []}
    data.update(fields)
    return NativeResult("ok", data, usage={"input_tokens": 500, "output_tokens": 50})


SUPERVISE_RETRY = supervise("retry", guidance="Read the failure; fix the root cause.")
SUPERVISE_ESCALATE = supervise("escalate", guidance="A stronger model takes the next attempt.")
SUPERVISE_GIVE_UP = supervise("give_up", explanation="a person should look at adder.py")
# Persistent failure: a1, a2 (cheap retry) -> supervisor says retry -> a3 -> supervisor
# says escalate -> a4 by the strong model -> supervisor gives up -> PARKED.
PARKING: dict[str, list[NativeResult]] = {
    "dsh:T-001": [BAD_ADD, BAD_ADD, BAD_ADD],
    "claude:T-001": [SUPERVISE_RETRY, SUPERVISE_ESCALATE, BAD_ADD, SUPERVISE_GIVE_UP],
}


@pytest.fixture
def math_plan() -> dict[str, object]:
    """The planner's boundary: requirements, tasks and stubs; test designers add checks."""
    return {
        "summary": "math ops",
        "requirements": [
            {"id": "R1", "text": "add two integers"},
            {"id": "R2", "text": "double an integer"},
        ],
        "files": [
            {"path": "adder.py", "content": ADDER_STUB},
            {"path": "doubler.py", "content": DOUBLER_STUB},
        ],
        "tasks": [
            {
                "id": "T-001", "goal": "implement add", "reqs": ["R1"], "files": ["adder.py"],
                "depends_on": [], "interface_depends_on": [], "tests": [],
                "contract": "add(a, b) == a + b",
            },
            {
                "id": "T-002", "goal": "implement double", "reqs": ["R2"],
                "files": ["doubler.py"], "depends_on": ["T-001"], "interface_depends_on": [],
                "tests": [], "contract": "double(a) == add(a, a)",
            },
        ],
    }


@dataclass
class ScriptedCaller:
    """Answers model calls from per-key queues: 'plan' or '<provider>:<task id>'."""

    responses: dict[str, list[NativeResult]] = field(default_factory=dict)
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(
        self,
        provider: str,
        model: str,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        cancelled: Callable[[], bool],
        *,
        effort: str = "",
    ) -> NativeResult:
        # call ids name their subject: a task (builds), a component (test design) or the
        # planner's integration design; the boundary itself is "plan"
        component = re.search(r"-test-design-component-([A-Za-z0-9_.]+)-\d+-", run_id)
        task = re.search(r"T-[0-9]{3}", run_id)
        if component:
            key = f"{provider}:design:{component.group(1)}"
        elif "-integration-design-planner-" in run_id:
            key = f"{provider}:integration"
        elif re.search(r"-look-\d+-", run_id):
            key = f"{provider}:look"
        elif "-red-judge-integration-" in run_id:
            key = f"{provider}:red:integration"
        elif "-red-judge-" in run_id:
            key = f"{provider}:red"
        else:
            key = f"{provider}:{task.group(0)}" if task else "plan"
        with self.lock:
            self.calls.append((provider, key, prompt))
            queue = self.responses.get(key)
            if not queue and key.endswith(":look"):
                # looking around is optional: an unscripted planner sees enough already
                return NativeResult("ok", {"requests": [], "note": ""})
            if not queue and (key.endswith(":red") or key.endswith(":red:integration")):
                # unscripted, every check failed on the stubs for the right reason
                return NativeResult("ok", {"checks": []})
            if not queue:
                pytest.fail(f"unexpected model call {key} ({model})")
            return queue.pop(0)

    def keys(self) -> list[str]:
        return [key for _, key, _ in self.calls]


def scripted(boundary: dict[str, object],
             builds: dict[str, list[NativeResult]] | None = None,
             designs: dict[str, list[NativeResult]] | None = None) -> ScriptedCaller:
    """A caller that plans `boundary`, designs each task's tests, then answers `builds`."""
    queues = {key: list(value) for key, value in (MATH_DESIGNS if designs is None
                                                   else designs).items()}
    for key, answers in (builds or {}).items():
        queues[key] = [*queues.get(key, []), *answers]
    return ScriptedCaller({"plan": [NativeResult("ok", boundary)], **queues})


def local_command(execution: Path, command: str, timeout: float,
                  seed: Path | None = None) -> Result:
    """Unsandboxed stand-in for orch_verify.run_command: /bin/sh -c from the root."""
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", command], cwd=execution, capture_output=True, text=True,
            timeout=timeout, check=False, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except subprocess.TimeoutExpired:
        return Result(124, f"timed out after {timeout:g}s", timed_out=True)
    return Result(result.returncode, result.stdout + result.stderr)


def local_runner(
    execution: Path, scratch: Path, revision: str, names: list[str] | None,
    *, timeout: float = 25.0,
) -> tuple[bool, str]:
    """Unsandboxed stand-in for orch_verify.run; same contract and report lines."""
    checks = [c for c in (orch_verify.parse(n) for n in names or []) if c is not None]
    if not checks:
        return False, "No verification command to run."
    passed, lines, tails = True, [], []
    for check in dict.fromkeys(checks):
        result = local_command(execution, check.command, timeout)
        passed &= result.ok
        status = "PASS" if result.ok else (
            "TIMEOUT" if result.timed_out else f"FAIL (exit {result.code})")
        lines.append(f"{status}: {check.id}")
        if not result.ok:
            tails.append(f"--- {check.command}\n{result.output[-6000:]}")
    return passed, "\n".join(lines) + ("\n\n" + "\n\n".join(tails) if tails else "")


@pytest.fixture
def unsandboxed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Planning's red gate runs declared commands without the OS sandbox (fast, portable);
    test_orch_verify and the generic end-to-end test exercise the real sandbox."""
    monkeypatch.setattr(orch_verify, "run_command", local_command)


@pytest.fixture
def orch_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    Workspace.git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("demo\n")
    Workspace.git(repo, "add", "README.md")
    Workspace.git(repo, "commit", "-m", "seed")
    return repo


@pytest.fixture
def caller(math_plan: dict[str, object]) -> ScriptedCaller:
    return scripted(math_plan)


@pytest.fixture
def orchestrator(orch_repo: Path, caller: ScriptedCaller, clock: Clock,
                 unsandboxed: None) -> Orchestrator:
    return Orchestrator(orch_repo, caller, local_runner, notify=lambda line: None, clock=clock)


@pytest.fixture
def planned(orchestrator: Orchestrator) -> Run:
    return orchestrator.plan("Build add and double.")


@pytest.fixture
def independent_run(math_plan: dict[str, object], orchestrator: Orchestrator) -> Run:
    cast(list[dict[str, str]], math_plan["files"])[1]["content"] = (
        "def double(a: int) -> int:\n    raise NotImplementedError\n"
    )
    cast(list[dict[str, object]], math_plan["tasks"])[1]["depends_on"] = []
    return orchestrator.plan("Build independent add and double functions.")


@pytest.fixture
def finished(orchestrator: Orchestrator, caller: ScriptedCaller, planned: Run) -> Run:
    caller.responses.update({"dsh:T-001": [GOOD_ADD], "dsh:T-002": [GOOD_DOUBLE]})
    return orchestrator.execute(planned.id, jobs=2)
