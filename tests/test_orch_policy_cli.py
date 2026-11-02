import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import orchestragent.orch_cli as orch_cli
from orchestragent.envelope import digest
from orchestragent.live_workspace import LiveWorkspace
from orchestragent.native import NativeResult
from orchestragent.orch_bench import Case, mock_caller, read_report, run_suite
from orchestragent.orch_policy import Policy, PolicyStore
from orchestragent.orch_run import CLAUDE_PLAN, DSF, SONNET_PLAN, Caller, Orchestrator, Route
from orchestragent.orch_state import OrchState
from orchestragent.serialization import dumps
from orchestragent.workspace import Workspace
from tests.conftest import ScriptedCaller, local_runner

BASELINE_RULE = "FROZEN_BASELINE_RULE_SENTINEL: validate the complete planner result."
CANDIDATE_RULE = "UNFROZEN_CANDIDATE_RULE_SENTINEL: revise the planner result."
SYNTHETIC_SUITE = (
    Path(__file__).resolve().parents[1] / "examples" / "orch-bench" / "suite.json"
)
ADD_SPEC = (
    "In adder.py implement add(a: int, b: int) -> int as the exact integer sum. "
    "Add test_adder.py unittest coverage for add(2, 3) == 5 and add(-4, 1) == -3."
)
ACCEPTANCE = (
    "import unittest\n\nfrom adder import add\n\n\n"
    "class HeldOutAdditionTests(unittest.TestCase):\n"
    "    def test_unseen_mixed_sign_inputs(self) -> None:\n"
    "        self.assertEqual(add(7, -11), -4)\n"
    "        self.assertEqual(add(-8, 12), 4)\n"
)


@pytest.fixture
def cli_policy_setup(orchestrator: Orchestrator) -> tuple[PolicyStore, str, str]:
    registry = orchestrator.policies
    active = registry.state.add_rule(BASELINE_RULE, "offline-test", kind="planner")
    assert active is not None
    registry.state.save_rule(replace(active, status="active"))
    candidate = registry.state.add_rule(CANDIDATE_RULE, "offline-test", kind="planner")
    assert candidate is not None
    return registry, active.id, candidate.id


@pytest.fixture
def curated_suite(tmp_path: Path) -> Path:
    path = tmp_path / "single-add.json"
    path.write_text(dumps({
        "schema_version": 1,
        "cases": [{
            "id": "single-add",
            "spec": ADD_SPEC,
            "seed": {"adder.py": (
                "def add(a: int, b: int) -> int:\n"
                "    raise NotImplementedError\n"
            )},
            "acceptance": {"test_heldout.py": ACCEPTANCE},
            "mock_shape": {"tasks": 1, "packages": 1, "fan_in": 0, "seed": 7},
        }],
    }))
    return path


@pytest.fixture
def correct_add_bundle() -> list[dict[str, str]]:
    return [
        {"path": "adder.py", "content": (
            "def add(a: int, b: int) -> int:\n    return a + b\n"
        )},
        {"path": "test_adder.py", "content": (
            "import unittest\n\nfrom adder import add\n\n\n"
            "class AdditionTests(unittest.TestCase):\n"
            "    def test_spec_examples(self) -> None:\n"
            "        self.assertEqual(add(2, 3), 5)\n"
            "        self.assertEqual(add(-4, 1), -3)\n"
        )},
    ]


@pytest.fixture
def overfit_add_bundle() -> list[dict[str, str]]:
    return [
        {"path": "adder.py", "content": (
            "def add(a: int, b: int) -> int:\n"
            "    return 5 if (a, b) == (2, 3) else 0\n"
        )},
        {"path": "test_adder.py", "content": (
            "import unittest\n\nfrom adder import add\n\n\n"
            "class AdditionTests(unittest.TestCase):\n"
            "    def test_spec_example(self) -> None:\n"
            "        self.assertEqual(add(2, 3), 5)\n"
        )},
    ]


@pytest.fixture
def failing_add_bundle() -> list[dict[str, str]]:
    return [
        {"path": "adder.py", "content": (
            "def add(a: int, b: int) -> int:\n    return a - b\n"
        )},
        {"path": "test_adder.py", "content": (
            "import unittest\n\nfrom adder import add\n\n\n"
            "class AdditionTests(unittest.TestCase):\n"
            "    def test_spec_example(self) -> None:\n"
            "        self.assertEqual(add(2, 3), 5)\n"
        )},
    ]


def scripted_bundle(files: list[dict[str, str]]) -> ScriptedCaller:
    return ScriptedCaller({"plan": [NativeResult("ok", {"files": files}, usage={
        "input_tokens": 40, "output_tokens": 20, "total_tokens": 60,
    })]})


def test_policy_cli_stages_without_activation_and_refuses_unaccepted_rollback(
    orch_repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    cli_policy_setup: tuple[PolicyStore, str, str],
) -> None:
    registry, baseline_rule_id, candidate_id = cli_policy_setup
    monkeypatch.setattr(orch_cli, "providers", lambda *_: pytest.fail("provider construction"))

    assert orch_cli.main(["--repo", str(orch_repo), "policy", "enable"]) == 0
    baseline = json.loads(capsys.readouterr().out)
    baseline_id = baseline["id"]
    assert [(row["id"], row["text"]) for row in baseline["rules"]] == [
        (baseline_rule_id, BASELINE_RULE),
    ]

    assert orch_cli.main([
        "--repo", str(orch_repo), "policy", "snapshot", "--include", candidate_id,
        "--exclude", baseline["rules"][0]["id"],
    ]) == 0
    staged = json.loads(capsys.readouterr().out)
    assert staged["parent"] == baseline_id
    assert [row["id"] for row in staged["rules"]] == [candidate_id]
    assert staged["rules"][0]["status"] == "active"

    assert orch_cli.main(["--repo", str(orch_repo), "policy", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["active"]["id"] == baseline_id
    assert [row["action"] for row in status["history"]] == ["enabled"]

    assert orch_cli.main(["--repo", str(orch_repo), "policy", "rollback", staged["id"]]) == 1
    assert "never an accepted active policy" in capsys.readouterr().err
    current = registry.current()
    assert current is not None and current.id == baseline_id
    assert orch_cli.main(["--repo", str(orch_repo), "policy", "rollback", baseline_id]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["id"] == baseline_id
    assert [row["action"] for row in registry.history()] == ["enabled", "rolled_back"]
    assert orch_cli.main(["--repo", str(orch_repo), "policy", "show", staged["id"]]) == 0
    assert json.loads(capsys.readouterr().out) == staged


def test_real_evaluation_records_spec_exposure_even_when_provider_stops(
    orch_repo: Path, curated_suite: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    cli_policy_setup: tuple[PolicyStore, str, str],
) -> None:
    registry, _, _ = cli_policy_setup
    frozen = registry.enable()
    caller = ScriptedCaller({"plan": [NativeResult("auth_required", {})]})
    client = SimpleNamespace(call=caller, close=lambda: None, access=SimpleNamespace(
        status=lambda _: {"cap_cents": 1000, "spent_cents": 0, "reserved_cents": 0}))
    monkeypatch.setattr(orch_cli, "providers", lambda _: client)
    assert orch_cli.main(["--repo", str(orch_repo), "bench", str(curated_suite),
                          "--output", str(tmp_path / "stopped-live-fixture"),
                          "--live", "--policy", frozen.id]) == 4
    assert digest(ADD_SPEC) not in registry.get(frozen.id).training_specs
    assert digest(ADD_SPEC) in registry.snapshot().training_specs
    assert [provider for provider, _, _ in caller.calls] == ["claude"]


def test_bench_report_cli_reopens_saved_report_without_provider_construction(
    orch_repo: Path,
    curated_suite: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "saved-report"
    run_suite(curated_suite, output, mock_caller, local_runner, mode="mock", max_calls=10)
    monkeypatch.setattr(orch_cli, "providers", lambda *_: pytest.fail("provider construction"))

    assert orch_cli.main([
        "--repo", str(orch_repo), "bench", "--output", str(output), "--report", "--json",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == read_report(output)


@pytest.mark.parametrize("strategy", ["single-opus", "single-sonnet", "single-dsf"])
def test_single_model_cli_requires_live_flag_before_provider_construction(
    orch_repo: Path,
    curated_suite: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    strategy: str,
) -> None:
    monkeypatch.setattr(orch_cli, "providers", lambda *_: pytest.fail("provider construction"))
    result = orch_cli.main([
        "--repo", str(orch_repo), "bench", str(curated_suite), "--output",
        str(tmp_path / "cli-output"), "--strategy", strategy,
    ])
    assert result == 1
    assert f"{strategy} requires --live" in capsys.readouterr().err


def test_frozen_policy_provenance_is_reported_and_candidate_never_enters_prompts(
    orchestrator: Orchestrator,
    curated_suite: Path,
    tmp_path: Path,
) -> None:
    registry = orchestrator.policies
    frozen_rule = registry.state.add_rule(BASELINE_RULE, "offline-test", kind="planner")
    assert frozen_rule is not None
    registry.state.save_rule(replace(frozen_rule, status="active"))
    frozen: Policy = registry.enable()
    candidate = registry.state.add_rule(CANDIDATE_RULE, "offline-test", kind="planner")
    assert candidate is not None
    prompts: list[str] = []

    def factory(case: Case) -> Caller:
        inner = mock_caller(case)

        def logged(provider: str, model: str, prompt: str, schema: dict[str, object],
                   run_id: str, cancelled: Callable[[], bool], *, effort: str = ""
                   ) -> NativeResult:
            prompts.append(prompt)
            return inner(provider, model, prompt, schema, run_id, cancelled, effort=effort)

        return logged

    output = tmp_path / "frozen-policy-report"
    result = run_suite(SYNTHETIC_SUITE, output, factory, local_runner, mode="mock",
                       max_calls=20, policy=frozen)
    report = read_report(output)
    trial = cast(list[dict[str, object]], report["trials"])[0]
    assert result["policy_id"] == report["policy_id"] == frozen.id
    snapshot = cast(dict[str, object], report["policy_snapshot"])
    snapshot_rules = cast(list[dict[str, object]], snapshot["rules"])
    assert snapshot["id"] == frozen.id
    assert snapshot_rules[0]["text"] == BASELINE_RULE
    assert report["case_kind"] == "synthetic" and report["synthetic"] is True
    assert trial["status"] == "passed" and prompts
    prompt_text = "\n".join(prompts)
    assert BASELINE_RULE in prompt_text
    assert CANDIDATE_RULE not in prompt_text

    trial_state = OrchState(Path(str(trial["repo"])) / ".git" / "orch" / "state.sqlite")
    run_id = str(trial["run_id"])
    assert trial_state.run(run_id).policy_id == frozen.id
    assert not any(event.kind.startswith("rule.trial")
                   for event in trial_state.events(run_id))


def test_single_opus_heldout_acceptance_rejects_overfit_generated_tests(
    curated_suite: Path,
    overfit_add_bundle: list[dict[str, str]],
    tmp_path: Path,
) -> None:
    caller = scripted_bundle(overfit_add_bundle)
    report = run_suite(curated_suite, tmp_path / "overfit-single", lambda _: caller,
                       local_runner, mode="mock", max_calls=10,
                       strategy="single-opus", baseline_attempts=1)
    trial = cast(list[dict[str, object]], report["trials"])[0]
    assert caller.keys() == ["plan"]
    assert caller.calls[0][0] == "claude"
    assert trial["status"] == "failed" and trial["acceptance_passed"] is False
    assert "FAIL" in str(trial["acceptance_report"])
    assert report["success_rate"] == 0.0


@pytest.mark.parametrize(("strategy", "route"), [
    ("single-opus", CLAUDE_PLAN), ("single-sonnet", SONNET_PLAN), ("single-dsf", DSF),
])
def test_successful_single_trial_has_only_selected_route_and_leaves_checkout_unchanged(
    orch_repo: Path,
    curated_suite: Path,
    correct_add_bundle: list[dict[str, str]],
    tmp_path: Path,
    strategy: str,
    route: Route,
) -> None:
    original_head = Workspace.git(orch_repo, "rev-parse", "HEAD")
    original_branch = Workspace.git(orch_repo, "branch", "--show-current")
    caller = scripted_bundle(correct_add_bundle)
    report = run_suite(curated_suite, tmp_path / "successful-single", lambda _: caller,
                       local_runner, mode="mock", max_calls=10,
                       strategy=strategy, baseline_attempts=2)
    trial = cast(list[dict[str, object]], report["trials"])[0]
    usage = cast(dict[str, object], trial["usage"])
    receipts = cast(list[dict[str, object]], usage["calls"])
    dispatches = cast(dict[str, int], trial["dispatches"])
    config = cast(dict[str, object], report["config"])

    assert config["strategy"] == strategy
    assert config["baseline_attempts"] == 2
    assert trial["status"] == "passed" and trial["acceptance_passed"] is True
    assert caller.calls[0][0] == route.provider and len(caller.calls) == 1
    assert {receipt["provider"] for receipt in receipts} == {route.provider}
    assert {receipt["model"] for receipt in receipts} == {route.model}
    assert {receipt["purpose"] for receipt in receipts} == {"single"}
    assert dispatches["single_model"] == 1
    assert dispatches["builder"] == dispatches["planner"] == dispatches["opus_interventions"] == 0
    assert Workspace.git(orch_repo, "rev-parse", "HEAD") == original_head
    assert Workspace.git(orch_repo, "branch", "--show-current") == original_branch


@pytest.mark.parametrize(("options", "route"), [([], DSF), (["--model", "sonnet"], SONNET_PLAN)])
def test_solve_defaults_to_dsf_and_keeps_candidate_isolated(
    orch_repo: Path, tmp_path: Path, correct_add_bundle: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    options: list[str], route: Route,
) -> None:
    spec = tmp_path / "task.txt"
    spec.write_text(ADD_SPEC)
    caller = ScriptedCaller({"plan": [NativeResult(
        "ok", {"files": correct_add_bundle, "need_files": []})]})
    closed: list[bool] = []
    clients = SimpleNamespace(call=caller, close=lambda: closed.append(True))
    monkeypatch.setattr(orch_cli, "providers", lambda _: clients)
    monkeypatch.setattr(LiveWorkspace, "run_unittests", staticmethod(local_runner))
    head = Workspace.git(orch_repo, "rev-parse", "HEAD")
    assert orch_cli.main(["--repo", str(orch_repo), "solve", str(spec), "--live", "--json",
                          *options]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "done" and result["model"] == route.model
    assert [p for p, _, _ in caller.calls] == [route.provider]
    assert [call["model"] for call in result["usage"]["calls"]] == [route.model]
    assert result["revision"] != head and closed == [True]
    assert Workspace.git(orch_repo, "rev-parse", "HEAD") == head
    assert not (orch_repo / "adder.py").exists()
    assert orch_cli.main(["--repo", str(orch_repo), "cost", "--run", result["run_id"],
                          "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["calls"] == 1


@pytest.mark.parametrize("options", [[], ["--live", "--max-calls", "0"],
                                   ["--live", "--context-bytes", "999999"]])
def test_solve_checks_limits_before_provider_construction(
    orch_repo: Path, monkeypatch: pytest.MonkeyPatch, options: list[str],
) -> None:
    monkeypatch.setattr(orch_cli, "providers", lambda *_: pytest.fail("provider construction"))
    assert orch_cli.main(["--repo", str(orch_repo), "solve", "missing.txt", *options]) == 1


def test_single_opus_repair_counts_against_shared_cap_and_pauses_incomplete_report(
    curated_suite: Path,
    failing_add_bundle: list[dict[str, str]],
    tmp_path: Path,
) -> None:
    caller = scripted_bundle(failing_add_bundle)
    report = run_suite(curated_suite, tmp_path / "capped-single", lambda _: caller,
                       local_runner, mode="mock", max_calls=1,
                       strategy="single-opus", baseline_attempts=2)
    trial = cast(list[dict[str, object]], report["trials"])[0]
    usage = cast(dict[str, object], trial["usage"])
    receipts = cast(list[dict[str, object]], usage["calls"])

    assert len(caller.calls) == 1  # the second repair attempt is locally denied
    assert trial["status"] == "paused" and trial["acceptance_passed"] is None
    assert report["expected_trials"] == report["completed_trials"] == 1
    assert report["passed_trials"] == 0 and report["success_rate"] is None
    assert [receipt["purpose"] for receipt in receipts] == ["single", "single"]
    assert [receipt["attempt"] for receipt in receipts] == [1, 2]
    assert [receipt["outcome"] for receipt in receipts] == ["ok", "policy_blocked"]
    assert cast(dict[str, int], trial["dispatches"])["single_model"] == 2
