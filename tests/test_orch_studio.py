"""orch studio over a real repository: views from orch state, changes through OrchState."""

from pathlib import Path
from typing import Any

import pytest

from orchestragent.native import NativeResult
from orchestragent.orch_run import Orchestrator
from orchestragent.orch_state import OrchEvent, OrchTask
from orchestragent.orch_studio import RepoBackend, diagnose, title_of
from orchestragent.orch_studio_server import StudioError
from orchestragent.orch_verify import Check
from orchestragent.platform import sandbox_available
from orchestragent.workspace import Workspace
from tests.conftest import (
    SUPERVISE_ESCALATE,
    SUPERVISE_GIVE_UP,
    SUPERVISE_RETRY,
    ScriptedCaller,
    edit_result,
    local_runner,
)
from tests.test_orch_generic_e2e import BOUNDARY, GREET_SHARD, GREET_TEST, SLUG_IMPL, designs

needs_sandbox = pytest.mark.skipif(not sandbox_available(), reason="no sandbox backend")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    Workspace.git(path, "init", "-b", "main")
    (path / "README.md").write_text("demo\n")
    Workspace.git(path, "add", "README.md")
    Workspace.git(path, "commit", "-m", "seed")
    return path


def backend(repo: Path, tmp_path: Path, launched: list[tuple[str, str]]) -> RepoBackend:
    def launch(_: Path, spec: str, run: str) -> Path:
        launched.append((spec, run))
        return tmp_path
    studio = RepoBackend(repo, tmp_path / "data", launch=launch)
    studio._provider_status = lambda: {  # type: ignore[method-assign]
        "claude": {"ready": True}, "dsh": {"ready": True}, "codex": {"ready": False}}
    return studio


@needs_sandbox
def test_a_parked_node_is_shown_diagnosed_and_unparked_by_a_message(
        repo: Path, tmp_path: Path) -> None:
    wrong = edit_result("lib/greet.sh", 'greet() { echo "hi $1"; }\n', search="")
    caller = ScriptedCaller({
        "plan": [NativeResult("ok", BOUNDARY)], **designs(),
        "dsh:T-001": [wrong, wrong, wrong],
        "claude:T-001": [SUPERVISE_RETRY, SUPERVISE_ESCALATE, wrong, SUPERVISE_GIVE_UP],
        "dsh:T-002": [SLUG_IMPL],
    })
    orch = Orchestrator(repo, caller, local_runner, notify=lambda _: None)
    run = orch.execute(orch.plan("# Greeter\n\nR1. greet\nR2. slug", test_jobs=1).id)
    launched: list[tuple[str, str]] = []
    studio = backend(repo, tmp_path, launched)

    runs = studio.runs()
    assert runs[0]["id"] == run.id and runs[0]["title"] == "Greeter"
    assert runs[0]["parked"] == 1 and runs[0]["live"] is False
    graph = studio.graph(run.id)
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes["T-001"]["status"] == "PARKED" and nodes["T-002"]["status"] == "DONE"
    assert nodes["T-001"]["diagnosis"]["kind"] == "own"
    assert nodes["T-001"]["route"]["vendor"] == "deepseek"

    node = studio.node(run.id, "T-001")
    assert node["requirements"][0]["id"] == "R1"
    assert node["checks"][0]["files"][0]["content"] == GREET_TEST
    assert node["checks"][0]["files"][0]["path"] == GREET_SHARD[1]
    assert node["files"][0]["state"] == "candidate" and "hi $1" in node["files"][0]["content"]
    failed = [a for a in node["attempts"] if a["outcome"] == "failed"]
    assert failed and "FAIL" in failed[0]["report"]
    assert {"started", "failed", "parked"} <= {item["kind"] for item in node["thread"]}
    assert all(route["model"] != "deepseek-v4-pro" for route in node["routes"])

    with pytest.raises(StudioError) as error:
        studio.route(run.id, "T-001", "codex/gpt-6-astra:high")  # not logged in
    assert error.value.code == "invalid"
    chosen = studio.route(run.id, "T-001", "claude/claude-opus-5-5:high")
    assert chosen["route_override"] is True and chosen["route"]["vendor"] == "anthropic"

    after = studio.message(run.id, "T-001", "Print exactly 'hello NAME'.", False)
    assert after["status"] == "READY" and after["person"]["messages"] == 1
    assert launched == [("", run.id)]  # a worker resumes the run
    thread = studio.node(run.id, "T-001")["thread"]
    assert thread[-1]["author"] == "you" and "hello NAME" in thread[-1]["text"]
    with pytest.raises(StudioError) as error:
        studio.retry(run.id, "T-002")
    assert error.value.code == "conflict"
    with pytest.raises(StudioError) as error:
        studio.node(run.id, "T-404")
    assert error.value.code == "not_found"


def test_diagnosis_tells_own_upstream_environment_and_waiting_apart() -> None:
    mine = Check("sh tests/b_test.sh", ("tests/b_test.sh",)).id
    theirs = Check("sh tests/a_test.sh", ("tests/a_test.sh",)).id
    a = OrchTask("T-001", "a", ["R1"], ["lib/a.sh"], [], [theirs], "", status="RUNNING")
    b = OrchTask("T-002", "b", ["R2"], ["lib/b.sh"], ["T-001"], [mine], "", status="PLANNED")
    tasks = {"T-001": a, "T-002": b}
    owners = {"lib/a.sh": "T-001", "lib/b.sh": "T-002"}
    checks = {theirs: "T-001", mine: "T-002"}
    assert diagnose(b, tasks, [], owners, checks)["kind"] == "waiting"

    def parked(report: str, category: str = "test") -> dict[str, Any]:
        task = OrchTask("T-002", "b", ["R2"], ["lib/b.sh"], ["T-001"], [mine], "",
                        status="PARKED", last_error=report)
        event = OrchEvent(1, "r", "T-002", "r/T-002/a1", "", "task.parked", 1.0,
                          {"category": category, "report": report})
        return diagnose(task, {**tasks, "T-001": OrchTask(
            "T-001", "a", ["R1"], ["lib/a.sh"], [], [theirs], "", status="DONE")},
            [event], owners, checks)

    assert parked(f"FAIL (exit 1): {mine}\n--- boom")["kind"] == "own"
    upstream = parked(f"FAIL (exit 1): {mine}\nlib/a.sh: line 3: bad substitution")
    assert upstream["kind"] == "upstream" and upstream["blamed"] == ["T-001"]
    assert parked(f"FAIL (exit 127): {mine}\nsh: go: not found")["kind"] == "environment"
    merged = parked(f"FAIL (exit 1): {theirs}", "integration")
    assert merged["kind"] == "upstream" and merged["blamed"] == ["T-001"]


def test_titles_skip_comments_and_heading_marks() -> None:
    assert title_of("<!-- nonce -->\n\n# Ledger\nR1. x") == "Ledger"
    assert title_of("") == "untitled run"
