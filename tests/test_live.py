from collections.abc import Callable
from pathlib import Path

import pytest

from orchestragent.adapters import StepResult
from orchestragent.controller import Controller
from orchestragent.live import LiveWorkflow
from orchestragent.models import Artifact, Task
from orchestragent.native import NativeResult
from orchestragent.serialization import dumps
from orchestragent.store import Conflict, Store
from orchestragent.workspace import Workspace
from tests.conftest import Clock


@pytest.fixture
def live_controller(tmp_path: Path) -> Controller:
    return Controller(
        Store(tmp_path / "tasks.sqlite"),
        Workspace(tmp_path / "mock"),
        live=LiveWorkflow(tmp_path / "live"),
        lease_seconds=240,
    )


def test_live_lease_outlasts_configured_max_effort(
    live_controller: Controller, clock: Clock,
) -> None:
    assert live_controller.live is not None
    live_controller.live.clients.timeout = 8000
    live_controller.clock = clock
    live_controller.submit("long-budget", "long call", mode="live")
    task = live_controller.claim()
    assert task is not None
    assert task.lease_until > clock.now + 32000


def test_live_call_renews_lease_before_another_serial_call(
    live_controller: Controller, clock: Clock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert live_controller.live is not None
    live_controller.clock = clock
    observed: list[bool | int] = []

    def run(task: Task, cancelled: Callable[[], bool]) -> tuple[StepResult, str, list[Artifact]]:
        clock.now = task.lease_until - 1
        observed.append(cancelled())
        clock.now += 2
        observed.append(live_controller.expire_leases())
        return StepResult("timed_out", "bounded native timeout"), "", []

    monkeypatch.setattr(live_controller.live, "run", run)
    task, _ = live_controller.submit("renew", "multiple bounded calls", mode="live")
    live_controller.tick()
    assert observed == [False, 0]
    assert live_controller.store.read(task.id).status == "timed_out"


@pytest.fixture
def native_calls(monkeypatch: pytest.MonkeyPatch, live_controller: Controller) -> list[str]:
    calls: list[str] = []

    def call(
        provider: str,
        model: str,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        cancelled: Callable[[], bool],
        *,
        effort: str = "",
    ) -> NativeResult:
        calls.append(provider)
        if "-plan-" in run_id:
            data: dict[str, object] = {
                "summary": "plan",
                "implementation": "add",
                "acceptance": "test",
                "implementation_provider": "codex",
                "review_provider": "claude",
            }
        elif "-implement-" in run_id:
            data = {
                "summary": "implemented",
                "files": [
                    {"path": "calc.py", "content": "def add(a,b):\n    return a+b\n"},
                    {
                        "path": "test_calc.py",
                        "content": "import unittest\nfrom calc import add\n"
                        "class Test(unittest.TestCase):\n"
                        "    def test_add(self):\n        self.assertEqual(add(2,3),5)\n",
                    },
                ],
            }
        else:
            data = {"summary": "reviewed", "approved": True, "findings": []}
        return NativeResult("ok", data, f"{provider}-session", {"input_tokens": 42})

    assert live_controller.live is not None
    monkeypatch.setattr(live_controller.live.clients, "call", call)
    return calls


def test_real_workflow_with_fake_native_transport(
    live_controller: Controller, native_calls: list[str]
) -> None:
    task, _ = live_controller.submit("live", "add numbers", mode="live")
    for _ in range(5):
        live_controller.tick()
    done = live_controller.store.read(task.id)
    assert (
        done.status,
        done.mode,
        done.cost_usd,
        native_calls,
        [s.provider for s in done.steps],
        bool(done.revision),
    ) == (
        "completed",
        "live",
        None,
        ["codex", "codex", "claude"],
        ["codex", "codex", "claude", "local", "local"],
        True,
    )


def test_live_restart_requires_explicit_resume(live_controller: Controller) -> None:
    task, _ = live_controller.submit("restart", "work", mode="live")
    live_controller.claim()
    live_controller.recover()
    current = live_controller.store.read(task.id)
    assert (current.status, live_controller.claim()) == ("paused", None)


@pytest.mark.parametrize(
    "outcome",
    ["rate_limited", "quota_exhausted", "auth_required", "timed_out", "failed", "waiting_approval"],
)
def test_live_errors_do_not_retry(
    live_controller: Controller, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    def run(task: Task, cancelled: Callable[[], bool]) -> tuple[StepResult, str, list[Artifact]]:
        return StepResult(outcome, "stopped", cost_usd=None, provider="codex"), "", []

    assert live_controller.live is not None
    monkeypatch.setattr(live_controller.live, "run", run)
    task, _ = live_controller.submit("error", "work", mode="live")
    live_controller.tick()
    current = live_controller.store.read(task.id)
    assert (current.status, live_controller.tick(), current.approval is not None) == (
        outcome,
        False,
        outcome == "waiting_approval",
    )


def test_live_cancel_fences_result(
    live_controller: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(task: Task, cancelled: Callable[[], bool]) -> tuple[StepResult, str, list[Artifact]]:
        live_controller.action(task.id, "cancel")
        assert cancelled()
        return StepResult(summary="late", cost_usd=None), "", [Artifact("late", "no")]

    assert live_controller.live is not None
    monkeypatch.setattr(live_controller.live, "run", run)
    task, _ = live_controller.submit("cancel", "work", mode="live")
    live_controller.tick()
    done = live_controller.store.read(task.id)
    assert (done.status, done.artifacts) == ("cancelled", [])


def test_mode_is_part_of_idempotency(live_controller: Controller) -> None:
    live_controller.submit("same", "work", mode="mock")
    with pytest.raises(Conflict):
        live_controller.submit("same", "work", mode="live")


def test_disabled_live_mode(controller: Controller) -> None:
    with pytest.raises(ValueError, match="mode unavailable"):
        controller.submit("x", "work", mode="live")


def test_mock_server_does_not_claim_persisted_live_tasks(live_controller: Controller) -> None:
    task, _ = live_controller.submit("disabled", "work", mode="live")
    live_controller.live = None
    mock, _ = live_controller.submit("available", "work", mode="mock")
    live_controller.tick()
    assert (
        live_controller.store.read(task.id).status,
        live_controller.store.read(mock.id).stage,
    ) == ("queued", "implement")


def test_live_timeout_preserves_provider_and_usage(live_controller: Controller) -> None:
    task, _ = live_controller.submit("deadline", "work", mode="live")
    claimed = live_controller.claim()
    assert claimed is not None
    with live_controller.store.transaction() as db:
        expired = live_controller.store.get(db, task.id)
        expired.lease_until = 0
        live_controller.store.save(db, expired)
    live_controller.finish(
        claimed,
        StepResult(cost_usd=None, provider="codex", model="chosen", usage={"input_tokens": 42}),
        "",
        [],
    )
    done = live_controller.store.read(task.id)
    assert (done.status, done.provider, done.steps[0].model, done.usage) == (
        "timed_out",
        "codex",
        "chosen",
        {"plan:1": {"input_tokens": 42}},
    )


def _task_with_plan(provider_plan: str, policy: dict[str, object]) -> Task:
    plan = {
        "summary": "s",
        "implementation": "i",
        "acceptance": "a",
        "implementation_provider": provider_plan,
        "review_provider": "claude",
    }
    return Task(
        id="t1",
        request_id="r1",
        prompt="p",
        scenario="happy",
        created_at=0.0,
        updated_at=0.0,
        policy=policy,
        artifacts=[Artifact("plan.json", dumps(plan))],
    )


@pytest.mark.parametrize(
    ("policy", "stage", "expected"),
    [
        ({"implementation_provider": "dsh", "review_provider": "claude"}, "implement", "dsh"),
        ({"implementation_provider": "dsh", "review_provider": "claude"}, "review", "claude"),
        ({}, "implement", "codex"),
        ({"implementation_provider": "", "review_provider": ""}, "implement", "codex"),
    ],
)
def test_pinned_providers_override_the_plan(
    tmp_path: Path, policy: dict[str, object], stage: str, expected: str
) -> None:
    workflow = LiveWorkflow(tmp_path)
    task = _task_with_plan("codex", policy)
    task.stage = stage
    provider, model, _instructions = workflow.route(task)
    assert provider == expected
    assert model


def _unit_task(policy: dict[str, object] | None = None) -> Task:
    plan = {
        "summary": "拆分",
        "implementation": "两个模块",
        "acceptance": "测试通过",
        "implementation_provider": "codex",
        "review_provider": "claude",
        "units": [
            {"id": "core", "goal": "核心", "files": ["calc.py"], "provider": "codex"},
            {"id": "tests", "goal": "测试", "files": ["test_calc.py"], "provider": "claude"},
        ],
    }
    return Task(
        id="0123456789abcdef0123456789abcdef",
        request_id="r-units",
        prompt="并行实现",
        scenario="happy",
        created_at=0.0,
        updated_at=0.0,
        revision="",
        policy=policy or {},
        artifacts=[Artifact("plan.json", dumps(plan))],
    )


def test_parallel_units_run_together_and_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    workflow = LiveWorkflow(tmp_path / "live")
    barrier = threading.Barrier(2, timeout=5)
    seen: list[str] = []

    def call(
        provider: str,
        model: str,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        cancelled: Callable[[], bool],
        *,
        effort: str = "",
    ) -> NativeResult:
        barrier.wait()  # both units must be in flight at the same time
        seen.append(run_id)
        if "core" in run_id:
            files = [{"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}]
        else:
            files = [
                {
                    "path": "test_calc.py",
                    "content": "import unittest\nfrom calc import add\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n",
                }
            ]
        return NativeResult("ok", {"summary": "ok", "files": files}, "", {"input_tokens": 10})

    monkeypatch.setattr(workflow.clients, "call", call)
    result, revision, artifacts = workflow.implement_units(_unit_task(), lambda: False)
    assert result.outcome == "ok"
    assert revision
    assert {artifact.name for artifact in artifacts} >= {"calc.py", "test_calc.py"}
    assert len(seen) == 2


def test_unit_writing_outside_its_scope_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = LiveWorkflow(tmp_path / "live")

    def call(
        provider: str,
        model: str,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        cancelled: Callable[[], bool],
        *,
        effort: str = "",
    ) -> NativeResult:
        return NativeResult("ok", {"summary": "x",
                                   "files": [{"path": "other.py", "content": "x = 1\n"}]}, "", {})

    monkeypatch.setattr(workflow.clients, "call", call)
    with pytest.raises(ValueError):
        workflow.implement_units(_unit_task(), lambda: False)


def test_failed_unit_is_retried_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = LiveWorkflow(tmp_path / "live")
    calls: list[str] = []

    def call(
        provider: str,
        model: str,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        cancelled: Callable[[], bool],
        *,
        effort: str = "",
    ) -> NativeResult:
        calls.append(run_id)
        if "core" in run_id and sum(1 for item in calls if "core" in item) == 1:
            return NativeResult("failed", {}, message="rate limited")
        if "core" in run_id:
            files = [{"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}]
        else:
            files = [{"path": "test_calc.py",
                      "content": "import unittest\nfrom calc import add\n"
                      "class T(unittest.TestCase):\n"
                      "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n"}]
        return NativeResult("ok", {"summary": "ok", "files": files}, "", {})

    monkeypatch.setattr(workflow.clients, "call", call)
    result, revision, _artifacts = workflow.implement_units(_unit_task(), lambda: False)
    assert result.outcome == "ok" and revision
    core = [item for item in calls if "core" in item]
    tests = [item for item in calls if "tests" in item]
    assert (len(core), len(tests)) == (2, 1)


def test_completed_units_are_reused_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestragent.models import Artifact

    workflow = LiveWorkflow(tmp_path / "live")
    calls: list[str] = []

    def call(
        provider: str,
        model: str,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        cancelled: Callable[[], bool],
        *,
        effort: str = "",
    ) -> NativeResult:
        calls.append(run_id)
        content = (
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n"
        )
        return NativeResult(
            "ok",
            {"summary": "ok", "files": [{"path": "test_calc.py", "content": content}]},
            "",
            {},
        )

    monkeypatch.setattr(workflow.clients, "call", call)
    task = _unit_task()
    task.artifacts.append(
        Artifact(
            "units-state.json",
            dumps({"core": [{"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}]}),
        )
    )
    result, _revision, _artifacts = workflow.implement_units(task, lambda: False)
    assert result.outcome == "ok"
    assert all("core" not in item for item in calls)


def test_rework_rebuilds_only_the_named_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestragent.models import Artifact

    workflow = LiveWorkflow(tmp_path / "live")
    calls: list[str] = []

    def call(
        provider: str,
        model: str,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        cancelled: Callable[[], bool],
        *,
        effort: str = "",
    ) -> NativeResult:
        calls.append(run_id)
        content = "x = 2\n" if "core" in run_id else "placeholder = 1\n"
        path = "calc.py" if "core" in run_id else "test_calc.py"
        return NativeResult(
            "ok", {"summary": "ok", "files": [{"path": path, "content": content}]}, "", {}
        )

    monkeypatch.setattr(workflow.clients, "call", call)
    task = _unit_task()
    task.reworks = 1
    task.artifacts.extend(
        [
            Artifact("units-state.json",
                     dumps({"core": [{"path": "calc.py", "content": "x = 1\n"}],
                            "tests": [{"path": "test_calc.py", "content": "placeholder = 1\n"}]})),
            Artifact("review-findings.json", dumps({"findings": ["calc.py 返回值错误"]})),
        ]
    )
    workflow.implement_units(task, lambda: False)
    assert len(calls) == 1
    assert "core" in calls[0]


def test_plan_units_are_repaired_not_rejected() -> None:
    from orchestragent.live import normalize_units

    units = normalize_units(
        [
            {"id": "a", "goal": "x", "files": ["a.py", "b.py"], "provider": "codex"},
            {"id": "b", "goal": "y", "files": ["b.py", "c.py"], "provider": "kimi"},
            {"id": "c", "goal": "z", "files": [], "provider": "claude"},
            {"id": "d", "goal": "w", "files": ["d.py"], "provider": "weird"},
        ],
        "codex",
    )
    assert units is not None
    assert [unit["files"] for unit in units] == [["a.py", "b.py"], ["c.py"], [], ["d.py"]]
    assert [unit["provider"] for unit in units] == ["codex", "codex", "claude", "codex"]


def test_more_than_four_units_is_capped_not_failed() -> None:
    from orchestragent.live import normalize_units

    units = normalize_units(
        [{"id": str(i), "goal": "g", "files": [f"f{i}.py"], "provider": "codex"} for i in range(9)],
        "codex",
    )
    assert units is not None and len(units) == 6


def test_unavailable_vendor_is_swapped_by_the_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = LiveWorkflow(tmp_path / "live")
    seen: list[str] = []

    def call(
        provider: str,
        model: str,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        cancelled: Callable[[], bool],
        *,
        effort: str = "",
    ) -> NativeResult:
        seen.append(provider)
        if provider == "codex":
            return NativeResult("auth_required", {}, message="login expired")
        content = "def add(a, b):\n    return a + b\n" if "core" in run_id else (
            "import unittest\nfrom calc import add\n"
            "class T(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n"
        )
        path = "calc.py" if "core" in run_id else "test_calc.py"
        return NativeResult(
            "ok", {"summary": "ok", "files": [{"path": path, "content": content}]}, "", {}
        )

    monkeypatch.setattr(workflow.clients, "call", call)
    result, revision, _artifacts = workflow.implement_units(_unit_task(), lambda: False)
    assert result.outcome == "ok" and revision
    assert "codex" in seen and "claude" in seen


@pytest.mark.parametrize("repeated", [False, True])
def test_unit_can_request_more_files_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repeated: bool,
) -> None:
    workflow = LiveWorkflow(tmp_path / "live")
    calls: list[str] = []

    def call(
        provider: str,
        model: str,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        cancelled: Callable[[], bool],
        *,
        effort: str = "",
    ) -> NativeResult:
        calls.append(prompt)
        if repeated or "REQUESTED FILES" not in prompt:
            return NativeResult(
                "ok",
                {"summary": "need context", "files": [], "need_files": ["engine/x.py"]},
                "",
                {},
            )
        content = "def add(a, b):\n    return a + b\n" if "core" in run_id else (
            "import unittest\nfrom calc import add\n"
            "class T(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n"
        )
        path = "calc.py" if "core" in run_id else "test_calc.py"
        return NativeResult(
            "ok", {"summary": "ok", "files": [{"path": path, "content": content}]}, "", {}
        )

    monkeypatch.setattr(workflow.clients, "call", call)
    workflow.file_reader = lambda project, names: "--- engine/x.py ---\ndef f():\n    pass\n"
    result, revision, _artifacts = workflow.implement_units(_unit_task(), lambda: False)
    assert result.outcome == ("failed" if repeated else "ok")
    assert bool(revision) is not repeated
    assert len(calls) == (3 if repeated else 4)
    assert any("REQUESTED FILES" in prompt for prompt in calls)


def test_unlimited_live_call_renews_lease_across_days(
    live_controller: Controller, clock: Clock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert live_controller.live is not None
    live_controller.live.clients.timeout = 0
    live_controller.lease_seconds = 21600
    live_controller.clock = clock
    observed: list[tuple[bool, int]] = []

    def run(task: Task, cancelled: Callable[[], bool]) -> tuple[StepResult, str, list[Artifact]]:
        for _ in range(72):
            clock.now += 3600
            observed.append((cancelled(), live_controller.expire_leases()))
        return StepResult("ok", "completed after three days"), "", []

    monkeypatch.setattr(live_controller.live, "run", run)
    task, _ = live_controller.submit("unlimited", "long reasoning", mode="live")
    live_controller.tick()
    assert (set(observed), live_controller.store.read(task.id).status) == ({(False, 0)}, "queued")
