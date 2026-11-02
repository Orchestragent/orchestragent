from collections.abc import Callable
from pathlib import Path

import pytest

from orchestragent.dsh import DshClient
from orchestragent.live_workspace import LiveWorkspace
from orchestragent.native import NativeResult
from orchestragent.orch_baseline import FILES_SCHEMA, SCOPED_SCHEMA, baseline
from orchestragent.orch_run import CLAUDE_PLAN, DSF, SONNET_PLAN, STOP, Orchestrator, Route
from orchestragent.workspace import Workspace
from tests.conftest import ScriptedCaller, local_runner

GOOD_FILES = [
    {"path": "adder.py", "content": "def add(a: int, b: int) -> int:\n    return a + b\n"},
    {"path": "test_adder.py", "content": (
        "import unittest\n\nfrom adder import add\n\n\n"
        "class AddTests(unittest.TestCase):\n"
        "    def test_adds_integers(self) -> None:\n"
        "        self.assertEqual(add(2, 3), 5)\n"
    )},
]
BAD_FILES = [
    {"path": "adder.py", "content": "def add(a: int, b: int) -> int:\n    return a - b\n"},
    GOOD_FILES[1],
]
SPEC = "Implement add(a: int, b: int) -> int in adder.py and test it in test_adder.py."


@pytest.fixture
def baseline_orchestrator(orch_repo: Path) -> Orchestrator:
    (orch_repo / "visible.py").write_text('VISIBLE = "source-visible-to-model"\n')
    Workspace.git(orch_repo, "add", "visible.py")
    Workspace.git(orch_repo, "commit", "-m", "visible benchmark seed")
    return Orchestrator(
        orch_repo, ScriptedCaller(), local_runner, notify=lambda _: None,
    )


@pytest.mark.parametrize("route", [CLAUDE_PLAN, SONNET_PLAN])
def test_single_model_baseline_commits_and_verifies_on_isolated_branch(
    baseline_orchestrator: Orchestrator, route: Route,
) -> None:
    orchestrator = baseline_orchestrator
    caller = ScriptedCaller({"plan": [NativeResult("ok", {"files": GOOD_FILES})]})
    orchestrator.call = caller
    original_head = Workspace.git(orchestrator.repo, "rev-parse", "HEAD")
    original_branch = Workspace.git(orchestrator.repo, "branch", "--show-current")
    receipt_seen_before_call: list[bool] = []
    schemas: list[dict[str, object]] = []

    def check_receipt(
        provider: str, model: str, prompt: str, schema: dict[str, object], run_id: str,
        cancelled: Callable[[], bool], *, effort: str = "",
    ) -> NativeResult:
        receipt_seen_before_call.append(
            any(call.id == run_id for call in orchestrator.state.usage.calls())
        )
        schemas.append(schema)
        return caller(provider, model, prompt, schema, run_id, cancelled, effort=effort)

    orchestrator.call = check_receipt
    run_id, revision, status = baseline(orchestrator, SPEC, attempts=1, route=route)
    assert run_id.startswith("single-")
    assert status == "done" and revision != original_head
    assert Workspace.git(orchestrator.repo, "rev-parse", "HEAD") == original_head
    assert Workspace.git(orchestrator.repo, "branch", "--show-current") == original_branch
    assert not (orchestrator.repo / "adder.py").exists()
    assert receipt_seen_before_call == [True]
    assert schemas == [FILES_SCHEMA]
    assert "source-visible-to-model" in caller.calls[0][2]
    receipts = orchestrator.state.usage.calls(run_id)
    assert len(receipts) == 1
    assert (receipts[0].purpose, receipts[0].scope, receipts[0].attempt,
            receipts[0].model, receipts[0].effort) == (
        "single", "execution", 1, route.model, route.effort,
    )
    assert receipts[0].base_revision == original_head


def test_failed_generated_tests_feed_current_code_into_repair_attempt(
    baseline_orchestrator: Orchestrator,
) -> None:
    caller = ScriptedCaller({
        "plan": [
            NativeResult("ok", {"files": BAD_FILES}),
            NativeResult("ok", {"files": GOOD_FILES}),
        ],
    })
    baseline_orchestrator.call = caller
    original_head = Workspace.git(baseline_orchestrator.repo, "rev-parse", "HEAD")
    run_id, revision, status = baseline(baseline_orchestrator, SPEC, attempts=2)
    assert status == "done" and revision != original_head
    assert len(caller.calls) == 2
    assert "return a - b" in caller.calls[1][2]
    assert "generated unittest suite failed" in caller.calls[1][2]
    receipts = baseline_orchestrator.state.usage.calls(run_id)
    assert [receipt.attempt for receipt in receipts] == [1, 2]
    assert all(receipt.base_revision == original_head for receipt in receipts)


@pytest.mark.parametrize(
    "files",
    [
        [
            {"path": "../escape.py", "content": "VALUE = 1\n"},
            {"path": "test_bad.py", "content": "import unittest\n"},
        ],
        [{"path": "module.py", "content": "VALUE = 1\n"}],
    ],
    ids=["unsafe-path", "missing-unittest-file"],
)
def test_invalid_bundle_returns_failed_without_changing_source_checkout(
    baseline_orchestrator: Orchestrator, tmp_path: Path, files: list[dict[str, str]],
) -> None:
    caller = ScriptedCaller({"plan": [NativeResult("ok", {"files": files})]})
    baseline_orchestrator.call = caller
    original_head = Workspace.git(baseline_orchestrator.repo, "rev-parse", "HEAD")
    branch = Workspace.git(baseline_orchestrator.repo, "branch", "--show-current")
    run_id, revision, status = baseline(baseline_orchestrator, SPEC, attempts=1)
    assert status == "failed" and revision == original_head
    assert len(caller.calls) == 1
    assert Workspace.git(baseline_orchestrator.repo, "rev-parse", "HEAD") == original_head
    assert Workspace.git(baseline_orchestrator.repo, "branch", "--show-current") == branch
    assert not list(tmp_path.rglob("escape.py"))
    assert len(baseline_orchestrator.state.usage.calls(run_id)) == 1


@pytest.mark.parametrize("outcome", sorted(STOP))
def test_stop_outcomes_pause_without_retries(
    baseline_orchestrator: Orchestrator, outcome: str,
) -> None:
    caller = ScriptedCaller({
        "plan": [NativeResult(outcome, {}, message="stop requested")],
    })
    baseline_orchestrator.call = caller
    original_head = Workspace.git(baseline_orchestrator.repo, "rev-parse", "HEAD")
    run_id, revision, status = baseline(baseline_orchestrator, SPEC, attempts=2)
    assert status == "paused" and revision == original_head
    assert len(caller.calls) == 1
    receipt = baseline_orchestrator.state.usage.calls(run_id)[0]
    assert receipt.outcome == outcome and receipt.attempt == 1


def test_attempt_cap_returns_last_safe_revision_after_repeated_test_failure(
    baseline_orchestrator: Orchestrator,
) -> None:
    caller = ScriptedCaller({
        "plan": [
            NativeResult("ok", {"files": BAD_FILES}),
            NativeResult("ok", {"files": BAD_FILES}),
        ],
    })
    baseline_orchestrator.call = caller
    original_head = Workspace.git(baseline_orchestrator.repo, "rev-parse", "HEAD")
    run_id, revision, status = baseline(baseline_orchestrator, SPEC, attempts=2)
    assert status == "failed" and revision == original_head
    assert len(caller.calls) == 2
    assert [receipt.attempt for receipt in baseline_orchestrator.state.usage.calls(run_id)] == [
        1, 2,
    ]
    assert Workspace.git(baseline_orchestrator.repo, "rev-parse", "HEAD") == original_head


@pytest.fixture
def scoped_orchestrator(baseline_orchestrator: Orchestrator) -> Orchestrator:
    orchestrator = baseline_orchestrator
    for file in BAD_FILES:
        (orchestrator.repo / file["path"]).write_text(file["content"])
    (orchestrator.repo / "aaa.py").write_text("# irrelevant seed\n" * 3000)
    Workspace.git(orchestrator.repo, "add", ".")
    Workspace.git(orchestrator.repo, "commit", "-m", "bug with trusted tests")
    return orchestrator


@pytest.mark.parametrize("sandbox", [False, True], ids=["injected-runner", "os-sandbox"])
def test_scoped_sparse_repair_keeps_seed_tests_and_uses_explicit_dsf_route(
    scoped_orchestrator: Orchestrator, sandbox: bool,
) -> None:
    orchestrator = scoped_orchestrator
    if sandbox:
        orchestrator.runner = LiveWorkspace.run_unittests
    caller = ScriptedCaller({"plan": [NativeResult("ok", {
        "files": [GOOD_FILES[0]], "need_files": [],
    })]})
    orchestrator.call = caller
    run_id, revision, status = baseline(
        orchestrator, "Fix adder.add to sum integers", route=DSF, attempts=1,
        context_mode="scoped", context_bytes=4000,
    )
    assert status == "done" and caller.calls[0][0] == "dsh"
    assert "return a - b" in caller.calls[0][2] and "irrelevant seed" not in caller.calls[0][2]
    assert orchestrator.sources(revision)["test_adder.py"] == GOOD_FILES[1]["content"]
    receipt = orchestrator.state.usage.calls(run_id)[0]
    assert (receipt.model, receipt.attempt, receipt.purpose) == (DSF.model, 1, "single")


def test_read_and_malformed_actions_each_consume_one_call_before_repair(
    scoped_orchestrator: Orchestrator,
) -> None:
    orchestrator = scoped_orchestrator
    replies = [
        NativeResult("ok", {"files": [], "need_files": ["adder.py"]}),
        NativeResult("ok", {"files": [], "need_files": []}),
        NativeResult("ok", {"files": [GOOD_FILES[0]], "need_files": []}),
    ]
    observed: list[tuple[bool, int]] = []

    def call(
        provider: str, model: str, prompt: str, schema: dict[str, object], run_id: str,
        cancelled: Callable[[], bool], *, effort: str = "",
    ) -> NativeResult:
        assert provider == DSF.provider and model == DSF.model and schema == SCOPED_SCHEMA
        assert ("return a - b" in prompt) == bool(observed)
        observed.append((any(c.id == run_id for c in orchestrator.state.usage.calls()),
                         len(DshClient.task_text(prompt, schema).encode())))
        return replies.pop(0)

    orchestrator.call = call
    run_id, _, status = baseline(orchestrator, "Repair addition", route=DSF, attempts=3,
                                 context_mode="scoped", context_bytes=4000)
    assert status == "done" and len(observed) == 3
    assert all(recorded and size <= 4000 for recorded, size in observed)
    assert [c.attempt for c in orchestrator.state.usage.calls(run_id)] == [1, 2, 3]


@pytest.mark.parametrize("data,reason", [
    ({"files": [GOOD_FILES[0]], "need_files": ["adder.py"]}, "exactly one"),
    ({"files": [], "need_files": ["../adder.py"]}, "safe existing"),
    ({"files": [GOOD_FILES[1]], "need_files": []}, "frozen"),
    ({"files": [{"path": "visible.py", "content": "VALUE = 0\n"}], "need_files": []},
     "Request complete current source"),
    ({"files": [], "need_files": ["adder.py"]}, "exhausted call limit"),
])
def test_invalid_or_final_read_actions_never_weaken_tests_or_overwrite_unseen_source(
    scoped_orchestrator: Orchestrator, data: dict[str, object], reason: str,
) -> None:
    orchestrator = scoped_orchestrator
    messages: list[str] = []
    orchestrator.notify = messages.append
    caller = ScriptedCaller({"plan": [NativeResult("ok", data)]})
    orchestrator.call = caller
    original = Workspace.git(orchestrator.repo, "rev-parse", "HEAD")
    run_id, revision, status = baseline(orchestrator, "Fix adder.py", route=DSF, attempts=1,
                                       context_mode="scoped", context_bytes=4000)
    assert status == "failed" and revision == original and len(caller.calls) == 1
    assert any(reason in message for message in messages)
    assert len(orchestrator.state.usage.calls(run_id)) == 1


@pytest.mark.parametrize("mode", ["full", "scoped"])
def test_cancelled_baseline_makes_no_call(
    baseline_orchestrator: Orchestrator, mode: str,
) -> None:
    orchestrator = baseline_orchestrator
    caller = ScriptedCaller()
    orchestrator.call = caller
    orchestrator.cancel.set()
    run_id, _, status = baseline(orchestrator, SPEC, route=DSF, context_mode=mode)
    assert status == "paused" and caller.calls == []
    assert orchestrator.state.usage.calls(run_id) == []


@pytest.mark.parametrize("outcome", sorted(STOP))
def test_scoped_stop_outcome_does_not_escalate_or_retry(
    scoped_orchestrator: Orchestrator, outcome: str,
) -> None:
    caller = ScriptedCaller({"plan": [NativeResult(outcome, {})]})
    scoped_orchestrator.call = caller
    _, _, status = baseline(scoped_orchestrator, "Fix adder.py", route=DSF, attempts=2,
                             context_mode="scoped")
    assert status == "paused" and len(caller.calls) == 1
    assert caller.calls[0][0] == "dsh"


@pytest.mark.parametrize("mode,limit", [("other", 4000), ("scoped", 2047),
                                        ("scoped", 32769), ("scoped", True)])
def test_invalid_context_configuration_fails_before_calls(
    baseline_orchestrator: Orchestrator, mode: str, limit: int,
) -> None:
    with pytest.raises(ValueError, match="context_"):
        baseline(baseline_orchestrator, SPEC, context_mode=mode, context_bytes=limit)
    assert baseline_orchestrator.state.usage.calls() == []


def test_oversized_spec_notifies_before_dispatch(baseline_orchestrator: Orchestrator) -> None:
    messages: list[str] = []
    baseline_orchestrator.notify = messages.append
    caller = ScriptedCaller()
    baseline_orchestrator.call = caller
    run_id, _, status = baseline(baseline_orchestrator, "文" * 2000, route=DSF,
                                 context_mode="scoped", context_bytes=2048)
    assert status == "failed" and caller.calls == []
    assert any("context_bytes" in message for message in messages)
    assert baseline_orchestrator.state.usage.calls(run_id) == []



def test_oversized_full_dsf_request_fails_before_receipt_or_dispatch(
    baseline_orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orchestragent.dsh.DshClient.input_limit", 40 * 1024)
    orchestrator = baseline_orchestrator
    messages: list[str] = []
    orchestrator.notify = messages.append
    caller = ScriptedCaller()
    orchestrator.call = caller
    run_id, _, status = baseline(orchestrator, "文" * 14000, route=DSF, context_mode="full")
    assert status == "failed" and caller.calls == []
    assert any("input_limit" in message for message in messages)
    assert orchestrator.state.usage.calls(run_id) == []
