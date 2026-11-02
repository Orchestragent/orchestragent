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


SOURCE_ENTRY: dict[str, object] = {"command": "/py", "args": ["-m", "orchestragent.orch_cli", "mcp"],
                                   "env": {"PYTHONPATH": "/src"}}


def test_each_host_gets_its_own_config_shape(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch_desktop, "server_entry", lambda: SOURCE_ENTRY)
    monkeypatch.setattr(orch_desktop, "is_macos", lambda: False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    for client in ("gemini", "windsurf", "vscode", "opencode"):
        install(client, tmp_path)
    vscode = json.loads((tmp_path / "cfg" / "Code" / "User" / "mcp.json").read_text())
    assert vscode == {"servers": {"orch": {"type": "stdio", "command": "/py",
                                           "args": ["-m", "orchestragent.orch_cli", "mcp"],
                                           "env": {"PYTHONPATH": "/src"}}}}
    opencode = json.loads((tmp_path / "cfg" / "opencode" / "opencode.json").read_text())
    assert opencode["mcp"]["orch"] == {
        "type": "local", "command": ["/py", "-m", "orchestragent.orch_cli", "mcp"],
        "enabled": True, "timeout": 660000, "environment": {"PYTHONPATH": "/src"}}
    windsurf = json.loads((tmp_path / ".codeium" / "windsurf" / "mcp_config.json").read_text())
    assert windsurf["mcpServers"]["orch"] == SOURCE_ENTRY
    gemini = json.loads((tmp_path / ".gemini" / "settings.json").read_text())["mcpServers"]["orch"]
    # Gemini CLI refuses PYTHONPATH overrides, so the source path is put on sys.path in -c
    assert "env" not in gemini and gemini["timeout"] == 660000
    assert gemini["args"][0] == "-c" and "'/src'" in gemini["args"][1] and gemini["args"][2] == "mcp"
    assert config_path("vscode", tmp_path) == tmp_path / "cfg" / "Code" / "User" / "mcp.json"
    monkeypatch.setattr(orch_desktop, "is_macos", lambda: True)
    assert config_path("vscode", tmp_path) == (
        tmp_path / "Library" / "Application Support" / "Code" / "User" / "mcp.json")


def test_gemini_bootstrap_runs_the_cli_from_the_source_path(tmp_path: Path) -> None:
    import sys

    root = Path(orch_desktop.__file__).resolve().parents[1]
    _, shaped = orch_desktop.client_entry("gemini", {
        "command": sys.executable, "args": ["-m", "orchestragent.orch_cli", "--help"],
        "env": {"PYTHONPATH": str(root)}})
    args = shaped["args"]
    assert isinstance(args, list)
    done = subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=60,
                          cwd=tmp_path, env={"PATH": "/usr/bin:/bin"})
    assert done.returncode == 0 and "usage: orch" in done.stdout


def test_codex_install_uses_its_cli_and_raises_the_tool_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    config = tmp_path / "config.toml"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr("orchestragent.orch_desktop.shutil.which",
                        lambda name: f"/opt/bin/{name}" if name == "codex" else None)
    monkeypatch.setattr(orch_desktop, "server_entry", lambda: SOURCE_ENTRY)

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        config.write_text('model = "x"\n\n[mcp_servers.orch]\ncommand = "/py"\n'
                          '\n[mcp_servers.orch.env]\nPYTHONPATH = "/src"\n')
        return subprocess.CompletedProcess(command, 0, "", "WARNING: noise\nAdded global MCP "
                                                             "server 'orch'.\n")

    monkeypatch.setattr("orchestragent.orch_desktop.subprocess.run", fake_run)
    assert install("codex") == "Added global MCP server 'orch'."
    assert seen == [["/opt/bin/codex", "mcp", "add", "orch", "--env", "PYTHONPATH=/src", "--",
                     "/py", "-m", "orchestragent.orch_cli", "mcp"]]
    assert "[mcp_servers.orch]\ntool_timeout_sec = 660\ncommand" in config.read_text()
    install("codex")  # a second install keeps one timeout line
    assert config.read_text().count("tool_timeout_sec") == 1
    monkeypatch.setattr("orchestragent.orch_desktop.shutil.which", lambda name: None)
    with pytest.raises(ValueError, match="Codex CLI not found"):
        install("codex")
