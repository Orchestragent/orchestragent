import re
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

import pytest

from orchestragent.controller import Controller
from orchestragent.jev import Decision
from orchestragent.live_workspace import LiveWorkspace
from orchestragent.native import NativeResult
from orchestragent.orch_bench import ACCEPTANCE_PACKAGE, Case
from orchestragent.orch_environment import PytestEnvironment
from orchestragent.orch_run import Orchestrator
from orchestragent.orch_state import Run
from orchestragent.orch_usage import Call
from orchestragent.settings import SettingsStore
from orchestragent.store import Store
from orchestragent.workspace import Workspace


class CaseProjectFactory(Protocol):
    def __call__(self, case: Case, name: str, sources: dict[str, str] | None = None,
                 *, acceptance: bool = True) -> Path: ...



@pytest.fixture(autouse=True)
def no_coverage_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scripted callers predate the coverage review; tests that cover it turn it back on."""
    monkeypatch.setattr("orchestragent.orch_run.COVERAGE_REVIEW", False)

@pytest.fixture
def dependency_environment(tmp_path: Path, request: pytest.FixtureRequest) -> PytestEnvironment:
    return PytestEnvironment.create(tmp_path / "dependencies", packages=("typing_extensions",),
                                    source_roots=getattr(request, "param", (".",)))


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


@pytest.fixture
def case_project(tmp_path: Path) -> CaseProjectFactory:
    """Assemble a seeded benchmark project with optional reference/held-out files."""
    def build(case: Case, name: str, sources: dict[str, str] | None = None,
              *, acceptance: bool = True) -> Path:
        project = tmp_path / f"{case.id}-{name}"
        project.mkdir()
        files = {**case.seed, **(sources or {})}
        if acceptance:
            files[f"{ACCEPTANCE_PACKAGE}/__init__.py"] = ""
            files.update({f"{ACCEPTANCE_PACKAGE}/{path}": source
                          for path, source in case.acceptance.items()})
        for path, source in files.items():
            assert LiveWorkspace.safe_path(path)
            target = project / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
        return project
    return build


@pytest.fixture(autouse=True)
def _keychain_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never read or write the real login keychain, and must not depend on
    whether this machine has the DSH CLI (tests of the direct API transport opt in)."""
    monkeypatch.setenv("ORCHESTRA_KEYCHAIN", "0")
    monkeypatch.setenv("ORCHESTRA_DSF_TRANSPORT", "cli")
    # Scripted Python cases run in seed repos holding only a README, which a real run
    # would route to the generic engine; generic-engine tests set ORCH_ENGINE themselves.
    monkeypatch.setenv("ORCH_ENGINE", "python")


@dataclass
class Clock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def controller(tmp_path: Path, clock: Clock) -> Controller:
    return Controller(
        Store(tmp_path / "tasks.sqlite"), Workspace(tmp_path / "workspaces"), clock=clock
    )


@pytest.fixture
def settings(tmp_path: Path) -> SettingsStore:
    return SettingsStore(Store(tmp_path / "settings.db"))


@pytest.fixture
def policy(settings: SettingsStore) -> dict[str, object]:
    return cast(dict[str, object], settings.read()["policy"])


@pytest.fixture
def providers(policy: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], policy["providers"])


# --- orchestration CLI fixtures ------------------------------------------------
ADDER_STUB = (
    '"""Addition."""\n\n\ndef add(a: int, b: int) -> int:\n'
    '    """Return a + b."""\n    raise NotImplementedError\n'
)
DOUBLER_STUB = (
    '"""Doubling built on add."""\n\nfrom adder import add\n\n\n'
    'def double(a: int) -> int:\n    """Return 2 * a using add."""\n'
    "    raise NotImplementedError\n"
)
MATH_TESTS = (
    "import unittest\n\nfrom adder import add\nfrom doubler import double\n\n\n"
    "class AddTest(unittest.TestCase):\n    def test_r1_adds(self) -> None:\n"
    "        self.assertEqual(add(2, 3), 5)\n\n\n"
    "class DoubleTest(unittest.TestCase):\n    def test_r2_doubles(self) -> None:\n"
    "        self.assertEqual(double(4), 8)\n"
)


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
THICK = NativeResult("ok", {"verdict": "contract", "explanation": "", "edits": [],
                            "lesson": "", "contract": "add(a, b) returns a + b exactly"})
EMPTY_REPLAN = NativeResult("ok", {"summary": "", "tasks": []})
# Persistent failure walks every rung: 0, 1 (DSF), 3 (Claude patch), 4 (thicken + DSF),
# then 5 (replan) parks because the replan proposes nothing.
PARKING: dict[str, list[NativeResult]] = {
    "dsh:T-001": [BAD_ADD, BAD_ADD, BAD_ADD],
    "claude:T-001": [BAD_ADD, THICK, EMPTY_REPLAN],
}


@pytest.fixture
def math_plan() -> dict[str, object]:
    return {
        "summary": "math ops",
        "requirements": [
            {"id": "R1", "text": "add two integers"},
            {"id": "R2", "text": "double an integer"},
        ],
        "files": [
            {"path": "adder.py", "content": ADDER_STUB},
            {"path": "doubler.py", "content": DOUBLER_STUB},
            {"path": "test_mathops.py", "content": MATH_TESTS},
        ],
        "tasks": [
            {
                "id": "T-001", "goal": "implement add", "reqs": ["R1"], "files": ["adder.py"],
                "depends_on": [], "tests": ["test_mathops.AddTest.test_r1_adds"],
                "contract": "add(a, b) == a + b",
            },
            {
                "id": "T-002", "goal": "implement double", "reqs": ["R2"],
                "files": ["doubler.py"], "depends_on": ["T-001"],
                "tests": ["test_mathops.DoubleTest.test_r2_doubles"],
                "contract": "double(a) == add(a, a)",
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
        task = re.search(r"T-[0-9]{3}", run_id)
        key = f"{provider}:{task.group(0)}" if task else "plan"
        with self.lock:
            self.calls.append((provider, key, prompt))
            queue = self.responses.get(key)
            if not queue:
                pytest.fail(f"unexpected model call {key} ({model})")
            return queue.pop(0)

    def keys(self) -> list[str]:
        return [key for _, key, _ in self.calls]


def local_runner(
    execution: Path, scratch: Path, revision: str, names: list[str] | None,
    *, timeout: float = 25.0,
) -> tuple[bool, str]:
    """Unsandboxed stand-in for LiveWorkspace.run_unittests; same contract."""
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", *(names or ["discover"])],
            cwd=execution, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:g}s"
    return result.returncode == 0, result.stdout + result.stderr


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
    return ScriptedCaller({"plan": [NativeResult("ok", math_plan)]})


@pytest.fixture
def orchestrator(orch_repo: Path, caller: ScriptedCaller, clock: Clock) -> Orchestrator:
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


# --- Jev fixtures -------------------------------------------------------------
def rung(choice: str, confidence: float) -> Decision:
    return Decision(
        "ok",
        {"rung": {"type": "choice", "choice": choice,
                  "probabilities": {choice: confidence, "retry": 1 - confidence}}},
        {"settled_cents": 1},
    )


def cheat(probability: float) -> Decision:
    return Decision("ok", {"cheat": {"type": "boolean", "probability": probability}},
                    {"settled_cents": 1})


@dataclass
class ScriptedJev:
    """Answers triage ('rung') and pre-screen ('cheat') questions from queues."""

    triage: list[Decision] = field(default_factory=list)
    screen: list[Decision] = field(default_factory=list)
    states: list[object] = field(default_factory=list)

    def __call__(self, state: object, questions: dict[str, object], run_id: str) -> Decision:
        self.states.append(state)
        if "cheat" in questions:
            return self.screen.pop(0) if self.screen else cheat(0.0)
        if not self.triage:
            pytest.fail("unexpected triage question")
        return self.triage.pop(0)
