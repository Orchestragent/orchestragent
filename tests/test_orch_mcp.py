import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestragent import orch_mcp
from orchestragent.orch_mcp import TOOLS, handle, serve


def call(name: str, arguments: dict[str, object], ident: int = 7) -> dict[str, object]:
    reply = handle({"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}})
    assert reply is not None and reply["id"] == ident
    return reply["result"]  # type: ignore[no-any-return]


def text(result: dict[str, object]) -> str:
    return result["content"][0]["text"]  # type: ignore[index,no-any-return]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


@pytest.fixture
def fake_orch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in CLI: `plan` logs a run id, `run` records its argument and succeeds."""
    record = tmp_path / "calls.txt"
    script = tmp_path / "fake_orch.py"
    script.write_text(
        "import sys\n"
        f"open({str(record)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if 'plan' in sys.argv: print('[plan] run r1a2b3c4d; usage: orch cost --run r1a2b3c4d')\n"
        "if 'run' in sys.argv: print('run r1a2b3c4d  done')\n"
    )
    monkeypatch.setattr(orch_mcp, "orch_command", lambda: [sys.executable, str(script)])
    return record


def test_initialize_negotiates_a_supported_protocol_and_lists_tools() -> None:
    reply = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26"}})
    assert reply is not None
    result = reply["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == "orch" and "tools" in result["capabilities"]
    unknown = handle({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                      "params": {"protocolVersion": "1999-01-01"}})
    assert unknown is not None and unknown["result"]["protocolVersion"] == "2025-06-18"
    listed = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert listed is not None
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert names == [tool["name"] for tool in TOOLS] and "orch_start" in names
    assert all(tool["inputSchema"]["type"] == "object" for tool in TOOLS)


def test_notifications_get_no_reply_and_bad_input_gets_jsonrpc_errors() -> None:
    lines = "\n".join([
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "{not json",
        json.dumps({"jsonrpc": "2.0", "id": 5, "method": "resources/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                    "params": {"name": "rm_rf", "arguments": {}}}),
        json.dumps({"jsonrpc": "2.0", "id": 8, "method": "ping"}),
    ]) + "\n"
    out = io.StringIO()
    assert serve(io.StringIO(lines), out) == 0
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [None, 5, 6, 8]
    assert [r["error"]["code"] for r in replies[:3]] == [-32700, -32601, -32602]
    assert replies[3]["result"] == {}


@pytest.mark.parametrize("arguments", [
    {"repo": "relative/path", "spec": "R1. something long enough to count"},
    {"repo": "/nonexistent/orch/repo", "spec": "R1. something long enough to count"},
])
def test_tool_mistakes_are_tool_errors_not_crashes(arguments: dict[str, object]) -> None:
    result = call("orch_start", arguments)
    assert result["isError"] is True and "repo" in text(result)


def test_start_rejects_non_repositories_and_empty_specs(repo: Path, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert "not a git repository" in text(call("orch_start", {"repo": str(plain),
                                                             "spec": "R1. long enough spec"}))
    result = call("orch_start", {"repo": str(repo), "spec": "short"})
    assert result["isError"] is True and "spec" in text(result)
    result = call("orch_start", {"repo": str(repo), "spec": "R1. a long enough spec",
                                 "base": "HEAD; rm -rf /"})
    assert result["isError"] is True and "base" in text(result)


def test_start_plans_then_runs_exactly_the_planned_run_and_wait_reports_it(
    repo: Path, fake_orch: Path,
) -> None:
    spec = "R1. Add add(a, b) returning a + b to calc.py.\n"
    started = call("orch_start", {"repo": str(repo), "spec": spec, "jobs": 2})
    assert started["isError"] is False
    job = text(started).split()[2]
    waited = call("orch_wait", {"repo": str(repo), "job": job, "timeout_s": 30})
    state = json.loads(text(waited))
    assert state["state"] == "finished" and state["exit_code"] == 0
    assert state["run"] == "r1a2b3c4d"
    calls = fake_orch.read_text().splitlines()
    assert calls[0].startswith(f"--repo {repo} plan --base HEAD ")
    assert calls[0].endswith("spec.md")
    assert calls[1] == f"--repo {repo} run --jobs 2 r1a2b3c4d"
    saved = next((repo / ".git" / "orch" / "mcp" / "jobs").iterdir()) / "spec.md"
    assert saved.read_text() == spec
    status = text(call("orch_status", {"repo": str(repo), "job": job}))
    assert '"state":"finished"' in status.replace(" ", "")


def test_a_failed_plan_never_starts_a_run(repo: Path, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    record = tmp_path / "calls.txt"
    script = tmp_path / "failing_orch.py"
    script.write_text(f"import sys\nopen({str(record)!r}, 'a').write(sys.argv[3] + '\\n')\n"
                      "sys.exit(2)\n")
    monkeypatch.setattr(orch_mcp, "orch_command", lambda: [sys.executable, str(script)])
    job = text(call("orch_start", {"repo": str(repo), "spec": "R1. a spec long enough"})).split()[2]
    state = json.loads(text(call("orch_wait", {"repo": str(repo), "job": job, "timeout_s": 30})))
    assert state["state"] == "finished" and state["exit_code"] != 0
    assert record.read_text().split() == ["plan"]


def test_cancel_stops_a_running_job(repo: Path, tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "slow_orch.py"
    script.write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setattr(orch_mcp, "orch_command", lambda: [sys.executable, str(script)])
    job = text(call("orch_start", {"repo": str(repo), "spec": "R1. a spec long enough"})).split()[2]
    assert json.loads(text(call("orch_status", {"repo": str(repo), "job": job})
                           ).split("job: ", 1)[1].split("\n\n")[0])["state"] == "running"
    assert "stopped" in text(call("orch_cancel", {"repo": str(repo), "job": job}))
    state = json.loads(text(call("orch_wait", {"repo": str(repo), "job": job, "timeout_s": 15})))
    assert state["state"] in {"finished", "stopped"}


def test_job_ids_are_validated_before_touching_the_filesystem(repo: Path) -> None:
    for bad in ("../../etc", "zzz", "0123456789ab/../x"):
        result = call("orch_wait", {"repo": str(repo), "job": bad})
        assert result["isError"] is True


def test_start_with_a_run_resumes_it_without_planning(repo: Path, fake_orch: Path) -> None:
    started = call("orch_start", {"repo": str(repo), "run": "r1a2b3c4d", "jobs": 3})
    assert started["isError"] is False and "resuming run r1a2b3c4d" in text(started)
    job = text(started).split()[2]
    state = json.loads(text(call("orch_wait", {"repo": str(repo), "job": job,
                                               "timeout_s": 30})))
    assert state["state"] == "finished" and state["run"] == "r1a2b3c4d"
    assert fake_orch.read_text().splitlines() == [f"--repo {repo} run --jobs 3 r1a2b3c4d"]
    bad = call("orch_start", {"repo": str(repo), "run": "r1; rm -rf /"})
    assert bad["isError"] is True and "run" in text(bad)


def test_intervention_tools_validate_and_call_the_cli(repo: Path, fake_orch: Path) -> None:
    hinted = call("orch_hint", {"repo": str(repo), "task": "T-002", "text": "use integer cents"})
    assert hinted["isError"] is False
    assert fake_orch.read_text().splitlines()[-1] == (
        f"--repo {repo} hint T-002 use integer cents")
    call("orch_take", {"repo": str(repo), "task": "T-002", "run": "r1a2b3c4d"})
    assert fake_orch.read_text().splitlines()[-1] == f"--repo {repo} take T-002 --run r1a2b3c4d"
    assert call("orch_submit", {"repo": str(repo), "task": "T-2"})["isError"] is True
