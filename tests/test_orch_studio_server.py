"""orch studio's HTTP contract (docs/STUDIO.md), served from the demo backend."""

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from orchestragent.orch_studio_demo import DemoBackend
from orchestragent.orch_studio_server import Settings, StudioServer

TOKEN = "t0ken-for-tests"


class Clock:
    def __init__(self) -> None:
        self.now = 1_762_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def studio() -> Iterator[tuple[str, DemoBackend, Clock]]:
    clock = Clock()
    backend = DemoBackend(clock, start=False)
    server = StudioServer(("127.0.0.1", 0), backend, Settings(TOKEN, poll=0.05, ping=0.2))
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", backend, clock
    finally:
        server.shutdown()
        server.server_close()


def call(base: str, path: str, body: dict[str, Any] | None = None, *,
         headers: dict[str, str] | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(
        base + path, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
                 **(headers if headers is not None else {})},
        method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def advance(backend: DemoBackend, clock: Clock, seconds: float) -> None:
    for _ in range(int(seconds * 4)):
        clock.now += 0.25
        backend.advance()


def test_every_request_needs_the_token_and_changes_need_a_header(
        studio: tuple[str, DemoBackend, Clock]) -> None:
    base, _, _ = studio
    request = urllib.request.Request(base + "/api/boot")
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 401
    status, body = call(base, "/api/boot")
    assert status == 200 and body["api"] == 1 and body["demo"] is True
    # a cookie alone never authorises a POST
    status, body = call(base, "/api/run/start", {"spec": "R1. x"},
                        headers={"Authorization": "", "Cookie": f"orch_studio={TOKEN}"})
    assert status == 401 and body["error"]["code"] == "unauthorized"


def test_graph_node_and_diagnoses_follow_the_contract(
        studio: tuple[str, DemoBackend, Clock]) -> None:
    base, backend, clock = studio
    _, runs = call(base, "/api/runs")
    live = next(run for run in runs if run["status"] != "done")
    _, graph = call(base, f"/api/graph?run={live['id']}")
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes["T-004"]["status"] == "PARKED" and nodes["T-004"]["diagnosis"]["kind"] == "own"
    assert nodes["T-005"]["diagnosis"] == {**nodes["T-005"]["diagnosis"], "kind": "waiting"}
    assert {"from": "T-003", "to": "T-005"} in graph["edges"]
    status, node = call(base, f"/api/node?run={live['id']}&task=T-004")
    assert status == 200 and node["node"]["id"] == "T-004"
    assert node["checks"] and node["attempts"][-1]["outcome"] == "failed"
    assert all(route["model"] != "deepseek-v4-pro" for route in node["routes"])
    status, missing = call(base, f"/api/node?run={live['id']}&task=T-999")
    assert status == 404 and missing["error"]["code"] == "not_found"
    advance(backend, clock, 40)  # T-003 fails once, then a Claude patch merges it
    _, graph = call(base, f"/api/graph?run={live['id']}")
    assert {n["id"]: n["status"] for n in graph["nodes"]}["T-003"] == "DONE"


def test_guidance_unparks_a_node_and_rework_ripples_downstream(
        studio: tuple[str, DemoBackend, Clock]) -> None:
    base, backend, clock = studio
    run = "rdemo0002"
    status, body = call(base, "/api/node/message",
                        {"run": run, "task": "T-004", "text": "Reject unsafe integers",
                         "rework": False})
    assert status == 200 and body["node"]["status"] == "READY"
    advance(backend, clock, 60)
    _, graph = call(base, f"/api/graph?run={run}")
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes["T-004"]["status"] == "DONE"
    assert nodes["T-005"]["status"] == "PARKED"
    assert nodes["T-005"]["diagnosis"]["kind"] == "upstream"
    assert nodes["T-005"]["diagnosis"]["blamed"] == ["T-003"]
    call(base, "/api/node/message", {"run": run, "task": "T-003",
                                     "text": "Do not escape non-ASCII", "rework": True})
    advance(backend, clock, 60)
    _, graph = call(base, f"/api/graph?run={run}")
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes["T-005"]["ripple"] == "affected" and nodes["T-005"]["status"] == "DONE"
    _, node = call(base, f"/api/node?run={run}&task=T-003")
    assert [item["author"] for item in node["thread"]].count("you") == 1


def test_routes_retry_start_and_settings(studio: tuple[str, DemoBackend, Clock]) -> None:
    base, _, _ = studio
    run = "rdemo0002"
    status, body = call(base, "/api/node/route",
                        {"run": run, "task": "T-004", "route": "claude/claude-opus-5-5:high"})
    assert status == 200 and body["node"]["route_override"] is True
    assert body["node"]["route"]["vendor"] == "anthropic"
    status, body = call(base, "/api/node/route", {"run": run, "task": "T-004", "route": "x/y"})
    assert status == 422
    status, body = call(base, "/api/node/retry", {"run": run, "task": "T-001"})
    assert status == 409 and "not PARKED" in body["error"]["message"]
    status, body = call(base, "/api/run/start", {"spec": "Greeter\nR1. greet\nR2. slug"})
    assert status == 200 and body["run"]["title"] == "Greeter"
    status, settings = call(base, "/api/settings")
    assert {p["name"] for p in settings["providers"]} >= {"claude", "dsh"}
    status, body = call(base, "/api/settings/key", {"provider": "codex", "key": "sk-secret"})
    assert status == 200 and body["provider"]["configured"] is True
    assert "sk-secret" not in json.dumps(call(base, "/api/settings")[1])


def test_events_report_changes(studio: tuple[str, DemoBackend, Clock]) -> None:
    base, backend, clock = studio
    request = urllib.request.Request(base + "/api/events?run=rdemo0002",
                                     headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(request, timeout=5) as stream:
        assert stream.readline().startswith(b"event: hello")
        stream.readline(), stream.readline()
        advance(backend, clock, 3)
        lines = [stream.readline() for _ in range(3)]
    assert lines[0].startswith(b"event: changed")
