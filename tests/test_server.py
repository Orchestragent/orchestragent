import concurrent.futures
import http.client
import json
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from orchestragent.chat import Chat
from orchestragent.codeindex import CodeIndex
from orchestragent.controller import Controller
from orchestragent.live import LiveWorkflow
from orchestragent.memory import MemoryStore, Note
from orchestragent.server import Server

Request = Callable[..., tuple[int, dict[str, object]]]


@pytest.fixture
def api(controller: Controller) -> Iterator[Request]:
    server = Server(controller, 0)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()

    def call(
        method: str, path: str, body: object = None, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        payload = json.dumps(body) if body is not None else None
        connection.request(
            method, path, payload, {"Content-Type": "application/json", **(headers or {})}
        )
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    try:
        yield call
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def test_http_round_trip_and_detached_execution(api: Request, controller: Controller) -> None:
    status, task = api("POST", "/tasks", {"request_id": "api", "prompt": "一条需求"})
    # Submission connection is already closed; worker does not depend on any client session.
    for _ in range(5):
        controller.tick()
    code, done = api("GET", f"/tasks/{task['id']}")
    event_code, events = api("GET", f"/tasks/{task['id']}/events?after=0")
    assert (status, code, done["status"], done["cost_usd"], event_code, bool(events["events"])) == (
        201,
        200,
        "completed",
        0,
        200,
        True,
    )


def test_delete_conversation_preserves_tasks_and_memory(
    api: Request, controller: Controller,
) -> None:
    controller.chat = Chat(controller.store)
    controller.memory = MemoryStore(controller.store)
    controller.chat.post("delete-message", "remove this", "project")
    task, _ = controller.submit("keep-task", "keep this task")
    task.project = "project"
    with controller.store.transaction() as db:
        controller.store.save(db, task)
    controller.memory.remember(Note("project", "stage", "keep", "keep this memory"))
    status, result = api("DELETE", "/chat/conversations/project")
    assert (status, result) == (200, {"deleted": "project"})
    assert controller.chat.messages(0, "project") == []
    assert all(row["id"] != "project" for row in controller.chat.conversations())
    assert controller.store.read(task.id) == task
    assert controller.memory.stats("project")["notes"] == 1


@pytest.mark.parametrize("budget", [8192, 204800, 4194304])
def test_context_save_survives_reload_and_controls_injection(
    api: Request, controller: Controller, tmp_path: Path, budget: int,
) -> None:
    controller.chat = Chat(controller.store)
    root = tmp_path / "project"
    root.mkdir()
    (root / "engine.py").write_text("# engine\n" * 2000)
    index = CodeIndex.ensure(tmp_path / "index.db")
    index.build("default", root)
    code, saved = api("POST", "/chat/conversations/default/context",
                      {"bytes": budget, "paths": [" engine.py "]})
    reopened = Chat(controller.store)
    actual_budget, paths = reopened.context_of("default")
    injected = index.context("default", root, "engine", actual_budget, paths)
    assert (code, saved["bytes"], actual_budget, paths) == (
        200, budget, budget, ["engine.py"],
    )
    assert len(injected.encode()) <= budget
    assert bool(injected)
    assert ("[truncated]" in injected) == (budget < 18000)


@pytest.mark.parametrize("paths", [["engine"] * 9, [123], [None], "engine"])
def test_context_rejects_invalid_prefixes_without_saving(
    api: Request, controller: Controller, paths: object,
) -> None:
    controller.chat = Chat(controller.store)
    code, _ = api("POST", "/chat/conversations/default/context",
                  {"bytes": 200000, "paths": paths})
    assert (code, controller.chat.context_of("default")) == (400, (0, []))


@pytest.mark.parametrize("running", [False, True])
def test_restart_gate_checks_nondefault_conversations(
    api: Request, controller: Controller, running: bool,
) -> None:
    controller.chat = Chat(controller.store)
    controller.chat.post("busy", "working", "another-project")
    if running:
        controller.chat.pending()
    assert api("POST", "/maintenance/drain", {"enabled": True})[0] == 409


def test_restart_gate_blocks_new_work_until_released(api: Request, controller: Controller) -> None:
    assert api("POST", "/maintenance/drain", {"enabled": True})[0] == 200
    assert api("POST", "/tasks", {"request_id": "blocked", "prompt": "wait"})[0] == 503
    assert controller.tick() is False
    assert api("GET", "/health")[0] == 200
    assert api("POST", "/maintenance/drain", {"enabled": False})[0] == 200
    assert api("POST", "/tasks", {"request_id": "allowed", "prompt": "go"})[0] == 201


def test_restart_gate_waits_for_inflight_http_request(
    api: Request, controller: Controller, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()
    original = controller.settings.propose

    def blocked(instruction: str) -> dict[str, object]:
        entered.set()
        assert release.wait(3)
        return original(instruction)

    monkeypatch.setattr(controller.settings, "propose", blocked)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        request = pool.submit(api, "POST", "/settings/proposals", {"instruction": "test"})
        try:
            assert entered.wait(3)
            assert api("POST", "/maintenance/drain", {"enabled": True})[0] == 409
        finally:
            release.set()
        assert request.result()[0] == 200


def test_restart_gate_and_submission_have_no_admission_race(api: Request) -> None:
    with concurrent.futures.ThreadPoolExecutor() as pool:
        drain = pool.submit(api, "POST", "/maintenance/drain", {"enabled": True})
        submit = pool.submit(api, "POST", "/tasks", {"request_id": "race", "prompt": "go"})
        assert (drain.result()[0], submit.result()[0]) in {(200, 503), (409, 201)}


def test_restart_waits_until_worker_cleanup_finishes(
    api: Request, controller: Controller, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()

    def cleaning_up() -> bool:
        entered.set()
        assert release.wait(3)
        return False

    monkeypatch.setattr(controller, "_tick", cleaning_up)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        worker = pool.submit(controller.tick)
        try:
            assert entered.wait(3)
            assert api("POST", "/maintenance/drain", {"enabled": True})[0] == 409
        finally:
            release.set()
        assert worker.result() is False
    assert api("POST", "/maintenance/drain", {"enabled": True})[0] == 200


@pytest.mark.parametrize("prompt,code", [("One", 200), ("Different", 409)])
def test_http_idempotency(api: Request, prompt: str, code: int) -> None:
    api("POST", "/tasks", {"request_id": "duplicate", "prompt": "One"})
    result, _ = api("POST", "/tasks", {"request_id": "duplicate", "prompt": prompt})
    assert result == code


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"request_id": "x", "prompt": ""},
        {"request_id": 2, "prompt": "x"},
        {"request_id": "x", "prompt": "x", "scenario": "unknown"},
        {"request_id": "x", "prompt": "x", "provider": "codex"},
        {"request_id": "x", "prompt": "x", "approved": True},
        {"request_id": "x", "prompt": "x", "budget_usd": 100},
        {"request_id": "x", "prompt": "a" * 17000},
    ],
)
def test_http_rejects_invalid_or_privilege_fields(api: Request, body: object) -> None:
    status, _ = api("POST", "/tasks", body)
    assert status == 400


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
        {"Host": "evil.example"},
        {"Host": "127.0.0.1.evil.example"},
    ],
)
def test_browser_and_dns_rebinding_requests_rejected(api: Request, headers: dict[str, str]) -> None:
    status, _ = api("GET", "/health", headers=headers)
    assert status == 403


@pytest.mark.parametrize("path", ["/missing", "/tasks/missing", "/tasks/missing/events"])
def test_missing_resources(api: Request, path: str) -> None:
    assert api("GET", path)[0] == 404


def test_approval_http_bound_to_operation(api: Request, controller: Controller) -> None:
    _, task = api(
        "POST", "/tasks", {"request_id": "approval", "prompt": "Approve", "scenario": "approval"}
    )
    controller.tick()
    controller.tick()
    waiting = controller.store.read(str(task["id"]))
    approval = waiting.approval
    assert approval is not None
    code, approved = api(
        "POST",
        f"/tasks/{waiting.id}/approvals/{approval.id}",
        {
            "decision": "approve",
            "operation_digest": approval.operation_digest,
        },
    )
    assert (code, approved["status"]) == (200, "queued")


def test_actions_and_cursor_over_http(api: Request, controller: Controller) -> None:
    _, task = api("POST", "/tasks", {"request_id": "actions", "prompt": "Pause"})
    task_id = str(task["id"])
    _, paused = api("POST", f"/tasks/{task_id}/actions", {"action": "pause"})
    _, page = api("GET", f"/tasks/{task_id}/events")
    _, cancelled = api("POST", f"/tasks/{task_id}/actions", {"action": "cancel"})
    _, next_page = api("GET", f"/tasks/{task_id}/events?after={page['cursor']}")
    assert (paused["status"], cancelled["status"], bool(next_page["events"])) == (
        "paused",
        "cancelled",
        True,
    )


def test_concurrent_http_submit(api: Request) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: api(
                    "POST",
                    "/tasks",
                    {
                        "request_id": "parallel",
                        "prompt": "Same",
                    },
                ),
                range(4),
            )
        )
    assert sorted(code for code, _ in results) == [200, 200, 200, 201]


def test_settings_proposal_apply_and_task_snapshot(api: Request, controller: Controller) -> None:
    _, old = api("POST", "/tasks", {"request_id": "before", "prompt": "Before settings"})
    _, proposal = api("POST", "/settings/proposals", {"instruction": "Claude 请先审查测试"})
    code, settings = api("POST", f"/settings/proposals/{proposal['id']}/apply", {})
    _, new = api("POST", "/tasks", {"request_id": "after", "prompt": "After settings"})
    original = controller.store.read(str(old["id"]))
    assert (code, original.policy_revision, new["policy_revision"], new["policy"]) == (
        200,
        1,
        2,
        settings["policy"],
    )


@pytest.mark.parametrize("revision", [True, "1", None, -1])
def test_settings_invalid_revision(api: Request, revision: object) -> None:
    _, settings = api("GET", "/settings")
    code, _ = api("POST", "/settings", {"base_revision": revision, "policy": settings["policy"]})
    assert code in {400, 409}


def test_settings_cannot_enable_billing(api: Request) -> None:
    _, settings = api("GET", "/settings")
    policy = settings["policy"]
    assert isinstance(policy, dict)
    policy["budget_usd"] = 500
    code, _ = api("POST", "/settings", {"base_revision": 1, "policy": policy})
    assert code == 400


@pytest.mark.parametrize("mode,expected", [("mock", 201), ("live", 400), ("invalid", 400)])
def test_http_execution_modes(api: Request, mode: str, expected: int) -> None:
    code, _ = api("POST", "/tasks", {"request_id": "mode", "prompt": "work", "mode": mode})
    assert code == expected


def test_health_contract_version(api: Request) -> None:
    assert api("GET", "/health") == (200, {"mode": "mock", "version": 2})


@pytest.fixture
def live_access(controller: Controller, tmp_path: Path) -> None:
    controller.live = LiveWorkflow(tmp_path / "live")


@pytest.mark.parametrize("tampered,expected", [(False, 200), (True, 409)])
def test_api_authorization_is_bound_and_never_echoes_key(
    api: Request, live_access: None, tampered: bool, expected: int
) -> None:
    body: dict[str, object] = {"provider": "dsh", "cap_cents": 500, "funding_kind": "balance"}
    code, preview = api("POST", "/providers/authorization/preview", body)
    code, result = api(
        "POST",
        "/providers/authorization/apply",
        {
            **body,
            "cap_cents": 1000 if tampered else 500,
            "operation_digest": preview["operation_digest"],
            "api_key": "fake-test-secret",
        },
    )
    assert (code, "fake-test-secret" in json.dumps(result)) == (expected, False)


def test_mock_service_cannot_grant_api_spend(api: Request) -> None:
    assert (
        api(
            "POST",
            "/providers/authorization/preview",
            {"provider": "dsh", "cap_cents": 500, "funding_kind": "balance"},
        )[0]
        == 400
    )


@pytest.fixture
def remote(controller: Controller) -> Iterator[Request]:
    server = Server(controller, 0, "device-token")
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()

    def call(
        method: str, path: str, body: object = None, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        payload = json.dumps(body) if body is not None else None
        connection.request(
            method, path, payload, {"Content-Type": "application/json", **(headers or {})}
        )
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    try:
        yield call
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def test_remote_listener_requires_its_token(remote: Request) -> None:
    assert remote("GET", "/health")[0] == 401
    assert remote("GET", "/health", headers={"Authorization": "Bearer wrong"})[0] == 401
    status, health = remote("GET", "/health", headers={"Authorization": "Bearer device-token"})
    assert (status, health["version"]) == (200, 2)


def test_remote_listener_ignores_proxy_host_but_rejects_browser_origin(remote: Request) -> None:
    token = {"Authorization": "Bearer device-token"}
    status, _ = remote("GET", "/tasks", headers={**token, "Host": "yiqun-mac.example.ts.net"})
    assert status == 200
    assert remote("GET", "/tasks", headers={**token, "Origin": "https://evil.example"})[0] == 403


def test_remote_listener_never_accepts_token_in_query(remote: Request) -> None:
    assert remote("GET", "/health?token=device-token")[0] == 401
