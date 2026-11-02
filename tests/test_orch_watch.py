import hashlib
import http.client
import io
import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from orchestragent.orch_cli import main
from orchestragent.orch_state import OrchState, OrchTask, Run
from orchestragent.orch_usage import Call
from orchestragent.orch_watch import Source, connect, derive, export, read, stats, view
from orchestragent.orch_watch_server import (
    COOKIE,
    Settings,
    WatchServer,
    is_loopback,
    new_token,
)
from orchestragent.orch_watch_tui import (
    Caps,
    Ui,
    choose,
    detect,
    gantt_lanes,
    move,
    palette,
    plain,
    render,
)
from orchestragent.serialization import dumps
from tests.conftest import Clock

SECRETS = ("sk-THISISASECRETKEY123", "hunter2", "secret spec text", "Traceback")
DSF_USAGE = {"input_tokens": 900, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0,
             "output_tokens": 400, "settled_cents": 4, "forwarded": 1}


def receipt(state: OrchState, clock: Clock, call_id: str, provider: str, purpose: str,
            start: float, end: float | None, *, task: str = "", attempt: int = 1,
            scope: str = "execution", usage: dict[str, object] | None = None) -> None:
    clock.now = start
    model = "claude-opus-5-5" if provider == "claude" else "deepseek-flash"
    call = Call(call_id, "r1", provider, model, purpose, start, task_id=task, attempt=attempt,
                sample=f"a{attempt}", scope=scope)
    if end is None:  # in flight: the durable row exists, the final receipt does not yet
        with state.store.transaction() as db:
            db.execute("INSERT INTO orch_calls VALUES (?,?,?,?)",
                       (call.id, call.run_id, call.started_at, dumps(asdict(call))))
        return
    with state.usage.capture(call):
        call.outcome, call.usage = "ok", dict(usage or DSF_USAGE)
        clock.now = end


def step(state: OrchState, clock: Clock, at: float, task: str, status: str, kind: str,
         attempt: int = 0, data: dict[str, object] | None = None, attempts: int = 0) -> None:
    clock.now = at
    if attempts:
        state.transition("r1", task, status, kind, data or {}, attempt=attempt, attempts=attempts)
    else:
        state.transition("r1", task, status, kind, data or {}, attempt=attempt)


def note(state: OrchState, clock: Clock, at: float, kind: str, data: dict[str, object],
         task: str = "", attempt: int = 0) -> None:
    clock.now = at
    state.record("r1", kind, data, task, attempt)


@pytest.fixture(scope="module")
def state_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Planning calls, a failed-then-merged task, and a dependent task still in flight.

    Module-scoped: every test only reads it (one test proves the bytes never change).
    """
    clock = Clock()
    path = tmp_path_factory.mktemp("watch") / "orch" / "state.sqlite"
    state = OrchState(path, clock=clock)
    receipt(state, clock, "plan-1", "claude", "plan", 1000, 1010, scope="planning",
            usage={"input_tokens": 2, "cache_read_input_tokens": 100,
                   "cache_creation_input_tokens": 10, "output_tokens": 500,
                   "provider_estimate_usd": 0.05})
    receipt(state, clock, "design-1", "dsh", "test-design", 1011, 1020, task="T-001",
            scope="planning")
    receipt(state, clock, "design-2", "dsh", "test-design", 1011, 1016, task="T-002",
            scope="planning")
    receipt(state, clock, "design-2i", "dsh", "integration-test-design", 1011, 1018,
            task="T-002", scope="planning")
    clock.now = 1021
    tasks = [OrchTask("T-001", "goal with token=hunter2", ["R1"], ["a.py"], [], ["t1"], "c"),
             OrchTask("T-002", "uses a", ["R2"], ["b.py"], ["T-001"], ["t2"], "c")]
    spec = "secret spec text sk-THISISASECRETKEY123"
    state.create_run(Run("r1", "repo", "b" * 40, "orch/r1", spec, {}, "p", "", 1021.0), tasks)
    note(state, clock, 1022, "run.started", {"jobs": 2})
    step(state, clock, 1022, "T-001", "READY", "task.ready")
    step(state, clock, 1023, "T-001", "RUNNING", "task.started", 1, attempts=1)
    note(state, clock, 1023, "attempt.called", {"route": "dsh/deepseek-flash", "sample": "a1"},
         "T-001", 1)
    receipt(state, clock, "build-1", "dsh", "build", 1023, 1030, task="T-001")
    step(state, clock, 1030, "T-001", "VERIFYING", "task.verifying", 1)
    step(state, clock, 1031, "T-001", "READY", "task.failed", 1,
         {"category": "test", "error": "Traceback: sk-THISISASECRETKEY123 failed"})
    step(state, clock, 1032, "T-001", "RUNNING", "task.started", 2, attempts=2)
    clock.now = 1033
    state.request_context("r1", "T-001", ["a.py::helper"], "rev")
    receipt(state, clock, "build-2", "dsh", "build", 1033, 1040, task="T-001", attempt=2)
    step(state, clock, 1040, "T-001", "VERIFYING", "task.verifying", 2)
    step(state, clock, 1041, "T-001", "MERGING", "task.merging", 2)
    note(state, clock, 1041.5, "task.gate", {"tests": 3, "skipped": 0}, "T-001", 2)
    step(state, clock, 1042, "T-001", "DONE", "task.done", 2)
    step(state, clock, 1042, "T-002", "READY", "task.ready")
    step(state, clock, 1043, "T-002", "RUNNING", "task.started", 1, attempts=1)
    note(state, clock, 1043, "attempt.called", {"route": "dsh/deepseek-flash", "sample": "a1"},
         "T-002", 1)
    receipt(state, clock, "build-3", "dsh", "build", 1043, None, task="T-002")
    note(state, clock, 1044, "attempt.called",
         {"route": "claude/claude-opus-5-5", "purpose": "review", "review": "x"}, "T-002", 1)
    return path


@pytest.fixture(scope="module")
def model(state_db: Path) -> dict[str, Any]:
    db = connect(state_db)
    try:
        return derive(read(db, "r1", 1050.0))
    finally:
        db.close()


@pytest.fixture
def served(state_db: Path) -> Iterator[tuple[WatchServer, str]]:
    token = new_token()
    server = WatchServer(("127.0.0.1", 0), Settings(state_db, token=token, poll=0.05))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield server, token
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def fetch(server: WatchServer, path: str, headers: dict[str, str] | None = None
          ) -> http.client.HTTPResponse:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    connection.request("GET", path, headers=headers or {})
    return connection.getresponse()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def by_id(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {node["id"]: node for node in model["nodes"]}


# --- model derivation -----------------------------------------------------------------


def test_graph_has_every_architecture_node_dependency_depth_and_edges(
        model: dict[str, Any]) -> None:
    nodes = by_id(model)
    assert {"cli", "orch", "planner", "guard", "deepseek", "claude", "sandbox", "merge",
            "design:T-001", "design:T-002", "task:T-001", "task:T-002"} <= set(nodes)
    assert (nodes["task:T-001"]["depth"], nodes["task:T-002"]["depth"]) == (0, 1)
    edges = {edge["id"]: edge["kind"] for edge in model["edges"]}
    assert edges["task:T-001>task:T-002"] == "dep"
    assert {"design:T-001>guard", "guard>deepseek", "planner>claude", "task:T-001>sandbox",
            "task:T-002>claude"} <= set(edges)


def test_task_history_follows_state_events_including_the_failure(model: dict[str, Any]) -> None:
    history = [entry[1] for entry in by_id(model)["task:T-001"]["history"]]
    assert history == ["planned", "ready", "running", "verifying", "failed", "running",
                       "verifying", "merging", "done"]
    assert [p[1] for p in model["phases"]] == ["planning", "planned", "running"]


def test_in_flight_calls_come_from_open_receipts_and_unmatched_attempt_events(
        model: dict[str, Any]) -> None:
    seen = view(model, 1050.0)
    live = {(call["id"], call["provider"]) for call in seen["in_flight"]}
    assert ("build-3", "dsh") in live
    assert any(call_id.startswith("event-") and provider == "claude"
               for call_id, provider in live)
    finished = {call["id"]: call for call in seen["finished"]}
    assert finished["build-1"]["tps"] == round(400 / 7, 1)
    assert seen["cny_cents"] == 20 and seen["usd"] == 0.05
    assert view(model, 1012.0)["phase"] == "planning"


def test_integration_test_design_is_its_own_shard_and_lane(model: dict[str, Any]) -> None:
    nodes = by_id(model)
    assert (nodes["design:T-002"]["label"], nodes["design:T-002:int"]["label"]) == (
        "design T-002", "design T-002 ∫")
    owners = {call["id"]: call["node"] for call in model["calls"]}
    assert (owners["design-2"], owners["design-2i"]) == ("design:T-002", "design:T-002:int")
    lanes = gantt_lanes(model)
    assert lanes.index("design:T-002:int") == lanes.index("design:T-002") + 1
    ascii_text = "\n".join(render(model, 1050.0, 160, 48, Caps(8, False), Ui()).lines())
    unicode_text = "\n".join(render(model, 1050.0, 160, 48, Caps(256, True), Ui()).lines())
    assert "design T-002 int" in ascii_text and "design T-002 ∫" in unicode_text
    out = io.StringIO()
    plain(iter([model]).__next__, out, replay=True, speed=1e9, sleep=lambda s: None,
          ascii_only=True)
    assert "design T-002 int" in out.getvalue() and out.getvalue().isascii()


def test_token_figures_are_output_tokens_and_speed_is_output_per_second(
        model: dict[str, Any]) -> None:
    seen = view(model, 1050.0)
    outputs = [call["tokens"]["output"] for call in seen["finished"]]
    assert seen["output"] == sum(outputs) == 500 + 400 * 5
    assert seen["input"] == 112 + 1000 * 5
    assert stats(model, 1050.0)["task:T-001"]["output"] == 800
    done = next(entry for entry in model["log"]
                if entry["kind"] == "call.done" and entry["call"] == "build-1")
    assert done["summary"] == "build ok 7.0s · in 1000 · out 400 tok · 57.1 tok/s"
    assert "1400" not in done["summary"]  # never input+output
    text = "\n".join(render(model, 1050.0, 160, 48, Caps(256, True),
                            Ui(selected="task:T-001")).lines())
    assert "out 2.5k tok" in text and "in 2.0k / out 800 tok" in text and "800 out" in text


def test_gantt_cursor_never_overwrites_axis_labels(model: dict[str, Any]) -> None:
    for t in (1000.0, 1025.0, 1050.0):
        grid = render(model, t, 140, 42, Caps(256, True), Ui())
        axis = next(y for y, line in enumerate(grid.lines()) if "+00:00" in line)
        assert "cursor" not in grid.styles[axis]
        assert re.fullmatch(r"[ +\d:]*", grid.lines()[axis].strip().split(" ", 1)[1]) is not None


def test_messages_cover_requests_results_retries_context_and_test_failures(
        model: dict[str, Any]) -> None:
    kinds = {message["kind"] for message in model["messages"]}
    assert {"request", "result", "retry", "context", "testfail", "verify", "merge", "done",
            "dispatch"} <= kinds
    request = next(m for m in model["messages"] if m["ref"] == "plan-1")
    assert request["path"] == ["orch", "planner", "claude"]


def test_critical_path_runs_through_the_planner_slowest_shard_and_dependency_chain(
        model: dict[str, Any]) -> None:
    critical = {call["id"] for call in model["calls"] if call["critical"]}
    assert {"plan-1", "design-1", "build-1", "build-2"} <= critical
    assert "design-2" not in critical
    assert by_id(model)["task:T-001"]["critical"]


def test_model_and_export_never_carry_prompts_specs_or_secrets(
        model: dict[str, Any], tmp_path: Path) -> None:
    text = json.dumps(model) + export(model, tmp_path / "run.html").read_text()
    assert not [secret for secret in SECRETS if secret in text]


# --- read-only access ---------------------------------------------------------------


def test_state_database_is_opened_read_only(state_db: Path, tmp_path: Path) -> None:
    before = digest(state_db)
    db = connect(state_db)
    with pytest.raises(sqlite3.OperationalError):
        db.execute("INSERT INTO orch_rules VALUES ('x', '{}')")
    db.close()
    source = Source(state_db)
    assert source.poll() is not None and source.poll() is None  # unchanged: no new model
    source.close()
    assert digest(state_db) == before
    missing = tmp_path / "absent.sqlite"
    with pytest.raises(FileNotFoundError):
        connect(missing)
    assert not missing.exists() and Source(missing).poll() is None


# --- web: auth, streaming, export ----------------------------------------------------------


@pytest.mark.parametrize(("host", "loopback"), [
    ("127.0.0.1", True), ("localhost", True), ("::1", True), ("127.0.0.2", True),
    ("0.0.0.0", False), ("::", False), ("192.168.1.5", False), ("example.com", False),
])
def test_loopback_detection(host: str, loopback: bool) -> None:
    assert is_loopback(host) is loopback


def test_non_loopback_bind_requires_a_token(state_db: Path) -> None:
    with pytest.raises(ValueError, match="requires a token"):
        WatchServer(("0.0.0.0", 0), Settings(state_db))


@pytest.mark.parametrize(("credentials", "status"), [
    ({}, 401),
    ({"Authorization": "Bearer wrong"}, 401),
    ({"Cookie": f"{COOKIE}=wrong"}, 401),
    ({"Authorization": "Bearer {token}"}, 200),
    ({"Cookie": COOKIE + "={token}"}, 200),
])
def test_every_request_needs_the_token_when_one_is_set(
        served: tuple[WatchServer, str], credentials: dict[str, str], status: int) -> None:
    server, token = served
    headers = {key: value.format(token=token) for key, value in credentials.items()}
    response = fetch(server, "/api/model", headers)
    assert response.status == status
    if status == 200:
        assert json.loads(response.read())["run"]["id"] == "r1"


def test_query_token_becomes_an_http_only_cookie_and_leaves_the_url(
        served: tuple[WatchServer, str]) -> None:
    server, token = served
    response = fetch(server, f"/?token={token}&run=r1")
    assert response.status == 303
    assert response.getheader("Location") == "/?run=r1"
    assert f"{COOKIE}={token}; HttpOnly" in (response.getheader("Set-Cookie") or "")
    page = fetch(server, "/", {"Cookie": f"{COOKIE}={token}"})
    assert page.status == 200 and "no-referrer" in (page.getheader("Referrer-Policy") or "")


@pytest.mark.parametrize(("host", "status"), [("localhost:8780", 200), ("evil.example:8780", 401)])
def test_tokenless_loopback_answers_only_loopback_hosts(
        state_db: Path, host: str, status: int) -> None:
    server = WatchServer(("127.0.0.1", 0), Settings(state_db))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.putrequest("GET", "/api/runs", skip_host=True)
        connection.putheader("Host", host)
        connection.endheaders()
        assert connection.getresponse().status == status
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_events_stream_pushes_the_model(served: tuple[WatchServer, str]) -> None:
    server, token = served
    response = fetch(server, "/api/events", {"Authorization": f"Bearer {token}"})
    assert response.getheader("Content-Type", "").startswith("text/event-stream")
    assert response.readline().strip() == b"event: model"
    data = response.readline().decode()
    assert json.loads(data.removeprefix("data: "))["run"]["id"] == "r1"
    response.close()


def test_export_is_one_self_contained_offline_file(model: dict[str, Any], tmp_path: Path) -> None:
    text = export(model, tmp_path / "run.html", speed=4).read_text()
    assert not re.search(r"(?i)https?://", text)
    assert not re.search(r"(?i)<(script|link|img)[^>]+(src|href)=", text)
    boot = re.search(r'<script type="application/json" id="boot">(.*?)</script>', text, re.S)
    assert boot is not None
    data = json.loads(boot.group(1))
    assert (data["mode"], data["speed"], data["model"]["run"]["id"]) == ("export", 4, "r1")


# --- terminal UI -----------------------------------------------------------------------------


@pytest.mark.parametrize(("env", "encoding", "colors", "ascii_only", "expected"), [
    ({}, "UTF-8", 256, False, Caps(256, True)),
    ({}, "utf8", 8, False, Caps(8, True)),
    ({"NO_COLOR": "1"}, "UTF-8", 256, False, Caps(0, True)),
    ({}, "ANSI_X3.4-1968", 256, False, Caps(256, False)),
    ({}, "UTF-8", 256, True, Caps(256, False)),
    ({}, "UTF-8", 2, False, Caps(0, True)),
])
def test_capabilities_degrade(env: dict[str, str], encoding: str, colors: int, ascii_only: bool,
                              expected: Caps) -> None:
    assert detect(env, encoding, colors, True, ascii_only) == expected


@pytest.mark.parametrize(("web", "plain_flag", "stdout_tty", "term", "expected"), [
    (False, False, True, "xterm-256color", "tui"),
    (False, False, False, "xterm-256color", "plain"),
    (False, False, True, "dumb", "plain"),
    (False, False, True, "", "plain"),
    (False, True, True, "xterm", "plain"),
    (True, False, False, "dumb", "web"),
])
def test_mode_choice_prefers_terminal_and_falls_back_to_plain(
        web: bool, plain_flag: bool, stdout_tty: bool, term: str, expected: str) -> None:
    assert choose(web=web, plain=plain_flag, stdout_tty=stdout_tty, stdin_tty=True,
                  term=term) == expected


def test_palette_respects_colour_depth() -> None:
    assert {colour for colour, _ in palette(Caps(0, True)).values()} == {-1}
    assert max(colour for colour, _ in palette(Caps(8, True)).values()) < 8
    assert max(colour for colour, _ in palette(Caps(256, True)).values()) > 8


def styles(grid: Any) -> set[str]:
    return {style for row in grid.styles for style in row}


def test_full_render_draws_boxes_states_and_moving_messages(model: dict[str, Any]) -> None:
    message = next(m for m in model["messages"] if m["ref"] == "build-2" and m["kind"] == "retry")
    grid = render(model, 1050.0, 160, 48, Caps(256, True), Ui(selected="task:T-002"),
                  [(message, 0.5)])
    text = "\n".join(grid.lines())
    for label in ("Orchestrator", "Opus planner", "DeepSeek API", "Claude API", "DSH guard",
                  "bwrap sandbox", "git merge", "design T-001", "T-002", "parallel"):
        assert label in text
    assert "┌" in text and "╔" in text  # boxes, and the selected node
    assert {"state.done", "state.running", "msg.retry", "bar.deepseek"} <= styles(grid)


def test_ascii_render_uses_only_ascii(model: dict[str, Any]) -> None:
    grid = render(model, 1050.0, 160, 48, Caps(8, False), Ui())
    text = "\n".join(grid.lines())
    assert text.isascii() and "+-" in text and "Orchestrator" in text


def test_small_terminal_switches_to_compact_list(model: dict[str, Any]) -> None:
    text = "\n".join(render(model, 1050.0, 80, 24, Caps(0, True), Ui()).lines())
    assert "BUILD" in text and "T-002" in text and "┌" not in text
    tiny = "\n".join(render(model, 1050.0, 30, 8, Caps(0, True), Ui()).lines())
    assert "too small" in tiny


def test_detail_overlay_lists_calls_and_events(model: dict[str, Any]) -> None:
    text = "\n".join(render(model, 1050.0, 160, 48, Caps(256, True),
                            Ui(selected="task:T-001", detail=True)).lines())
    assert "details" in text and "build a2" in text and "task.failed" in text


def test_arrow_and_tab_selection_walks_the_graph(model: dict[str, Any]) -> None:
    assert move(model, "cli", "tab") == "orch"
    assert move(model, "orch", "right") == "planner"
    assert move(model, "planner", "down") == "design:T-001"
    assert move(model, "cli", "left") == "cli"


def test_plain_replay_prints_one_line_per_event_and_a_status(model: dict[str, Any]) -> None:
    out = io.StringIO()
    loads = iter([model])
    assert plain(lambda: next(loads, None), out, replay=True, speed=1e9,
                 sleep=lambda seconds: None) == 0
    text = out.getvalue()
    for kind in ("call.sent", "call.done", "task.failed", "context.request", "task.done"):
        assert kind in text
    assert text.splitlines()[-1].startswith("status run=r1 ")


def test_plain_live_follows_until_interrupted(model: dict[str, Any]) -> None:
    out = io.StringIO()

    def stop(seconds: float) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        plain(lambda: model, out, replay=False, speed=1, sleep=stop, clock=lambda: 1050.0)
    assert "task.done" in out.getvalue() and "in_flight=2" in out.getvalue()


def test_cli_without_a_terminal_streams_plain_text(
        state_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["watch", "--db", str(state_db), "--replay", "--speed", "1000000"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("orch watch (plain) run r1") and "task.failed" in output


def test_cli_export_and_web_only_flags(state_db: Path, tmp_path: Path,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "replay.html"
    assert main(["watch", "--db", str(state_db), "--export", str(target)]) == 0
    assert target.is_file()
    assert main(["watch", "--db", str(state_db), "--bind", "0.0.0.0"]) == 1
    assert "apply to --web" in capsys.readouterr().err


def test_scheduler_detail_names_its_state_once_and_counts_the_runs_calls(
    model: dict[str, Any],
) -> None:
    from orchestragent.orch_watch_tui import Caps, Ui, render
    t = float(model["t1"])
    grid = render(model, t, 140, 42, Caps(256, True), Ui(selected="orch"))
    text = "\n".join("".join(row) for row in grid.chars)
    line = next(row for row in text.splitlines() if " Orchestrator " in row and "─" in row)
    state = line.split(" Orchestrator ", 1)[1].split()[0]
    assert line.count(f" {state} ") == 1
    assert f"calls {len(model['calls'])}" in text
