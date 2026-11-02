import json
import subprocess
from pathlib import Path

import pytest

from orchestragent import orch_desktop
from orchestragent.orch_desktop import config_path, install, merge_config, notify

ENTRY: dict[str, object] = {"command": "/usr/local/bin/orch", "args": ["mcp"]}


def test_merge_keeps_other_servers_backs_up_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"theme": "dark",
                                "mcpServers": {"github": {"command": "gh-mcp"}}}))
    assert "registered orch" in merge_config(path, ENTRY)
    saved = json.loads(path.read_text())
    assert saved["theme"] == "dark" and saved["mcpServers"]["github"] == {"command": "gh-mcp"}
    assert saved["mcpServers"]["orch"] == ENTRY
    backup = path.with_name(path.name + ".orch-backup")
    assert json.loads(backup.read_text())["mcpServers"] == {"github": {"command": "gh-mcp"}}
    before = path.read_text()
    assert "already registered" in merge_config(path, ENTRY)
    assert path.read_text() == before


def test_merge_creates_a_missing_config_and_refuses_foreign_shapes(tmp_path: Path) -> None:
    fresh = tmp_path / "new" / "mcp.json"
    merge_config(fresh, ENTRY)
    assert json.loads(fresh.read_text()) == {"mcpServers": {"orch": ENTRY}}
    odd = tmp_path / "odd.json"
    odd.write_text("[1, 2]")
    with pytest.raises(ValueError, match="not a JSON object"):
        merge_config(odd, ENTRY)
    assert odd.read_text() == "[1, 2]"


def test_config_locations_per_client_and_platform(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    assert config_path("cursor", tmp_path) == tmp_path / ".cursor" / "mcp.json"
    monkeypatch.setattr(orch_desktop, "is_macos", lambda: True)
    assert config_path("claude-desktop", tmp_path) == (
        tmp_path / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    monkeypatch.setattr(orch_desktop, "is_macos", lambda: False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert config_path("claude-desktop", tmp_path) == (
        tmp_path / "cfg" / "Claude" / "claude_desktop_config.json")
    with pytest.raises(ValueError):
        config_path("claude-code", tmp_path)


def test_claude_code_install_uses_its_cli_at_user_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr("orchestragent.orch_desktop.shutil.which",
                        lambda name: f"/opt/bin/{name}" if name == "claude" else None)
    monkeypatch.setattr(orch_desktop, "server_entry", lambda: {
        "command": "/py", "args": ["-m", "orchestragent.orch_cli", "mcp"],
        "env": {"PYTHONPATH": "/src"}})

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "Added stdio MCP server orch", "")

    monkeypatch.setattr("orchestragent.orch_desktop.subprocess.run", fake_run)
    assert "Added" in install("claude-code")
    assert seen == [["/opt/bin/claude", "mcp", "add", "orch", "--scope", "user", "--env",
                     "PYTHONPATH=/src", "--", "/py", "-m", "orchestragent.orch_cli", "mcp"]]
    with pytest.raises(ValueError):
        install("vim")


def test_source_checkout_entry_runs_this_package(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = orch_desktop.server_entry()  # tests run from the source checkout
    assert entry["args"][-1] == "mcp"  # type: ignore[index]
    env = entry.get("env")
    assert isinstance(env, dict) and env["PYTHONPATH"].endswith("/src")


def test_notify_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("orchestragent.orch_desktop.shutil.which", lambda name: None)
    assert notify("t", "m") is False
    calls: list[list[str]] = []
    monkeypatch.setattr(orch_desktop, "is_macos", lambda: True)
    monkeypatch.setattr("orchestragent.orch_desktop.shutil.which", lambda name: "/usr/bin/" + name)
    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("orchestragent.orch_desktop.subprocess.run", fake_run)
    assert notify('run "r1" done', "repo → orch/r1") is True
    assert calls[0][:2] == ["osascript", "-e"]
    assert '"run \\"r1\\" done"' in calls[0][2]  # quoted for AppleScript, not interpolated raw


def test_orch_app_bundle_is_valid_and_its_launcher_serves_the_dashboard(
    tmp_path: Path,
) -> None:
    import os
    import plistlib
    import signal
    import time
    import urllib.request

    from orchestragent.orch_desktop import install_app
    app = tmp_path / "Orch.app"
    assert "installed" in install_app(app)
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert (info["CFBundleExecutable"], info["CFBundlePackageType"],
            info["CFBundleIdentifier"]) == ("Orch", "APPL", "dev.orchestragent.orch")
    launcher = app / "Contents" / "MacOS" / "Orch"
    assert os.access(launcher, os.X_OK)
    assert subprocess.run(["sh", "-n", str(launcher)]).returncode == 0
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    home = tmp_path / "home"
    env = {**os.environ, "HOME": str(home), "ORCH_APP_DRY": "1", "ORCH_APP_REPO": str(repo)}
    out = subprocess.run([str(launcher)], env=env, capture_output=True, text=True,
                         timeout=60).stdout
    state = home / "Library" / "Application Support" / "orch" / "app"
    pid = int((state / "server.pid").read_text())
    try:
        url = out.split("dashboard: ", 1)[1].split()[0]
        for _ in range(20):
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    assert response.status == 200
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise AssertionError("dashboard did not answer")
    finally:
        os.kill(pid, signal.SIGTERM)
    install_app(app)  # reinstall over its own bundle is fine
    with pytest.raises(ValueError):
        install_app(tmp_path / "Orch")
    (tmp_path / "Other.app").mkdir()
    with pytest.raises(ValueError, match="not an app bundle"):
        install_app(tmp_path / "Other.app")
