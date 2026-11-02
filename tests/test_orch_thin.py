import copy
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any, cast

import pytest

from orchestragent import orch_verify
from orchestragent.native import ContextPrompt, NativeResult
from orchestragent.orch_build import SPEC_AUTHORITY
from orchestragent.orch_cli import main
from orchestragent.orch_generic import assemble, boundary_errors
from orchestragent.orch_policy import Tuning
from orchestragent.orch_run import (
    CLAUDE_PLAN,
    DSF,
    DSF_EXTENDED,
    Orchestrator,
    PlanError,
    Route,
)
from orchestragent.orch_state import Run
from orchestragent.orch_thin import (
    BOUNDARY_RULES,
    BOUNDARY_STAGE,
    boundary_fingerprint,
    boundary_prompt,
    boundary_review_prompt,
    canonical_boundary,
    citing_tasks,
    coverage_prompt,
    fill_requirements,
    integration_targets,
    review_verdict,
    spec_requirements,
)
from orchestragent.orch_thin import test_prompt as design_prompt
from orchestragent.orch_usage import Call, output_cut, unresolved
from orchestragent.orch_verify import Check
from orchestragent.store import Conflict
from orchestragent.workspace import Workspace
from tests.conftest import (
    ADD_CHECK,
    ADD_DESIGN,
    ADD_TEST,
    ADD_TESTS,
    DOUBLE_CHECK,
    DOUBLE_DESIGN,
    GOOD_ADD,
    GOOD_DOUBLE,
    ScriptedCaller,
    design,
    edit_result,
    local_runner,
)


def shard_file(tid: str, integration: bool = False) -> str:
    """Where the scripted test designers put a task's frozen test file."""
    return f"test_orch_{tid.lower().replace('-', '_')}{'_integration' if integration else ''}.py"


def shard(tid: str, source: str, integration: bool = False,
          usage: dict[str, object] | None = None) -> NativeResult:
    """A test design for `tid`: one unittest file and the command that runs exactly it."""
    path = shard_file(tid, integration)
    answer = design(path, source, f"python3 -m unittest -q {path.removesuffix('.py')}")
    return replace(answer, usage=usage) if usage else answer


def contract_test(module: str, expression: str, expected: object, rid: str) -> str:
    return (f"import unittest\nfrom {module} import {expression.split('(')[0]}\n"
            f"class TestContract(unittest.TestCase):\n    def test_{rid}_value(self):\n"
            f"        self.assertEqual({expression}, {expected!r})\n")


@pytest.mark.parametrize(("graph", "expected"), [
    ({"a": [], "b": ["a"], "c": ["b"]}, ["c"]),
    ({"a": [], "b": [], "join": ["a", "b"], "last": ["join"]}, ["join", "last"]),
    ({"a": [], "b": ["a"], "join": ["a", "b"], "last": ["join"]}, ["last"]),
    ({"a": [], "b": [], "join": ["a", "b"], "other": []}, ["join"]),
    ({}, []),
])
def test_integration_targets_cover_true_joins_without_redundant_ancestral_inputs(
    graph: dict[str, list[str]], expected: list[str],
) -> None:
    assert integration_targets(graph) == expected


def test_legacy_integration_scope_only_selects_terminal_outputs() -> None:
    graph = {"a": [], "b": [], "join": ["a", "b"], "last": ["join"]}
    assert integration_targets(graph, "terminal") == ["last"]
    with pytest.raises(ValueError, match="integration scope"):
        integration_targets(graph, "unknown")


def test_boundary_lint_feedback_does_not_change_cached_context() -> None:
    first = boundary_prompt("requirement", "repo", [])
    retry = boundary_prompt("requirement", "repo", ["invalid ownership"])
    assert isinstance(first, ContextPrompt) and isinstance(retry, ContextPrompt)
    assert first.shared == retry.shared
    assert "invalid ownership" in retry.dynamic and "invalid ownership" not in retry.shared


@pytest.fixture
def boundary(math_plan: dict[str, object]) -> dict[str, object]:
    return copy.deepcopy(math_plan)


@pytest.mark.parametrize("integration", [False, True])
def test_design_scope_preserves_upstream_requirements_without_unrelated_contracts(
    boundary: dict[str, object], integration: bool,
) -> None:
    requirements = cast(list[dict[str, str]], boundary["requirements"])
    requirements[0]["text"] = "PRODUCER_DOMAIN: positive, zero and negative inputs are valid"
    requirements.append({"id": "R9", "text": "UNRELATED_REQUIREMENT_SENTINEL"})
    tasks = cast(list[dict[str, Any]], boundary["tasks"])
    tasks.append({"id": "T-009", "goal": "unrelated", "contract": "UNRELATED_CONTRACT_SENTINEL",
                  "reqs": ["R9"], "files": ["unrelated.py"], "depends_on": [],
                  "interface_depends_on": [], "tests": []})
    original = copy.deepcopy(boundary)
    prompt = design_prompt("original spec", boundary, "repo map", [], task_id="T-002",
                           integration=integration)
    assert "PRODUCER_DOMAIN" in prompt and "positive, zero and negative" in prompt
    assert "UNRELATED_REQUIREMENT_SENTINEL" not in prompt
    assert "UNRELATED_CONTRACT_SENTINEL" not in prompt and boundary == original
    assert ('["T-001","T-002"]' in prompt) is integration


def revised(result: NativeResult, number: int) -> NativeResult:
    """An author revision; an unchanged resubmission is a named stop, not a new draft."""
    return NativeResult("ok", {**copy.deepcopy(result.data), "summary": f"revision {number}"})


@pytest.fixture
def alias_boundary(boundary: dict[str, object]) -> dict[str, object]:
    reqs = cast(list[dict[str, Any]], boundary["requirements"])
    tasks = cast(list[dict[str, Any]], boundary["tasks"])
    labels = {req["id"]: req["id"] + "part" for req in reqs}
    aliases = {task["id"]: "module_" + str(i) for i, task in enumerate(tasks)}
    for req in reqs:
        req["id"] = labels[req["id"]]
    for task in tasks:
        task["id"] = aliases[task["id"]]
        task["reqs"] = [labels[r] for r in task["reqs"]]
        task["depends_on"] = [aliases[d] for d in task["depends_on"]]
    tasks[1]["interface_depends_on"] = [tasks[0]["id"]]
    return boundary


def test_boundary_labels_are_assigned_once_before_tests_without_changing_meaning(
    alias_boundary: dict[str, object],
) -> None:
    before = copy.deepcopy(alias_boundary)
    fixed = canonical_boundary(alias_boundary)
    tasks = cast(list[dict[str, Any]], fixed["tasks"])
    assert boundary_errors(fixed, frozenset()) == []
    assert tasks[1]["depends_on"] == tasks[1]["interface_depends_on"] == ["T-001"]
    assert alias_boundary == before and canonical_boundary(fixed) == fixed
    assert fixed["files"] == before["files"]
    assert [r["text"] for r in cast(list[dict[str, Any]], fixed["requirements"])] == [
        r["text"] for r in cast(list[dict[str, Any]], before["requirements"])]
    assert [t["contract"] for t in tasks] == [
        t["contract"] for t in cast(list[dict[str, Any]], before["tasks"])]


@pytest.mark.parametrize("defect", ["duplicate", "missing", "frozen", "unhashable"])
def test_ambiguous_boundary_labels_are_never_guessed(
    alias_boundary: dict[str, object], defect: str,
) -> None:
    tasks = cast(list[dict[str, Any]], alias_boundary["tasks"])
    if defect == "duplicate":
        tasks[1]["id"] = tasks[0]["id"]
    elif defect == "missing":
        tasks[1]["depends_on"] = ["unknown"]
    elif defect == "frozen":
        tasks[0]["tests"] = [ADD_TEST]
    else:
        tasks[0]["id"] = []
    assert canonical_boundary(alias_boundary) == alias_boundary
    assert boundary_errors(alias_boundary, frozenset())


def test_assembly_preserves_explicit_interface_dependencies(
    boundary: dict[str, object],
) -> None:
    cast(list[dict[str, object]], boundary["tasks"])[1]["interface_depends_on"] = ["T-001"]
    assert boundary_errors(boundary, frozenset()) == []
    plan = assemble(boundary, [
        ("T-001", [{"path": "test_add.py", "content": ADD_TESTS}], ADD_CHECK.id),
        ("T-002", [{"path": "test_double.py", "content": "x"}], DOUBLE_CHECK.id),
    ], frozenset())
    tasks = cast(list[dict[str, object]], plan["tasks"])
    assert tasks[1]["interface_depends_on"] == ["T-001"]
    assert [task["tests"] for task in tasks] == [[ADD_CHECK.id], [DOUBLE_CHECK.id]]


@pytest.fixture
def distributed_case(
    orch_repo: Path, boundary: dict[str, object], unsandboxed: None,
) -> tuple[Orchestrator, ScriptedCaller]:
    files = cast(list[dict[str, str]], boundary["files"])
    existing = files.pop(0)
    (orch_repo / existing["path"]).write_text(existing["content"])
    Workspace.git(orch_repo, "add", existing["path"])
    Workspace.git(orch_repo, "commit", "-m", "existing implementation target")
    responses = {"plan": [NativeResult("ok", boundary)]}
    for tid, rid, module, expression, expected, build in [
        ("T-001", "r1", "adder", "add(2, 3)", 5, GOOD_ADD),
        ("T-002", "r2", "doubler", "double(4)", 8, GOOD_DOUBLE),
    ]:
        responses[f"dsh:{tid}"] = [shard(tid, contract_test(module, expression, expected, rid)),
                                   build]
    caller = ScriptedCaller(responses)
    barrier = Barrier(2)

    def call(provider: str, model: str, prompt: str, schema: dict[str, object], run_id: str,
             cancelled: Callable[[], bool], *, effort: str = "") -> NativeResult:
        if "-test-design-" in run_id and "-1-" in run_id:
            barrier.wait(timeout=5)
        return caller(provider, model, prompt, schema, run_id, cancelled, effort=effort)

    return Orchestrator(orch_repo, call, local_runner, notify=lambda _: None), caller


@pytest.fixture
def resumable_case(
    distributed_case: tuple[Orchestrator, ScriptedCaller],
) -> tuple[Orchestrator, ScriptedCaller]:
    orch, caller = distributed_case
    orch.call = caller  # resume cases run shards sequentially to control the crash boundary
    return orch, caller


@pytest.mark.parametrize("legacy", [False, True])
def test_distributed_policy_and_execution_settings_stay_pinned_through_planning_resume(
    resumable_case: tuple[Orchestrator, ScriptedCaller], legacy: bool,
) -> None:
    orch, caller = resumable_case
    rule = orch.state.add_rule("Keep public inputs and exceptions explicit.", "prior", "planner")
    assert rule is not None
    orch.state.save_rule(replace(rule, status="active"))
    original = None if legacy else orch.policies.enable(
        tuning=Tuning(jobs=1, test_jobs=1, verify_jobs=1, cache_mode="off"))
    caller.responses["dsh:T-002"].insert(0, NativeResult("auth_required", {}))
    with pytest.raises(PlanError, match="auth_required"):
        orch.plan("add and double", test_jobs=1 if legacy else None)
    run_id = orch.state.usage.calls()[0].run_id
    if legacy:
        orch.policies.enable(tuning=Tuning(jobs=8))
        with orch.state.store.transaction() as db:
            row = db.execute("SELECT body FROM orch_planning_sessions WHERE id=?",
                             (run_id,)).fetchone()
            body = json.loads(row[0])
            body.pop("policy_id")
            db.execute("UPDATE orch_planning_sessions SET body=? WHERE id=?",
                       (json.dumps(body), run_id))
    else:
        candidate = orch.policies.snapshot(tuning=Tuning(jobs=8))
        # A concurrent activation must not replace the interrupted run's inputs.
        with orch.state.store.transaction() as db:
            db.execute("UPDATE orch_policy_head SET id=?", (candidate.id,))
    orch = Orchestrator(orch.repo, caller, local_runner, notify=lambda _: None)
    run = orch.resume_plan(run_id)
    assert run.policy_id == (original.id if original else "")
    assert caller.keys().count("plan") == caller.keys().count("dsh:T-001") == 1
    planner_prompt = next(prompt for provider, _, prompt in caller.calls if provider == "claude")
    assert (rule.text in planner_prompt) is (not legacy)
    assert orch.execute(run.id).status == "done"
    started = next(e.data for e in orch.state.events(run.id) if e.kind == "run.started")
    assert (started["jobs"], started["cache_mode"]) == ((1, "off") if original else (4, "auto"))


@pytest.fixture
def integration_case(
    resumable_case: tuple[Orchestrator, ScriptedCaller],
) -> tuple[Orchestrator, ScriptedCaller]:
    orch, caller = resumable_case
    boundary = caller.responses["plan"][0].data
    cast(list[dict[str, object]], boundary["requirements"]).append({
        "id": "R3", "text": "quad(n) composes add and double to return four times n"})
    cast(list[dict[str, object]], boundary["files"]).append({
        "path": "quad.py", "content": "from adder import add\nfrom doubler import double\n"
        "def quad(n: int) -> int:\n    raise NotImplementedError\n"})
    cast(list[dict[str, object]], boundary["tasks"]).append({
        "id": "T-003", "goal": "compose add and double", "reqs": ["R3"], "files": ["quad.py"],
        "depends_on": ["T-001", "T-002"], "interface_depends_on": [], "tests": [],
        "contract": "quad(n) returns 4*n"})
    designs = []
    for integration, value in ((True, 2), (False, 0)):  # integration shards dispatch first
        source = ("import unittest\nfrom quad import quad\n"
                  "class Composition(unittest.TestCase):\n    def test_r3_composed(self):\n"
                  f"        self.assertEqual(quad({value}), {4 * value})\n")
        designs.append(shard("T-003", source, integration))
    caller.responses["dsh:T-003"] = [*designs, edit_result("quad.py", "return 0"),
                                    edit_result("quad.py", "return add(double(n), double(n))")]
    return orch, caller


@pytest.fixture
def middle_join_case(
    integration_case: tuple[Orchestrator, ScriptedCaller],
) -> tuple[Orchestrator, ScriptedCaller]:
    orch, caller = integration_case
    boundary = caller.responses["plan"][0].data
    files = cast(list[dict[str, str]], boundary["files"])
    next(file for file in files if file["path"] == "doubler.py")["content"] = (
        "def double(a: int) -> int:\n    raise NotImplementedError\n")
    tasks = cast(list[dict[str, Any]], boundary["tasks"])
    tasks[1].update(depends_on=[], contract="double(a) == 2*a")
    caller.responses["dsh:T-002"][-1] = edit_result("doubler.py", "return 2*a")
    cast(list[dict[str, str]], boundary["requirements"]).append({
        "id": "R4", "text": "render(n) is the string form of quad(n)"})
    files.append({"path": "rendered.py", "content": "from quad import quad\n"
                  "def render(n: int) -> str:\n    raise NotImplementedError\n"})
    tasks.append({"id": "T-004", "goal": "render quad", "reqs": ["R4"],
                  "files": ["rendered.py"], "depends_on": ["T-003"], "interface_depends_on": [],
                  "tests": [], "contract": "render(n) returns str(quad(n))"})
    source = ("import unittest\nfrom rendered import render\n"
              "class Render(unittest.TestCase):\n    def test_r4_render(self):\n"
              "        self.assertEqual(render(0), '0')\n")
    caller.responses["dsh:T-004"] = [
        *[shard("T-004", source, integration) for integration in (True, False)],
        edit_result("rendered.py", "return str(quad(n))"),
    ]
    return orch, caller


@pytest.mark.parametrize("legacy", [False, True])
def test_mid_graph_integration_is_pinned_and_legacy_resume_does_not_buy_new_shards(
    middle_join_case: tuple[Orchestrator, ScriptedCaller], legacy: bool,
) -> None:
    orch, caller = middle_join_case
    # the first shard dispatched (deepest integration), so nothing else is bought before it
    caller.responses["dsh:T-004"].insert(0, NativeResult("auth_required", {}))
    with pytest.raises(PlanError, match="auth_required"):
        orch.plan("Compose independent producers then render the result", test_jobs=1)
    run_id = orch.state.usage.calls()[0].run_id
    if legacy:
        with orch.state.store.transaction() as db:
            row = db.execute("SELECT body FROM orch_planning_sessions WHERE id=?",
                             (run_id,)).fetchone()
            body = json.loads(row[0])
            body.pop("integration_scope")
            db.execute("UPDATE orch_planning_sessions SET body=? WHERE id=?",
                       (json.dumps(body), run_id))
        caller.responses["dsh:T-003"].pop(0)  # legacy terminal-only work was already frozen
    resumed = Orchestrator(orch.repo, caller, local_runner, notify=lambda _: None)
    resumed.resume_plan(run_id)
    assert resumed.planning.session(run_id).integration_scope == ("terminal" if legacy else "joins")
    assert len(resumed.state.task(run_id, "T-003").tests) == (1 if legacy else 2)
    assert resumed.execute(run_id, jobs=1).status == "done"
    failures = [e for e in resumed.state.events(run_id, "T-003") if e.kind == "task.failed"]
    assert len(failures) == int(not legacy)
    assert caller.keys().count("plan") == 1
    designs = [c for c in resumed.state.usage.calls(run_id)
               if c.purpose == "integration-test-design" and c.outcome == "ok"]
    assert len(designs) == (1 if legacy else 2)


@pytest.mark.parametrize("mode", ["enabled", "disabled", "resume", "legacy_resume"])
def test_independent_integration_shard_gates_composed_code_and_resumes_without_rebilling(
    integration_case: tuple[Orchestrator, ScriptedCaller], mode: str,
) -> None:
    orch, caller = integration_case
    enabled = mode in {"enabled", "resume"}
    responses = caller.responses["dsh:T-003"]
    if not enabled:
        responses.pop(0)  # the integration design, which dispatches first when enabled
    if "resume" in mode:
        responses.insert(0, NativeResult("auth_required", {}))
        with pytest.raises(PlanError, match="auth_required"):
            orch.plan("Compose add, double and quad", test_jobs=1, integration_checks=enabled)
        run_id = orch.state.usage.calls()[0].run_id
        if mode == "legacy_resume":
            with orch.state.store.transaction() as db:
                row = db.execute("SELECT body FROM orch_planning_sessions WHERE id=?",
                                 (run_id,)).fetchone()
                body = json.loads(row[0])
                body.pop("integration_checks")
                db.execute("UPDATE orch_planning_sessions SET body=? WHERE id=?",
                           (json.dumps(body), run_id))
        orch = Orchestrator(orch.repo, caller, local_runner, notify=lambda _: None)
        run = orch.resume_plan(run_id)
    else:
        run = orch.plan("Compose add, double and quad", test_jobs=1, integration_checks=enabled)
    session = orch.planning.session(run.id)
    task = orch.state.task(run.id, "T-003")
    assert session.integration_checks is enabled and len(task.tests) == (2 if enabled else 1)
    assert all(orch_verify.is_check(test) for test in task.tests)
    assert caller.keys().count("plan") == caller.keys().count("dsh:T-001") == 1
    calls = orch.state.usage.calls(run.id)
    assert sum(c.purpose == "integration-test-design" for c in calls) == (
        2 if mode == "resume" else int(enabled))
    completed = orch.execute(run.id, jobs=1)
    assert completed.status == "done"
    failures = [e for e in orch.state.events(run.id, "T-003") if e.kind == "task.failed"]
    assert len(failures) == int(enabled)
    source = Workspace.git(orch.repo, "show", f"{completed.verified_head}:quad.py")
    assert ("return add(double(n), double(n))" in source) is enabled


@pytest.mark.parametrize("outcome", ["auth_required", "timeout"])
def test_planning_resume_reuses_boundary_and_finished_shards_without_rebilling(
    resumable_case: tuple[Orchestrator, ScriptedCaller], outcome: str,
) -> None:
    orch, caller = resumable_case
    caller.responses["dsh:T-002"].insert(0, NativeResult(outcome, {}))
    with pytest.raises(PlanError, match=outcome):
        orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    calls = orch.state.usage.calls()
    run_id, old_base = calls[0].run_id, calls[0].base_revision
    (orch.repo / "unrelated.txt").write_text("a later user commit\n")
    Workspace.git(orch.repo, "add", "unrelated.txt")
    Workspace.git(orch.repo, "commit", "-m", "user moves HEAD while planning is interrupted")
    resumed = Orchestrator(orch.repo, caller, local_runner, notify=lambda _: None)
    if outcome == "timeout":
        with pytest.raises(Conflict, match="Unresolved planning call"):
            resumed.resume_plan(run_id)
        assert len(caller.calls) == len(calls)
    completed = resumed.resume_plan(
        run_id, ack_unknown_calls=[calls[-1].id] if outcome == "timeout" else [],
    )
    assert completed.base == old_base and completed.status == "planned"
    assert caller.keys().count("plan") == caller.keys().count("dsh:T-001") == 1
    assert caller.keys().count("dsh:T-002") == 2
    assert len(resumed.state.usage.calls(run_id)) == 4
    original = next(c for c in resumed.state.usage.calls(run_id) if c.id == calls[-1].id)
    assert unresolved(original.outcome, original.usage) == (outcome == "timeout")
    assert resumed.resume_plan(run_id).checkpoint == completed.checkpoint
    assert len(caller.calls) == 4


def test_planning_response_and_receipt_roll_back_together_on_storage_failure(
    resumable_case: tuple[Orchestrator, ScriptedCaller], monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch, caller = resumable_case
    finish = orch.planning.finish

    def crash(db: sqlite3.Connection, call: Call, result: NativeResult | None) -> None:
        finish(db, call, result)
        if call.task_id == "T-002":  # the first shard dispatched: it has the larger closure
            raise RuntimeError("injected after response write before receipt transaction commit")

    monkeypatch.setattr(orch.planning, "finish", crash)
    with pytest.raises(RuntimeError, match="injected"):
        orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    calls = orch.state.usage.calls()
    assert len(calls) == 2  # the queued second shard never dispatches after the storage fault
    pending = next(c for c in calls if c.task_id == "T-002")
    assert pending.outcome == "pending" and pending.finished_at is None
    with orch.state.store.transaction() as db:
        row = db.execute("SELECT response FROM orch_planning_calls WHERE call_id=?",
                         (pending.id,)).fetchone()
    assert row is not None and row["response"] is None
    resumed = Orchestrator(orch.repo, caller, local_runner, notify=lambda _: None)
    with pytest.raises(Conflict, match="Unresolved planning call"):
        resumed.resume_plan(pending.run_id)
    assert len(caller.calls) == len(calls)


@pytest.mark.parametrize("change", ["route", "schema"])
def test_planning_resume_rejects_changed_request_without_dispatch(
    resumable_case: tuple[Orchestrator, ScriptedCaller], monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    orch, caller = resumable_case
    caller.responses["dsh:T-002"].insert(0, NativeResult("auth_required", {}))
    with pytest.raises(PlanError):
        orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    calls = orch.state.usage.calls()
    if change == "route":
        monkeypatch.setattr("orchestragent.orch_run.PLANNER",
                            replace(CLAUDE_PLAN, effort="max"))
    else:
        monkeypatch.setattr("orchestragent.orch_run.PLAN_SCHEMA", {"type": "object"})
    resumed = Orchestrator(orch.repo, caller, local_runner, notify=lambda _: None)
    with pytest.raises((Conflict, PlanError), match="changed"):
        resumed.resume_plan(calls[0].run_id)
    assert len(caller.calls) == len(calls)


@pytest.mark.parametrize("reviewed", [False, True])
def test_cli_resumes_pinned_planning_and_requires_unknown_call_authorization(
    resumable_case: tuple[Orchestrator, ScriptedCaller], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], reviewed: bool,
) -> None:
    orch, caller = resumable_case
    caller.responses["dsh:T-002"].insert(0, NativeResult("timeout", {}))
    if reviewed:
        caller.responses["plan"].append(NativeResult("ok", {"approved": True, "issues": []}))
    with pytest.raises(PlanError):
        orch.plan("Implement adder.add and doubler.double", test_jobs=1,
                  reviewed_boundary=reviewed)
    calls = orch.state.usage.calls()
    clients = SimpleNamespace(call=caller, jev=SimpleNamespace(
        status=lambda: {"ready": False}, decide=None), close=lambda: None)
    monkeypatch.setattr("orchestragent.orch_cli.providers", lambda _: clients)
    monkeypatch.setattr(orch_verify, "run", local_runner)
    args = ["--repo", str(orch.repo), "plan", "--resume", calls[0].run_id]
    assert main(args) == 1
    assert "Unresolved planning call" in capsys.readouterr().err
    assert main([*args, "--ack-unknown-call", calls[-1].id]) == 0
    assert "planned" in capsys.readouterr().out
    assert len(caller.calls) == (5 if reviewed else 4)


@pytest.mark.parametrize("repair", ["none", "lint", "review", "settled-json", "mixed"])
def test_reviewed_boundary_keeps_structural_repairs_on_dsf_and_semantics_on_planner(
    resumable_case: tuple[Orchestrator, ScriptedCaller], repair: str,
) -> None:
    orch, caller = resumable_case
    boundary = caller.responses["plan"][0]
    approved = NativeResult("ok", {"approved": True, "issues": []})
    responses = [boundary, approved]
    if repair in {"review", "mixed"}:
        responses = [boundary, NativeResult("ok", {"approved": False, "issues": [
            "T-001 must preserve the original add error semantics required by R1."]}),
            revised(boundary, 1), approved]
    elif repair == "lint":
        invalid = copy.deepcopy(boundary)
        cast(list[dict[str, object]], invalid.data["tasks"])[0]["depends_on"] = ["T-001"]
        responses.insert(0, invalid)
    if repair in {"settled-json", "mixed"}:
        responses.insert(0, NativeResult("failed", {}, message="Invalid DeepSeek JSON result"))
    caller.responses["plan"] = responses
    run = orch.plan("Implement adder.add and doubler.double", test_jobs=1,
                    reviewed_boundary=True, attempts=2)
    calls = orch.state.usage.calls(run.id)
    draft = [c for c in calls if c.purpose == "boundary-draft"]
    review = [c for c in calls if c.purpose == "boundary-review"]
    assert len(draft) == (1 if repair == "none" else 3 if repair == "mixed" else 2)
    assert len(review) == (2 if repair in {"review", "mixed"} else 1)
    assert all(c.provider == "dsh" for c in draft) and all(c.provider == "claude" for c in review)
    assert not any(c.purpose == "plan" for c in calls)
    assert run.status == "planned" and orch.planning.session(run.id).reviewed_boundary
    if repair in {"review", "mixed"}:
        assert any("PREVIOUS REVIEWED DRAFT" in text and "add error semantics" in text
                   for provider, _, text in caller.calls if provider == "dsh")


@pytest.mark.parametrize(("failure", "calls", "code"), [
    ("json", 2, "structural_budget"),
    ("review", 4, "review_budget"),
    ("unchanged", 3, "repeated_boundary"),
])
def test_reviewed_planning_limits_each_failure_class_and_resume_does_not_refill_it(
    resumable_case: tuple[Orchestrator, ScriptedCaller], failure: str, calls: int, code: str,
) -> None:
    orch, caller = resumable_case
    draft = caller.responses["plan"][0]
    rejected = NativeResult("ok", {"approved": False, "issues": ["T-001 violates R1"]})
    caller.responses["plan"] = {
        "json": [NativeResult("failed", {}, message="Invalid DSH JSON result")] * 2,
        "review": [draft, rejected, revised(draft, 1), rejected],
        "unchanged": [draft, rejected, draft],
    }[failure]
    with pytest.raises(PlanError, match=rf"\({code}\)"):
        orch.plan("Implement adder.add and doubler.double", reviewed_boundary=True, attempts=2)
    count = len(caller.calls)
    run_id = orch.state.usage.calls()[0].run_id
    with pytest.raises(PlanError, match=rf"\({code}\)"):
        orch.resume_plan(run_id)
    assert len(caller.calls) == count == calls
    assert json.loads((orch.run_dir(run_id) / "planning" / "stop.json").read_text())[
        "code"] == code


@pytest.fixture
def ownership_drafts(
    resumable_case: tuple[Orchestrator, ScriptedCaller],
) -> dict[str, NativeResult]:
    """Two tasks claiming R1, the legal repair, and an unrelated structural defect."""
    valid = resumable_case[1].responses["plan"][0]
    duplicate, cycle = copy.deepcopy(valid.data), copy.deepcopy(valid.data)
    cast(list[dict[str, Any]], duplicate["tasks"])[1]["reqs"] = ["R1", "R2"]
    cast(list[dict[str, Any]], cycle["tasks"])[0]["depends_on"] = ["T-002"]
    return {"duplicate": NativeResult("ok", duplicate), "valid": valid,
            "cycle": NativeResult("ok", cycle)}


def test_default_plan_is_opus_boundary_then_dsf_tests_after_named_repair(
    resumable_case: tuple[Orchestrator, ScriptedCaller], ownership_drafts: dict[str, NativeResult],
) -> None:
    orch, caller = resumable_case
    caller.responses["plan"] = [ownership_drafts["duplicate"], ownership_drafts["valid"]]
    run = orch.execute(orch.plan("Implement adder.add and doubler.double", test_jobs=1,
                                 attempts=2).id)
    session = orch.planning.session(run.id)
    calls = orch.state.usage.calls(run.id)
    authors = [text for provider, key, text in caller.calls if key == "plan"]
    assert run.status == "done" and not session.reviewed_boundary
    assert (session.planner, session.effort, session.designer) == (
        CLAUDE_PLAN.label, "medium", "dsh/deepseek-flash")
    assert sorted({(c.provider, c.model, c.effort, c.purpose) for c in calls
                   if c.scope == "planning"}) == [
        ("claude", "claude-opus-5-5", "medium", "plan"),
        ("dsh", "deepseek-flash", DSF_EXTENDED.effort, "test-design")]
    assert all(c.provider == "dsh" for c in calls if c.scope == "execution")
    assert "R1: accepted by both T-001 and T-002" in authors[1]
    assert all(BOUNDARY_RULES in text for text in authors)


@pytest.mark.parametrize(("drafts", "attempts", "code"), [
    (["duplicate", "duplicate"], 3, "repeated_boundary"),
    (["duplicate", "duplicate-renamed"], 3, "repeated_structural_error"),
    (["duplicate", "cycle"], 2, "structural_budget"),
])
def test_opus_boundary_stops_are_named_and_resume_replays_them_without_dispatch(
    resumable_case: tuple[Orchestrator, ScriptedCaller], ownership_drafts: dict[str, NativeResult],
    drafts: list[str], attempts: int, code: str,
) -> None:
    orch, caller = resumable_case
    ownership_drafts["duplicate-renamed"] = revised(ownership_drafts["duplicate"], 1)
    caller.responses["plan"] = [ownership_drafts[name] for name in drafts]
    with pytest.raises(PlanError, match=rf"plan failed lint \({code}\)"):
        orch.plan("Implement adder.add and doubler.double", attempts=attempts)
    run_id = orch.state.usage.calls()[0].run_id
    resumed = Orchestrator(orch.repo, caller, local_runner, notify=lambda _: None)
    with pytest.raises(PlanError, match=rf"\({code}\)"):
        resumed.resume_plan(run_id)
    stop = json.loads((orch.run_dir(run_id) / "planning" / "stop.json").read_text())
    assert caller.keys() == ["plan", "plan"] and stop["code"] == code
    assert {c.provider for c in orch.state.usage.calls(run_id)} == {"claude"}


def test_revision_keeps_structural_errors_and_review_findings_for_the_author(
    resumable_case: tuple[Orchestrator, ScriptedCaller], ownership_drafts: dict[str, NativeResult],
) -> None:
    orch, caller = resumable_case
    caller.responses["plan"] = [
        ownership_drafts["duplicate"], ownership_drafts["valid"],
        NativeResult("ok", {"approved": False, "issues": ["R2 contract omits ordering"]}),
        revised(ownership_drafts["valid"], 1), NativeResult("ok", {"approved": True, "issues": []}),
    ]
    run = orch.plan("Implement adder.add and doubler.double", test_jobs=1,
                    reviewed_boundary=True, attempts=2)
    last, review = (next(text for provider, key, text in reversed(caller.calls)
                         if provider == route and key == "plan") for route in ("dsh", "claude"))
    assert run.status == "planned"
    assert "R1: accepted by both T-001 and T-002" in last and "R2 contract omits ordering" in last
    assert BOUNDARY_RULES in review


@pytest.mark.parametrize(("mutation", "message"), [
    ("duplicate", "R1: accepted by both T-001 and T-002"),
    ("cycle", "depends_on must be acyclic"),
    ("writer", "doubler.py: written by both T-001 and T-002"),
    ("unclaimed", "requirements without an accepting task: R2"),
])
def test_boundary_errors_name_requirements_tasks_and_files(
    boundary: dict[str, object], mutation: str, message: str,
) -> None:
    tasks = cast(list[dict[str, Any]], boundary["tasks"])
    if mutation == "duplicate":
        tasks[0]["reqs"] = tasks[1]["reqs"] = ["R1", "R2"]
    elif mutation == "cycle":
        tasks[0]["depends_on"] = ["T-002"]
    elif mutation == "writer":
        tasks[0]["files"] = ["adder.py", "doubler.py"]
    else:
        tasks[1]["reqs"] = ["R1"]
        tasks[0]["reqs"] = []
    assert any(message in error for error in boundary_errors(boundary, frozenset()))


@pytest.fixture
def bench_specs() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1] / "examples" / "orch-bench"
    return {path.parent.name: path.read_text() for path in root.glob("*/spec.md")}


@pytest.mark.parametrize(("case", "ids", "inside"), [
    ("boltons-checkpoint", ["R1", "R2", "R3", "R4"],
     {"R4": "- ingest(file_obj, source_id)", "R1": "and is idempotent."}),
    ("boltons-buildgraph", [f"R{n}" for n in range(1, 9)],
     {"R3": "`fingerprint(task_id, dependency_keys)`"}),
    ("networkx-timeflow", None, {}),  # a wrapped line also starts with "R1. "
])
def test_spec_requirements_keep_whole_labelled_blocks_or_report_unsupported(
    bench_specs: dict[str, str], case: str, ids: list[str] | None, inside: dict[str, str],
) -> None:
    texts = spec_requirements(bench_specs[case])
    assert (sorted(texts, key=lambda rid: int(rid[1:])) if texts is not None else None) == ids
    assert all(fragment in cast(dict[str, str], texts)[rid] for rid, fragment in inside.items())


@pytest.mark.parametrize("spec", [
    "R1. Parse records.\n```python\nR1. Not a label inside a fence\n```\nR2. Save.\n",
    "R1. Parse records:\n```python\nparse(b'x')  # important: bytes only\n```\n"
    "Reject text input.\nR2. Save.\n",
    "R1. Parse records.\n### Error handling\nReject text input.\nR2. Save.\n",
    "R1. Parse records.\n~~~\nkeep = True\n~~~\nR2. Save.\n",
])
def test_ambiguous_spec_layouts_are_unsupported_and_leave_planner_text_unchanged(
    boundary: dict[str, object], spec: str,
) -> None:
    reqs = cast(list[dict[str, str]], boundary["requirements"])
    reqs[0]["text"] = ""
    before = copy.deepcopy(boundary)
    assert spec_requirements(spec) is None
    assert fill_requirements(boundary, spec_requirements(spec)) is boundary == before
    assert '"spec_requirement_ids":[]' in cast(ContextPrompt, boundary_prompt(spec, "r", [])).shared


def test_planner_may_leave_spec_requirement_text_to_the_controller(
    boundary: dict[str, object],
) -> None:
    spec = "# Feature\nR1. Add two ints.\nKeep it exact.\n\nAlso keep inputs unchanged.\n"
    reqs = cast(list[dict[str, str]], boundary["requirements"])
    reqs[0]["text"] = ""
    filled = fill_requirements(boundary, spec_requirements(spec))
    prompt = boundary_prompt(spec, "repo", [])
    assert isinstance(prompt, ContextPrompt) and '"spec_requirement_ids":["R1"]' in prompt.shared
    assert [r["text"] for r in cast(list[dict[str, str]], filled["requirements"])] == [
        "Add two ints.\nKeep it exact.\n\nAlso keep inputs unchanged.", "double an integer"]
    assert reqs[0]["text"] == "" and boundary_errors(filled, frozenset()) == []


def test_saved_timeflow_drafts_replay_to_named_ownership_and_a_repeated_fingerprint() -> None:
    saved = json.loads((Path(__file__).resolve().parents[1] / "docs" / "measurements"
                        / "2026-10-31-timeflow-cluster-planning.json").read_text())
    drafts = [json.loads(item["response"])["data"] for item in saved
              if item["phase"].startswith('["boundary-draft"')]
    results = []
    for draft in drafts:
        declared = {file["path"] for file in draft["files"]}
        existing = frozenset(path for task in draft["tasks"] for path in task["files"]
                             if path not in declared)
        results.append(boundary_errors(draft, existing, existing_edits=True))
    assert [len(errors) for errors in results] == [1, 0, 1]
    assert "R8: accepted by both T-007 and T-009" in results[0][0]
    assert "R8: accepted by both T-008 and T-009" in results[2][0]
    assert boundary_fingerprint(results[0]) == boundary_fingerprint(results[2])


def test_boundary_fingerprint_ignores_task_numbering_and_order() -> None:
    first = ["T-001: a.py imports b.py, owned by T-002; add T-002 to depends_on", "R3: x"]
    second = ["R3: x", "T-004: a.py imports b.py, owned by T-007; add T-007 to depends_on"]
    assert boundary_fingerprint(first) == boundary_fingerprint(second)
    assert boundary_fingerprint(first) != boundary_fingerprint(["R4: x"])


SHARD_USAGE: dict[str, object] = {"input_tokens": 10, "output_tokens": 5, "settled_cents": 1,
                                  "forwarded": 1}
DANGLING = NativeResult(  # a closed content string followed by a stray member
    "failed", {}, usage=SHARD_USAGE, message="Invalid DSH JSON result: Expecting ':' delimiter",
    text='{"files":[{"path":"test_orch_t_001.py","content":"import unittest\\n","\\n"}]}')
# Passes on the stubs, so it proves nothing (red gate).
VACUOUS = shard("T-001", "import unittest\nclass Vacuous(unittest.TestCase):\n"
                "    def test_r1_nothing(self):\n        pass\n", usage=SHARD_USAGE)
# A runner the sandbox does not have: exit 127 (red gate).
NOT_RUNNABLE = replace(design(shard_file("T-001"), contract_test("adder", "add(2, 3)", 5, "r1"),
                              "no-such-test-runner test_orch_t_001.py"), usage=SHARD_USAGE)


@pytest.mark.parametrize(("failures", "attempts", "calls", "stop", "reason"), [
    ([DANGLING], 2, 2, "", ""),
    ([DANGLING, DANGLING], 2, 3, "", ""),
    ([DANGLING, DANGLING, DANGLING], 2, 3, "lint_budget", "invalid/truncated JSON"),
    ([DANGLING, VACUOUS, VACUOUS], 3, 3, "repeated_lint_error", "prove nothing"),
    ([VACUOUS, NOT_RUNNABLE], 2, 2, "lint_budget", "could not run (exit 127)"),
], ids=["format-once", "format-twice", "persistent-format", "format-then-repeated-red",
        "red-failures-share-the-revision-budget"])
def test_shard_format_repairs_and_lint_revisions_have_separate_named_bounds(
    resumable_case: tuple[Orchestrator, ScriptedCaller], failures: list[NativeResult],
    attempts: int, calls: int, stop: str, reason: str,
) -> None:
    orch, caller = resumable_case
    caller.responses["dsh:T-001"][:0] = failures
    if stop:
        with pytest.raises(PlanError, match=rf"test-designer T-001 failed lint \({stop}\)") as err:
            orch.plan("Implement adder.add and doubler.double", test_jobs=1, attempts=attempts)
        assert reason in err.value.errors[0]
    else:
        orch.plan("Implement adder.add and doubler.double", test_jobs=1, attempts=attempts)
    designed = [c for c in orch.state.usage.calls() if c.task_id == "T-001"]
    prompts = [text for _, key, text in caller.calls if key == "dsh:T-001"]
    assert [c.attempt for c in designed] == list(range(1, calls + 1))
    assert all(c.effort == DSF_EXTENDED.effort for c in designed)
    assert {c.purpose for c in designed} == {"test-design"}
    if failures[0] is DANGLING:
        assert "error_char=" in prompts[1] and "invalid/truncated JSON" in prompts[1]


CUT_SHARD = replace(DANGLING, usage={**SHARD_USAGE, "stop_reason": "max_tokens",
                                     "turn_end": "max-tokens"})
UNSETTLED_CUT = replace(CUT_SHARD, usage={"forwarded": 1, "reserved_cents": 300,
                                          "stop_reason": "max_tokens"})


@pytest.mark.parametrize(("failures", "reservation", "efforts", "stop"), [
    ([], 300, [DSF_EXTENDED.effort], ""),
    ([CUT_SHARD], 300, [DSF_EXTENDED.effort], "failed lint (output_limit)"),
    ([], 100, [DSF.effort], ""),
    ([CUT_SHARD], 100, [DSF.effort], "failed lint (output_limit)"),
    ([UNSETTLED_CUT], 300, [DSF_EXTENDED.effort], "returned failed"),
], ids=["covered-request-starts-wide", "cut-at-widest-stops", "uncovered-request-keeps-base",
        "uncovered-cut-stops", "unknown-cut-not-settled"])
def test_shard_starts_at_the_widest_covered_budget_and_a_confirmed_cut_stops(
    resumable_case: tuple[Orchestrator, ScriptedCaller], monkeypatch: pytest.MonkeyPatch,
    failures: list[NativeResult], reservation: int, efforts: list[str], stop: str,
) -> None:
    orch, caller = resumable_case
    monkeypatch.setattr("orchestragent.orch_run.DSF_RESERVATION", reservation)
    caller.responses["dsh:T-001"][:0] = failures
    if stop:
        with pytest.raises(PlanError, match=rf"test-designer T-001 {re.escape(stop)}"):
            orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    else:
        orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    designed = [c for c in orch.state.usage.calls() if c.task_id == "T-001"]
    assert [c.effort for c in designed] == efforts
    assert [c.attempt for c in designed] == list(range(1, len(efforts) + 1))
    assert all(output_cut(failure.usage) for failure in failures)


def test_shard_output_limit_stop_replays_on_resume_without_new_calls(
    resumable_case: tuple[Orchestrator, ScriptedCaller],
) -> None:
    orch, caller = resumable_case
    caller.responses["dsh:T-001"][:0] = [CUT_SHARD]
    with pytest.raises(PlanError, match=r"failed lint \(output_limit\)"):
        orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    count = len(caller.calls)
    run_id = orch.state.usage.calls()[0].run_id
    resumed = Orchestrator(orch.repo, caller, local_runner, notify=lambda _: None)
    with pytest.raises(PlanError, match=r"failed lint \(output_limit\)"):
        resumed.resume_plan(run_id)
    assert len(caller.calls) == count and caller.keys().count("dsh:T-001") == 1


@pytest.mark.parametrize(("rejected", "problem"), [
    (VACUOUS, "prove nothing"), (NOT_RUNNABLE, "could not run (exit 127)")])
def test_red_gate_returns_a_vacuous_or_unrunnable_check_to_the_designer_before_freezing(
    resumable_case: tuple[Orchestrator, ScriptedCaller], rejected: NativeResult, problem: str,
) -> None:
    orch, caller = resumable_case
    caller.responses["dsh:T-001"].insert(0, rejected)
    run = orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    prompts = [text for _, key, text in caller.calls if key == "dsh:T-001"]
    assert run.status == "planned" and len(prompts) == 2
    assert problem in prompts[1]
    [frozen] = orch.state.task(run.id, "T-001").tests
    assert cast(Check, orch_verify.parse(frozen)).command == (
        "python3 -m unittest -q test_orch_t_001")


def test_shards_start_deepest_and_integration_first_but_assemble_in_plan_order(
    integration_case: tuple[Orchestrator, ScriptedCaller],
) -> None:
    orch, caller = integration_case
    run = orch.plan("Compose add, double and quad", test_jobs=1)
    dispatched = [(c.task_id, c.purpose) for c in orch.state.usage.calls(run.id)
                  if c.provider == "dsh"]
    assert dispatched == [("T-003", "integration-test-design"), ("T-003", "test-design"),
                          ("T-002", "test-design"), ("T-001", "test-design")]
    tests = [cast(dict[str, str], file)["path"]
             for file in cast(list[object], run.plan["files"]) if "test_orch" in str(file)]
    assert tests == [shard_file("T-001"), shard_file("T-002"), shard_file("T-003"),
                     shard_file("T-003", True)]


@pytest.mark.parametrize(("interruption", "resumes"), [
    (NativeResult("auth_required", {}), True), (NativeResult("timeout", {}), False)])
def test_redesign_resumes_after_the_settled_draft_and_never_replays_unknown_calls(
    resumable_case: tuple[Orchestrator, ScriptedCaller], interruption: NativeResult,
    resumes: bool,
) -> None:
    orch, caller = resumable_case
    caller.responses["dsh:T-001"][:0] = [VACUOUS, interruption]
    with pytest.raises(PlanError, match=interruption.outcome):
        orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    run_id = orch.state.usage.calls()[0].run_id
    resumed = Orchestrator(orch.repo, caller, local_runner, notify=lambda _: None)
    if resumes:
        assert resumed.resume_plan(run_id).status == "planned"
    else:
        with pytest.raises(Conflict, match="Unresolved planning call"):
            resumed.resume_plan(run_id)
    calls = [(c.purpose, c.attempt) for c in resumed.state.usage.calls(run_id)
             if c.task_id == "T-001"]
    assert calls == [("test-design", 1), ("test-design", 2)] + (
        [("test-design", 2)] if resumes else [])
    assert caller.keys().count("dsh:T-001") == len(calls)


def test_shard_resume_replays_a_settled_bad_answer_instead_of_buying_it_again(
    resumable_case: tuple[Orchestrator, ScriptedCaller],
) -> None:
    orch, caller = resumable_case
    caller.responses["dsh:T-001"][:0] = [DANGLING, NativeResult("auth_required", {})]
    with pytest.raises(PlanError, match="auth_required"):
        orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    run_id = orch.state.usage.calls()[0].run_id
    resumed = Orchestrator(orch.repo, caller, local_runner, notify=lambda _: None)
    assert resumed.resume_plan(run_id).status == "planned"
    assert caller.keys().count("dsh:T-001") == 3 and caller.keys().count("plan") == 1
    assert [c.attempt for c in resumed.state.usage.calls(run_id) if c.task_id == "T-001"] == [
        1, 2, 2]


@pytest.mark.parametrize("interrupted", ["review", "shard"])
def test_reviewed_boundary_resume_reuses_finished_phases(
    resumable_case: tuple[Orchestrator, ScriptedCaller], interrupted: str,
) -> None:
    orch, caller = resumable_case
    caller.responses["plan"].append(NativeResult("ok", {"approved": True, "issues": []}))
    if interrupted == "review":
        caller.responses["plan"].insert(1, NativeResult("timeout", {}))
    else:
        caller.responses["dsh:T-002"].insert(0, NativeResult("auth_required", {}))
    with pytest.raises(PlanError):
        orch.plan("Implement adder.add and doubler.double", test_jobs=1, reviewed_boundary=True)
    original = orch.state.usage.calls()
    resumed = Orchestrator(orch.repo, caller, local_runner, notify=lambda _: None)
    run = resumed.resume_plan(original[0].run_id, ack_unknown_calls=(
        [original[-1].id] if interrupted == "review" else []))
    calls = resumed.state.usage.calls(run.id)
    assert run.status == "planned" and len(calls) == 5
    assert sum(c.purpose == "boundary-draft" for c in calls) == 1
    assert sum(c.purpose == "boundary-review" for c in calls) == (
        2 if interrupted == "review" else 1)
    assert len(caller.calls) == len(calls)


@pytest.mark.parametrize("verdict", [
    {}, {"approved": True, "issues": ["contradiction"]}, {"approved": False, "issues": []},
    {"approved": 1, "issues": []}, {"approved": False, "issues": [" "]},
])
def test_malformed_boundary_reviews_cannot_authorize_a_plan(verdict: object) -> None:
    with pytest.raises(ValueError, match="Boundary review"):
        review_verdict(verdict)


def test_boundary_review_keeps_project_evidence_in_a_stable_prefix() -> None:
    first = boundary_review_prompt("spec", "repo", {"summary": "first"}, "coupling")
    second = boundary_review_prompt("spec", "repo", {"summary": "revised"}, "coupling",
                                    findings=["R1 prior violation"])
    assert isinstance(first, ContextPrompt) and isinstance(second, ContextPrompt)
    assert first.shared == second.shared and first.dynamic != second.dynamic


@pytest.mark.parametrize("interrupted", [False, True])
def test_boundary_revision_preserves_findings_for_author_and_independent_review(
    resumable_case: tuple[Orchestrator, ScriptedCaller], monkeypatch: pytest.MonkeyPatch,
    interrupted: bool,
) -> None:
    orch, caller = resumable_case
    boundary = caller.responses["plan"][0]
    findings = ["R1 must preserve the original error behaviour.",
                "R2 must preserve the original call ordering."]
    caller.responses["plan"] = [
        boundary, NativeResult("ok", {"approved": False, "issues": [findings[0]]}),
        revised(boundary, 1),
        NativeResult("ok", {"approved": False, "issues": [findings[1]]}),
        revised(boundary, 2), NativeResult("ok", {"approved": True, "issues": []}),
    ]

    def fail_checkpoint(*args: object, **kwargs: object) -> Run:
        raise RuntimeError("injected checkpoint interruption")

    if interrupted:
        monkeypatch.setattr(orch, "_checkpoint", fail_checkpoint)
        with pytest.raises(RuntimeError, match="checkpoint interruption"):
            orch.plan("Implement adder.add and doubler.double", test_jobs=1,
                      reviewed_boundary=True, attempts=3)
        calls = len(caller.calls)
        run_id = orch.state.usage.calls()[0].run_id
        resumed = Orchestrator(orch.repo, caller, local_runner, notify=lambda line: None)
        run = resumed.resume_plan(run_id)
        assert len(caller.calls) == calls
    else:
        run = orch.plan("Implement adder.add and doubler.double", test_jobs=1,
                        reviewed_boundary=True, attempts=3)
    last = {provider: [text for route, key, text in caller.calls
                       if route == provider and key == "plan"][-1]
            for provider in ("dsh", "claude")}
    assert run.status == "planned"
    assert {provider: [finding in text for finding in findings]
            for provider, text in last.items()} == {"dsh": [True, True], "claude": [True, True]}


@pytest.mark.parametrize("role", ["author", "reviewer"])
def test_boundary_roles_share_controller_stage_without_dropping_spec(role: str) -> None:
    spec = "R1: implement add; include test_r1_behavior.py"
    prompt = (boundary_prompt(spec, "repo", []) if role == "author"
              else boundary_review_prompt(spec, "repo", {}))
    assert isinstance(prompt, ContextPrompt)
    assert SPEC_AUTHORITY in prompt.shared and BOUNDARY_RULES in prompt.shared
    evidence = json.loads(prompt.shared[prompt.shared.index('{"'):])
    assert evidence["controller_stage"] == BOUNDARY_STAGE
    assert evidence["spec" if role == "author" else "original_spec"] == spec


@pytest.mark.parametrize("window", ["before-commit", "before-state", "user-edit"])
def test_planning_checkpoint_resume_checks_saved_content_and_preserves_user_edits(
    resumable_case: tuple[Orchestrator, ScriptedCaller], monkeypatch: pytest.MonkeyPatch,
    window: str,
) -> None:
    orch, caller = resumable_case
    git = orch.git

    def interrupted_git(path: Path, *args: str, **kwargs: Any) -> str:
        if window == "before-commit" and path.name == "integration" and args[0] == "commit":
            raise RuntimeError("injected checkpoint interruption")
        return git(path, *args, **kwargs)

    def interrupted_state(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected checkpoint interruption")

    monkeypatch.setattr(orch, "git", interrupted_git)
    monkeypatch.setattr(orch.state, "create_run", interrupted_state)
    with pytest.raises(RuntimeError, match="injected checkpoint"):
        orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    calls = orch.state.usage.calls()
    run_id = calls[0].run_id
    owned = orch.integration(run_id) / "doubler.py"
    if window == "user-edit":
        owned.write_text("# user correction; preserve it\n")
    resumed = Orchestrator(orch.repo, caller, local_runner, notify=lambda _: None)
    if window == "user-edit":
        with pytest.raises(PlanError, match="differs from the saved plan"):
            resumed.resume_plan(run_id)
        assert owned.read_text() == "# user correction; preserve it\n"
    else:
        run = resumed.resume_plan(run_id)
        assert run.status == "planned" and run.id == run_id
        assert Workspace.git(orch.repo, "rev-parse", f"orch/{run_id}/plan") == run.checkpoint
    assert len(caller.calls) == len(calls) == 3


@pytest.mark.parametrize("repair", ["none", "ownership", "settled-json", "capacity"])
def test_distributed_existing_repo_parallel_tests_and_local_retry(
    distributed_case: tuple[Orchestrator, ScriptedCaller], repair: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orchestragent.orch_run.DSF_BUDGET", 39919)
    orch, caller = distributed_case
    if repair == "ownership":  # a test file may not overwrite code a task owns
        caller.responses["dsh:T-001"].insert(0, design(
            "adder.py", "def add(a, b):\n    return a + b\n", "python3 -m unittest"))
    elif repair == "settled-json":
        caller.responses["dsh:T-001"].insert(0, NativeResult(
            "failed", {}, usage={"settled_cents": 1}, message="Invalid DSH JSON result: truncated"))
    elif repair == "capacity":
        oversized = copy.deepcopy(caller.responses["dsh:T-001"][0])
        cast(list[dict[str, str]], oversized.data["files"])[0]["content"] += "# verbose\n" * 5000
        caller.responses["dsh:T-001"].insert(0, oversized)
    run = orch.plan("Implement adder.add and doubler.double", test_jobs=2)
    completed = orch.execute(run.id, jobs=2)
    calls = orch.state.usage.calls(run.id)
    assert completed.status == "done" and run.strategy == "opus-dsf"
    assert len([c for c in calls if c.provider == "claude"]) == 1
    assert [(tid, len([c for c in calls if c.task_id == tid and c.purpose == "test-design"]))
            for tid in ("T-001", "T-002")] == [("T-001", 1 + (repair != "none")), ("T-002", 1)]
    assert all(c.scope == "planning" for c in calls if c.purpose == "test-design")
    assert any(e.kind == "plan.distributed" for e in orch.state.events(run.id))
    saved = [json.loads(path.read_text())
             for path in (orch.run_dir(run.id) / "planning").glob("T-*.json")]
    assert sum(item["accepted"] is True for item in saved) == 2
    if repair == "capacity":
        assert sum(item["accepted"] is False for item in saved) == 1


def test_oversized_owned_source_is_rejected_before_test_generation(
    distributed_case: tuple[Orchestrator, ScriptedCaller],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orchestragent.orch_run.DSF_BUDGET", 39919)
    orch, caller = distributed_case
    orch.call = caller
    with (orch.repo / "adder.py").open("a") as source:
        source.write("# padding\n" * 5000)
    Workspace.git(orch.repo, "add", "adder.py")
    Workspace.git(orch.repo, "commit", "-m", "large source")
    caller.responses["plan"].append(copy.deepcopy(caller.responses["plan"][0]))
    with pytest.raises(PlanError) as error:
        orch.plan("Implement adder.add and doubler.double", attempts=2)
    assert caller.keys() == ["plan", "plan"]
    assert "builder budget" in str(error.value.errors)


def test_shard_and_retry_details_follow_identical_shared_prefix(
    boundary: dict[str, object],
) -> None:
    prompts = [design_prompt("Original spec", boundary, "index map", errors, task_id=tid)
               for tid, errors in [("T-001", []), ("T-002", []), ("T-001", ["repair"])]]
    prefixes = [p.split("\n## ASSIGNED TEST SHARD\n")[0] for p in prompts]
    assert len(set(prefixes)) == 1 and "Original spec" in prefixes[0]
    assert SPEC_AUTHORITY in prefixes[0]
    assert len(set(prompts)) == 3


def test_test_design_reads_pinned_legacy_source_before_builders(
    distributed_case: tuple[Orchestrator, ScriptedCaller],
) -> None:
    orch, caller = distributed_case
    original = (orch.repo / "adder.py").read_text()
    original += "\ndef legacy_offset():\n    return 7\n"
    (orch.repo / "adder.py").write_text(original)
    Workspace.git(orch.repo, "add", "adder.py")
    Workspace.git(orch.repo, "commit", "-m", "legacy helper")
    (orch.repo / "adder.py").write_text(original + "\n# UNCOMMITTED_SOURCE\n")
    orch.plan("Implement adder.add and doubler.double")
    shards = [prompt for provider, _, prompt in caller.calls if provider == "dsh"]
    assert len(shards) == 2
    assert all(original in prompt and "UNCOMMITTED_SOURCE" not in prompt for prompt in shards)
    assert all("PINNED EXISTING SOURCE" in prompt for prompt in shards)
    assert len({p.split("\n## ASSIGNED TEST SHARD\n")[0] for p in shards}) == 1


def test_distributed_rejection_stops_queued_shards_without_replaying_unknown_calls(
    distributed_case: tuple[Orchestrator, ScriptedCaller],
) -> None:
    orch, caller = distributed_case
    orch.call = caller  # one slot: the integration fixture's parallel barrier is not needed
    caller.responses["dsh:T-002"] = [NativeResult("timeout", {})]  # dispatched first
    with pytest.raises(PlanError, match="test-designer T-002 returned timeout"):
        orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    assert caller.keys() == ["plan", "dsh:T-002"]


@pytest.mark.parametrize("mutation", [
    "cycle", "ambiguous", "extra-owner", "tests", "unsafe-path", "empty-stub",
])
def test_invalid_boundary_is_rejected_before_any_dsf_call(
    orchestrator: Orchestrator, boundary: dict[str, object], caller: ScriptedCaller,
    mutation: str,
) -> None:
    tasks = cast(list[dict[str, object]], boundary["tasks"])
    files = cast(list[dict[str, str]], boundary["files"])
    if mutation == "cycle":
        tasks[0]["depends_on"] = ["T-002"]
    elif mutation == "ambiguous":
        tasks[1]["reqs"] = ["R1", "R2"]
    elif mutation == "extra-owner":
        tasks[0]["files"] = ["unplanned.py"]
    elif mutation == "tests":
        tasks[0]["tests"] = [ADD_TEST]
    elif mutation == "unsafe-path":
        files[0]["path"] = "../adder.py"
        tasks[0]["files"] = ["../adder.py"]
    else:
        files[0]["content"] = "  \n"
    caller.responses["plan"] = [NativeResult("ok", boundary)]
    with pytest.raises(PlanError):
        orchestrator.plan("invalid boundaries", attempts=1)
    assert [provider for provider, _, _ in caller.calls] == ["claude"]
    assert not orchestrator.state.runs() and boundary_errors(boundary, frozenset())


@pytest.mark.parametrize("mutation", ["extra-key", "stub-path", "escape", "duplicate", "empty"])
def test_bad_test_design_cannot_modify_frozen_boundary_or_create_a_checkpoint(
    orchestrator: Orchestrator, caller: ScriptedCaller, mutation: str,
) -> None:
    answer = copy.deepcopy(DOUBLE_DESIGN.data)  # T-002 is dispatched first
    files = cast(list[dict[str, str]], answer["files"])
    if mutation == "extra-key":
        answer["tasks"] = []
    elif mutation == "stub-path":
        files[0]["path"] = "doubler.py"
    elif mutation == "escape":
        files[0]["path"] = "../test_double.py"
    elif mutation == "duplicate":
        files.append(dict(files[0]))
    else:
        answer.clear()
    caller.responses["dsh:T-002"] = [NativeResult("ok", answer)]
    with pytest.raises(PlanError):
        orchestrator.plan("test isolation", attempts=1, test_jobs=1)
    assert [provider for provider, _, _ in caller.calls] == ["claude", "dsh"]
    assert not orchestrator.state.runs()


def test_test_only_repair_keeps_the_original_boundary(
    orchestrator: Orchestrator, caller: ScriptedCaller,
) -> None:
    caller.responses["dsh:T-002"] = [NativeResult("ok", {"bad": "design"}), DOUBLE_DESIGN,
                                     GOOD_DOUBLE]
    caller.responses["dsh:T-001"] = [ADD_DESIGN, GOOD_ADD]
    run = orchestrator.execute(orchestrator.plan("bounded test repair", attempts=2,
                                                 test_jobs=1).id, jobs=1)
    assert run.status == "done"
    assert [call.purpose for call in orchestrator.state.usage.calls(run.id)][:3] == [
        "plan", "test-design", "test-design"]
    assert [provider for provider, _, _ in caller.calls][:3] == ["claude", "dsh", "dsh"]
    assert caller.keys().count("plan") == 1


@pytest.mark.parametrize(
    ("stage", "outcome"),
    [
        (0, "auth_required"),
        (0, "cancelled"),
        (1, "policy_blocked"),
        (1, "quota_exhausted"),
        (1, "rate_limited"),
        (1, "cancelled"),
    ],
)
def test_provider_or_budget_stop_does_not_retry_or_fallback(
    orchestrator: Orchestrator, caller: ScriptedCaller, stage: int, outcome: str,
) -> None:
    stop = NativeResult(outcome, {}, usage={"forwarded": 0, "rejected": 1})
    if stage == 0:
        caller.responses["plan"][0] = stop
    else:
        caller.responses["dsh:T-002"][0] = stop  # the first shard dispatched
    with pytest.raises(PlanError):
        orchestrator.plan("stop safely", attempts=3, test_jobs=1)
    assert len(caller.calls) == stage + 1
    assert orchestrator.state.usage.calls()[-1].outcome == outcome
    assert not orchestrator.state.runs()


def test_cancel_between_boundary_and_design_never_dispatches_dsf(
    orchestrator: Orchestrator, caller: ScriptedCaller,
) -> None:
    def cancel(
        provider: str,
        model: str,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        cancelled: object,
        *,
        effort: str = "",
    ) -> NativeResult:
        result = caller(provider, model, prompt, schema, run_id, lambda: False, effort=effort)
        orchestrator.cancel.set()
        return result

    orchestrator.call = cancel
    with pytest.raises(PlanError, match="cancelled"):
        orchestrator.plan("stop before test design")
    assert [provider for provider, _, _ in caller.calls] == ["claude"]


STREAM_CLOSED = NativeResult(
    "failed", {}, usage={"forwarded": 1, "reserved_cents": 300, "turn_end": "error",
                         "turn_error": "STREAM_CLOSED"},
    message="DSH process stopped without a valid result",
)


def test_a_dropped_shard_stream_repeats_only_that_call_and_planning_continues(
    resumable_case: tuple[Orchestrator, ScriptedCaller], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Run r073174b7 lost the whole Boltons run to one DSF STREAM_CLOSED.
    monkeypatch.setattr("orchestragent.orch_run.TRANSIENT_RETRY_DELAYS", (0.0, 0.0))
    orch, caller = resumable_case
    caller.responses["dsh:T-001"][:0] = [STREAM_CLOSED]
    orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    designed = [c for c in orch.state.usage.calls() if c.task_id == "T-001"]
    assert [(c.attempt, c.sample, c.outcome) for c in designed] == [
        (1, "", "failed"), (1, "~retry1", "ok")]
    assert caller.keys().count("dsh:T-001") == 2
    count = len(caller.calls)
    run_id = designed[0].run_id
    resumed = Orchestrator(orch.repo, caller, local_runner, notify=lambda _: None)
    resumed.resume_plan(run_id)  # the finished plan replays; nothing is asked again
    assert len(caller.calls) == count


def test_a_shard_that_keeps_dropping_stops_after_its_repeats(
    resumable_case: tuple[Orchestrator, ScriptedCaller], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orchestragent.orch_run.TRANSIENT_RETRY_DELAYS", (0.0, 0.0))
    orch, caller = resumable_case
    caller.responses["dsh:T-001"][:0] = [STREAM_CLOSED] * 3
    with pytest.raises(PlanError, match="test-designer T-001 returned failed"):
        orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    designed = [c for c in orch.state.usage.calls() if c.task_id == "T-001"]
    assert [c.sample for c in designed] == ["", "~retry1", "~retry2"]


def test_test_design_effort_switch_routes_only_test_design_without_thinking(
    resumable_case: tuple[Orchestrator, ScriptedCaller], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orchestragent.orch_run.DSF_TEST", Route("dsh", "deepseek-flash", "off"))
    orch, _ = resumable_case
    orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    designed = [c for c in orch.state.usage.calls() if c.purpose.endswith("test-design")]
    assert designed and {c.effort for c in designed} == {"off-extended"}


def test_planner_effort_switch_changes_only_the_orchestrated_planner(
    resumable_case: tuple[Orchestrator, ScriptedCaller], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orchestragent.orch_run.PLANNER", replace(CLAUDE_PLAN, effort="medium"))
    orch, _ = resumable_case
    orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    planned = [c for c in orch.state.usage.calls() if c.provider == "claude"]
    assert planned and {c.effort for c in planned} == {"medium"}
    assert CLAUDE_PLAN.effort == "high"  # the single-agent baseline's route is untouched


ADD_VALUE = contract_test("adder", "add(2, 3)", 5, "r1")
ZERO_TEST = "\n    def test_r1_zero(self):\n        self.assertEqual(add(0, 0), 0)\n"


@pytest.fixture
def reviewed_case(
    resumable_case: tuple[Orchestrator, ScriptedCaller], monkeypatch: pytest.MonkeyPatch,
) -> tuple[Orchestrator, ScriptedCaller]:
    monkeypatch.setattr("orchestragent.orch_run.COVERAGE_REVIEW", True)
    orch, caller = resumable_case
    caller.responses["claude:T-002"] = [NativeResult("ok", {"missing": [], "wrong": []})]
    return orch, caller


def test_coverage_review_has_unasserted_behaviour_added_before_freezing(
    reviewed_case: tuple[Orchestrator, ScriptedCaller],
) -> None:
    orch, caller = reviewed_case
    caller.responses["claude:T-001"] = [
        NativeResult("ok", {"missing": [{"req": "R1", "clause": "add(0, 0) returns 0"}],
                            "wrong": [{"test": "test_r1_value", "problem": "fine, kept"}]}),
        # Second look at the revised draft: only `wrong` counts, so no third design.
        NativeResult("ok", {"missing": [{"req": "R1", "clause": "add(1, 1) returns 2"}],
                            "wrong": []}),
    ]
    caller.responses["dsh:T-001"].insert(1, shard("T-001", ADD_VALUE + ZERO_TEST))
    run = orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    calls = orch.state.usage.calls(run.id)
    t1 = [(c.provider, c.purpose) for c in calls if c.task_id == "T-001"]
    assert t1 == [("dsh", "test-design"), ("claude", "coverage-review"), ("dsh", "test-design"),
                  ("claude", "coverage-review")]
    review = next(p for _, key, p in caller.calls if key == "claude:T-001")
    assert "ASSIGNED REQUIREMENTS" in review and "def test_r1_value" in review
    repair = [p for _, key, p in caller.calls if key == "dsh:T-001"][1]
    assert "Test review before freezing" in repair and "missing R1: add(0, 0) returns 0" in repair
    assert "wrong test_r1_value" in repair
    frozen = orch.git(orch.repo, "show", f"{run.checkpoint}:{shard_file('T-001')}")
    assert "def test_r1_zero" in frozen and "def test_r1_value" in frozen
    [check] = orch.state.task(run.id, "T-001").tests
    assert cast(Check, orch_verify.parse(check)).files == (shard_file("T-001"),)


@pytest.mark.parametrize("review", [
    NativeResult("ok", {"missing": []}),
    NativeResult("failed", {}, message="DSH upstream HTTP 400", usage={"rejected": 400}),
], ids=["covered", "review-failed"])
def test_coverage_review_without_gaps_or_failing_never_blocks_planning(
    reviewed_case: tuple[Orchestrator, ScriptedCaller], review: NativeResult,
) -> None:
    orch, caller = reviewed_case
    caller.responses["claude:T-001"] = [review]
    run = orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    purposes = [c.purpose for c in orch.state.usage.calls(run.id) if c.task_id == "T-001"]
    assert purposes == ["test-design", "coverage-review"]


def test_second_review_corrects_a_wrong_added_test_once_more(
    reviewed_case: tuple[Orchestrator, ScriptedCaller],
) -> None:
    orch, caller = reviewed_case
    caller.responses["claude:T-001"] = [
        NativeResult("ok", {"missing": [{"req": "R1", "clause": "zero"}], "wrong": []}),
        NativeResult("ok", {"missing": [], "wrong": [
            {"test": "test_r1_zero", "problem": "use add(0, 1) == 1"}]}),
    ]
    corrected = ZERO_TEST.replace("add(0, 0), 0", "add(0, 1), 1")
    caller.responses["dsh:T-001"][1:1] = [shard("T-001", ADD_VALUE + ZERO_TEST),
                                          shard("T-001", ADD_VALUE + corrected)]
    run = orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    t1 = [c.purpose for c in orch.state.usage.calls(run.id) if c.task_id == "T-001"]
    assert t1 == ["test-design", "coverage-review", "test-design", "coverage-review",
                  "test-design"]
    frozen = orch.git(orch.repo, "show", f"{run.checkpoint}:{shard_file('T-001')}")
    assert "add(0, 1), 1" in frozen and "add(0, 0), 0" not in frozen


def test_a_failed_review_repair_freezes_the_reviewed_draft_without_redesign(
    reviewed_case: tuple[Orchestrator, ScriptedCaller],
) -> None:
    orch, caller = reviewed_case
    caller.responses["claude:T-001"] = [
        NativeResult("ok", {"missing": [{"req": "R1", "clause": "zero"}], "wrong": []})]
    caller.responses["dsh:T-001"].insert(1, NativeResult("ok", {"files": [], "command": "x"}))
    run = orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    t1 = [c.purpose for c in orch.state.usage.calls(run.id) if c.task_id == "T-001"]
    assert t1 == ["test-design", "coverage-review", "test-design"]  # no further design
    frozen = orch.git(orch.repo, "show", f"{run.checkpoint}:{shard_file('T-001')}")
    assert frozen.strip() == ADD_VALUE.strip()


def test_shard_prompts_and_review_demand_decisive_assertions(
    boundary: dict[str, object],
) -> None:
    for integration in (False, True):
        prompt = design_prompt("spec", boundary, "", [], task_id="T-001",
                               integration=integration)
        assert "Never write try/except or if/else that lets either of two behaviours pass" in prompt
    review = coverage_prompt({"R1": "add raises ValueError"}, "t.py", "pass")
    assert "does not assert it: list it in `missing`" in review


@pytest.mark.parametrize(("model", "provider"), [("sonnet", "claude"), ("dsf", "dsh")])
def test_review_model_switch_routes_the_shard_review(
    resumable_case: tuple[Orchestrator, ScriptedCaller], monkeypatch: pytest.MonkeyPatch,
    model: str, provider: str,
) -> None:
    monkeypatch.setattr("orchestragent.orch_run.COVERAGE_REVIEW", True)
    monkeypatch.setattr("orchestragent.orch_run.REVIEW_MODEL", model)
    orch, caller = resumable_case
    clean = NativeResult("ok", {"missing": [], "wrong": []})
    for tid in ("T-001", "T-002"):
        key = f"{provider}:{tid}"
        caller.responses.setdefault(key, [])
        caller.responses[key].insert(1 if provider == "dsh" else 0, clean)
    run = orch.plan("Implement adder.add and doubler.double", test_jobs=1)
    reviews = [c for c in orch.state.usage.calls(run.id) if c.purpose == "coverage-review"]
    assert len(reviews) == 2 and {c.provider for c in reviews} == {provider}


def test_tasks_citing_an_upstream_requirement_get_integration_shards() -> None:
    boundary: dict[str, object] = {
        "requirements": [
            {"id": "R1", "text": "solve() returns a schedule."},
            {"id": "R2", "text": "audit() computes baseline R1 cost per scenario."},
            {"id": "R3", "text": "report() formats R2 results; see also R9."},
            {"id": "R4", "text": "cli() mentions R1 but does not depend on it."},
        ],
        "tasks": [
            {"id": "T-001", "reqs": ["R1"], "depends_on": []},
            {"id": "T-002", "reqs": ["R2"], "depends_on": ["T-001"]},
            {"id": "T-003", "reqs": ["R3"], "depends_on": ["T-002"]},
            {"id": "T-004", "reqs": ["R4"], "depends_on": []},  # cites R1, no upstream link
        ],
    }
    assert citing_tasks(boundary) == {"T-002", "T-003"}
    assert citing_tasks({"tasks": [], "requirements": []}) == set()
