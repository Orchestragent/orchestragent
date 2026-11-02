#!/bin/sh
# Install orch on macOS or Linux:
#   curl -fsSL https://raw.githubusercontent.com/Orchestragent/orchestragent/main/install.sh | sh
# Options (after `sh -s --` when piped): --dry-run  print the steps, change nothing
#                                         --yes      no questions (skip key entry)
# ORCH_SOURCE overrides what is installed (a local checkout path or a pip requirement).
set -eu

DRY=0
YES=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --yes) YES=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
OS="${ORCH_FAKE_OS:-$(uname -s)}"
SOURCE="${ORCH_SOURCE:-orchestragent[index] @ git+https://github.com/Orchestragent/orchestragent}"

say() { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
run() {
  if [ "$DRY" = 1 ]; then say "  would run: $*"; else "$@"; fi
}
ask() {  # ask "question" -> 0 for yes
  [ "$YES" = 1 ] && return 1
  (true < /dev/tty) 2>/dev/null || return 1  # no terminal to ask (CI, piped without tty)
  printf '%s [y/N] ' "$1"
  read -r answer < /dev/tty || return 1
  case "$answer" in y|Y|yes) return 0 ;; *) return 1 ;; esac
}

case "$OS" in
  Darwin) say "orch installer for macOS" ;;
  Linux) say "orch installer for Linux" ;;
  *) say "orch supports macOS and Linux (found $OS)"; exit 1 ;;
esac

# 1. Prerequisites the installer cannot provide itself.
missing=0
if ! have git; then
  missing=1
  if [ "$OS" = Darwin ]; then say "  FIX git: xcode-select --install"
  else say "  FIX git: sudo apt install git   (or your distribution's package)"; fi
fi
if [ "$OS" = Darwin ]; then
  if [ -x /usr/bin/sandbox-exec ]; then say "  ok  sandbox: macOS Seatbelt (built in)"
  else missing=1; say "  FIX /usr/bin/sandbox-exec is missing; orch needs it to run tests safely"; fi
else
  if ! have bwrap; then
    missing=1; say "  FIX sandbox: sudo apt install bubblewrap"
  elif ! bwrap --unshare-all --ro-bind / / true 2>/dev/null; then
    say "  FIX bwrap cannot create user namespaces (Ubuntu 24.04 AppArmor). One fix:"
    say "      sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
    missing=1
  else say "  ok  sandbox: bubblewrap"; fi
fi
[ "$missing" = 1 ] && [ "$DRY" = 0 ] && { say "Fix the items above, then rerun the installer."; exit 1; }

# 2. uv, then orch as an isolated tool (its own Python 3.12+, nothing system-wide).
if have uv; then
  say "  ok  uv $(uv --version 2>/dev/null | cut -d' ' -f2)"
else
  say "  ..  installing uv (https://astral.sh/uv)"
  if [ "$DRY" = 1 ]; then say "  would run: curl -LsSf https://astral.sh/uv/install.sh | sh"
  else curl -LsSf https://astral.sh/uv/install.sh | sh; fi
  PATH="$HOME/.local/bin:$PATH"; export PATH
fi
say "  ..  installing orch"
run uv tool install --force --python 3.12 "$SOURCE"
PATH="$HOME/.local/bin:$PATH"; export PATH

# 3. Models: Claude Code plans, DeepSeek designs tests and builds.
if have claude; then
  say "  ok  Claude Code CLI (planner): claude auth login, or ANTHROPIC_API_KEY"
else
  say "  ..  Claude Code CLI not found. It is the verified planner (or pick another"
  say "      model per role: orch models / orch providers). To install it:"
  say "      curl -fsSL https://claude.ai/install.sh | bash"
  say "      then: claude auth login (subscription) or export ANTHROPIC_API_KEY=..."
fi
if ask "Store your DeepSeek API key for orch now?"; then
  run orch keys dsh
else
  say "  ..  later: orch keys dsh   (DeepSeek key, kept in the macOS Keychain / a 0600 file)"
fi

# 4. Let the agent apps on this machine hand builds to orch (MCP).
if have claude; then run orch mcp --install claude-code || true; fi
if [ "$OS" = Darwin ] && [ -d "/Applications/Claude.app" ]; then
  run orch mcp --install claude-desktop || true
fi
if [ -d "$HOME/.cursor" ]; then run orch mcp --install cursor || true; fi
if have codex; then run orch mcp --install codex || true; fi
if have gemini || [ -d "$HOME/.gemini" ]; then run orch mcp --install gemini || true; fi
if have opencode; then run orch mcp --install opencode || true; fi
if [ -d "$HOME/.codeium/windsurf" ]; then run orch mcp --install windsurf || true; fi
if have code; then run orch mcp --install vscode || true; fi

# 5. macOS: a double-click dashboard in ~/Applications.
if [ "$OS" = Darwin ] && ask "Add Orch.app to ~/Applications (choose a project, see it live)?"; then
  run orch app install
fi

# 6. Final check.
say ""
if [ "$DRY" = 1 ]; then say "  would run: orch doctor"; else orch doctor || true; fi
say ""
say "Next: cd into a git repository, write spec.md (numbered requirements), then"
say "      orch plan spec.md && orch run --notify      # watch it live: orch watch"
