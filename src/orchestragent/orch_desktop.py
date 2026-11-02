"""Desktop integration: register `orch mcp` with agent clients, notify when a run ends.

Config files are merged, never replaced: other MCP servers and settings are kept, the
previous file is backed up next to it, and nothing is written when orch is already
registered with the same command.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from orchestragent.platform import is_macos, source_root

CLIENTS = ("claude-code", "cursor", "claude-desktop")


def server_entry() -> dict[str, object]:
    """How a client should start `orch mcp` on this machine."""
    installed = shutil.which("orch")
    if installed and source_root() is None:
        return {"command": installed, "args": ["mcp"]}
    root = source_root()
    entry: dict[str, object] = {"command": sys.executable,
                                "args": ["-m", "orchestragent.orch_cli", "mcp"]}
    if root is not None:
        entry["env"] = {"PYTHONPATH": str(root / "src")}
    return entry


def config_path(client: str, home: Path | None = None) -> Path:
    home = home or Path.home()
    if client == "cursor":
        return home / ".cursor" / "mcp.json"
    if client == "claude-desktop":
        if is_macos():
            return home / "Library" / "Application Support" / "Claude" / \
                "claude_desktop_config.json"
        base = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
        return Path(base) / "Claude" / "claude_desktop_config.json"
    raise ValueError(f"{client} has no config file; use its CLI")


def merge_config(path: Path, entry: dict[str, object]) -> str:
    current: dict[str, object] = {}
    if path.exists():
        text = path.read_text()
        current = json.loads(text) if text.strip() else {}
        if not isinstance(current, dict):
            raise ValueError(f"{path} is not a JSON object; not touching it")
    servers = current.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{path}: mcpServers is not an object; not touching it")
    if servers.get("orch") == entry:
        return f"orch is already registered in {path}"
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".orch-backup"))
    servers["orch"] = entry
    current["mcpServers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n")
    return f"registered orch in {path} (restart the client to load it)"


def install(client: str, home: Path | None = None) -> str:
    if client not in CLIENTS:
        raise ValueError(f"client must be one of {', '.join(CLIENTS)}")
    entry = server_entry()
    if client == "claude-code":
        claude = shutil.which("claude")
        if claude is None:
            raise ValueError("Claude Code CLI not found on PATH (`claude`)")
        env = entry.get("env")
        args = entry["args"]
        assert isinstance(args, list)
        # The server name goes first: `--env` takes several values and would swallow it.
        command = [claude, "mcp", "add", "orch", "--scope", "user"]
        for key, value in (env.items() if isinstance(env, dict) else ()):
            command += ["--env", f"{key}={value}"]
        command += ["--", str(entry["command"]), *map(str, args)]
        done = subprocess.run(command, capture_output=True, text=True, check=False)
        output = (done.stdout + done.stderr).strip()
        if done.returncode and "already exists" not in output:
            raise ValueError(output or "claude mcp add failed")
        return output or "registered orch with Claude Code (user scope)"
    return merge_config(config_path(client, home), entry)


def notify(title: str, message: str) -> bool:
    """Best-effort desktop notification; never raises, returns whether one was sent."""
    try:
        if is_macos() and shutil.which("osascript"):
            script = (f"display notification {json.dumps(message)} "
                      f"with title {json.dumps(title)}")
            command = ["osascript", "-e", script]
        elif shutil.which("notify-send") and (os.environ.get("DISPLAY")
                                              or os.environ.get("WAYLAND_DISPLAY")):
            command = ["notify-send", title, message]
        else:
            return False
        return subprocess.run(command, capture_output=True, timeout=10,
                              check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
