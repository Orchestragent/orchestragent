"""Desktop integration: register `orch mcp` with agent clients, notify when a run ends.

Config files are merged, never replaced: other MCP servers and settings are kept, the
previous file is backed up next to it, and nothing is written when orch is already
registered with the same command.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from orchestragent.platform import is_macos, source_root

CLIENTS = ("claude-code", "codex", "gemini", "cursor", "claude-desktop", "vscode", "windsurf",
           "opencode")
# `orch_wait` may block up to 600 s; hosts with short default tool timeouts get a longer one.
TOOL_TIMEOUT_S = 660


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


def _config_home(home: Path) -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or str(home / ".config"))


def config_path(client: str, home: Path | None = None) -> Path:
    home = home or Path.home()
    if client == "cursor":
        return home / ".cursor" / "mcp.json"
    if client == "gemini":
        return home / ".gemini" / "settings.json"
    if client == "windsurf":
        return home / ".codeium" / "windsurf" / "mcp_config.json"
    if client == "opencode":
        return _config_home(home) / "opencode" / "opencode.json"
    if client == "vscode":
        if is_macos():
            return home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
        return _config_home(home) / "Code" / "User" / "mcp.json"
    if client == "claude-desktop":
        if is_macos():
            return home / "Library" / "Application Support" / "Claude" / \
                "claude_desktop_config.json"
        return _config_home(home) / "Claude" / "claude_desktop_config.json"
    raise ValueError(f"{client} has no config file; use its CLI")


def client_entry(client: str, entry: dict[str, object]) -> tuple[str, dict[str, object]]:
    """The servers key and the entry in the shape this client's config file expects."""
    args = entry["args"]
    assert isinstance(args, list)
    env = entry.get("env")
    if client == "vscode":
        shaped: dict[str, object] = {"type": "stdio", "command": entry["command"], "args": args}
        if env:
            shaped["env"] = env
        return "servers", shaped
    if client == "opencode":
        shaped = {"type": "local", "command": [entry["command"], *args], "enabled": True,
                  "timeout": TOOL_TIMEOUT_S * 1000}
        if env:
            shaped["environment"] = env
        return "mcp", shaped
    if client == "gemini":
        shaped = {**entry, "timeout": TOOL_TIMEOUT_S * 1000}
        path = env.get("PYTHONPATH") if isinstance(env, dict) else None
        if path:  # Gemini CLI refuses PYTHONPATH overrides: put the source on sys.path itself
            boot = (f"import sys, runpy; sys.path.insert(0, {path!r}); "
                    "runpy.run_module('orchestragent.orch_cli', run_name='__main__')")
            shaped["args"] = ["-c", boot, *args[2:]]  # args = ["-m", module, "mcp"]
            del shaped["env"]
        return "mcpServers", shaped
    return "mcpServers", dict(entry)


def merge_config(path: Path, entry: dict[str, object], key: str = "mcpServers") -> str:
    current: dict[str, object] = {}
    if path.exists():
        text = path.read_text()
        current = json.loads(text) if text.strip() else {}
        if not isinstance(current, dict):
            raise ValueError(f"{path} is not a JSON object; not touching it")
    servers = current.get(key, {})
    if not isinstance(servers, dict):
        raise ValueError(f"{path}: {key} is not an object; not touching it")
    if servers.get("orch") == entry:
        return f"orch is already registered in {path}"
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".orch-backup"))
    servers["orch"] = entry
    current[key] = servers
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
    if client == "codex":
        return _install_codex(entry)
    key, shaped = client_entry(client, entry)
    return merge_config(config_path(client, home), shaped, key)


def _install_codex(entry: dict[str, object]) -> str:
    """`codex mcp add` writes ~/.codex/config.toml; then give orch_wait room to block."""
    codex = shutil.which("codex")
    if codex is None:
        raise ValueError("Codex CLI not found on PATH (`codex`)")
    args = entry["args"]
    assert isinstance(args, list)
    env = entry.get("env")
    command = [codex, "mcp", "add", "orch"]
    for key, value in (env.items() if isinstance(env, dict) else ()):
        command += ["--env", f"{key}={value}"]
    command += ["--", str(entry["command"]), *map(str, args)]
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    output = "\n".join(line for line in (done.stdout + done.stderr).splitlines()
                       if not line.startswith("WARNING")).strip()
    if done.returncode:
        raise ValueError(output or "codex mcp add failed")
    config = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    if config.is_file():
        text = config.read_text()
        header = "[mcp_servers.orch]\n"
        start = text.find(header)
        section = text[start:].split("\n[", 1)[0] if start >= 0 else ""
        if start >= 0 and "tool_timeout_sec" not in section:
            at = start + len(header)
            config.write_text(text[:at] + f"tool_timeout_sec = {TOOL_TIMEOUT_S}\n" + text[at:])
    return output or "registered orch with Codex"


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


APP_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Orch</string>
  <key>CFBundleDisplayName</key><string>Orch</string>
  <key>CFBundleIdentifier</key><string>dev.orchestragent.orch</string>
  <key>CFBundleExecutable</key><string>Orch</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>{version}</string>
  <key>CFBundleVersion</key><string>{version}</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
"""

# The launcher: choose a project, (re)start its live dashboard, open it in the browser.
APP_LAUNCHER = r"""#!/bin/sh
# Orch.app: watch an orch project live. Generated by `orch app install`.
set -u
run_orch() {{ {orch} "$@"; }}
STATE="$HOME/Library/Application Support/orch/app"
mkdir -p "$STATE"
say() {{ [ "${{ORCH_APP_DRY:-0}}" = 1 ] && echo "$*"; }}
if [ -n "${{ORCH_APP_REPO:-}}" ]; then
  REPO="$ORCH_APP_REPO"
else
  REPO=$(osascript \
    -e 'set ask to "Choose a project (a git repository) to watch with orch:"' \
    -e 'POSIX path of (choose folder with prompt ask)' \
    2>/dev/null) || exit 0
fi
REPO="${{REPO%/}}"
if [ -f "$STATE/server.pid" ]; then  # one dashboard at a time: stop the previous one
  kill "$(cat "$STATE/server.pid")" 2>/dev/null || true
fi
: > "$STATE/server.log"
(trap '' HUP; run_orch --repo "$REPO" watch --web --port 0) > "$STATE/server.log" 2>&1 &
echo $! > "$STATE/server.pid"
URL=""
for _ in $(seq 1 50); do
  URL=$(sed -n 's/^orch watch: serving \(http[^ ]*\).*/\1/p' "$STATE/server.log" | head -n 1)
  [ -n "$URL" ] && break
  sleep 0.2
done
if [ -z "$URL" ]; then
  osascript -e 'display alert "Orch could not start the dashboard"' >/dev/null 2>&1
  echo "see $STATE/server.log" >&2
  say "error: no dashboard URL"; exit 1
fi
say "dashboard: $URL for $REPO"
if [ "${{ORCH_APP_DRY:-0}}" != 1 ]; then
  open "$URL"
  osascript -e 'on run argv' \
    -e 'display notification ("Watching " & item 1 of argv) with title "Orch"' \
    -e 'end run' "$(basename "$REPO")" >/dev/null 2>&1
fi
"""


def app_command() -> str:
    """The orch command line the app runs (installed `orch`, or this source checkout)."""
    entry = server_entry()
    args = entry["args"]
    assert isinstance(args, list)
    parts = [str(entry["command"]), *[str(a) for a in args][:-1]]  # drop "mcp"
    command = " ".join(shlex.quote(part) for part in parts)
    env = entry.get("env")
    if isinstance(env, dict) and env:
        command = "env " + " ".join(shlex.quote(f"{k}={v}") for k, v in env.items()) + " " \
            + command
    return command


def install_app(dest: Path, version: str = "0.1.0") -> str:
    """Write Orch.app (Info.plist + launcher). No compilation; nothing leaves the machine."""
    if dest.suffix != ".app":
        raise ValueError("the destination must end in .app, e.g. ~/Applications/Orch.app")
    if dest.exists() and not (dest / "Contents" / "Info.plist").is_file():
        raise ValueError(f"{dest} exists and is not an app bundle; not replacing it")
    macos = dest / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    (dest / "Contents" / "Info.plist").write_text(APP_PLIST.format(version=version))
    launcher = macos / "Orch"
    launcher.write_text(APP_LAUNCHER.format(orch=app_command()))
    launcher.chmod(0o755)
    return (f"installed {dest}: open it, choose a project, and its live dashboard opens in "
            "your browser (run again to switch projects)")
