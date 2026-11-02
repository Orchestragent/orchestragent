#!/bin/sh
# orch for Linux, one-time setup inside the unpacked directory. No root, no network:
# a virtualenv from the system Python plus the bundled, hash-checked tree-sitter wheels.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"
say() { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# Prefer the system Python; an interpreter outside /usr (uv, pyenv) is bound read-only
# into the bwrap sandbox as well.
python="${PYTHON:-}"
if [ -z "$python" ]; then
  for candidate in /usr/bin/python3.13 /usr/bin/python3.12 /usr/bin/python3; do
    if [ -x "$candidate" ]; then python="$candidate"; break; fi
  done
fi
if [ -z "$python" ]; then
  say "FIX python 3.12+ not found: sudo apt install python3 python3-venv"
  exit 1
fi
if ! "$python" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
  say "FIX $python is older than 3.12"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  if ! "$python" -m venv .venv; then
    say "FIX python venv is missing: sudo apt install python3-venv, then rerun ./install.sh"
    exit 1
  fi
fi
if .venv/bin/python -m pip install --quiet --disable-pip-version-check --no-index \
    --find-links wheels --require-hashes -r wheels/requirements.txt; then
  say "  ok  tree-sitter (offline, hash-checked against uv.lock)"
else
  say "  opt tree-sitter did not install here; Python code is still fully indexed"
fi
touch .package

have git || say "  FIX git: sudo apt install git"
if have bwrap; then
  if bwrap --unshare-all --ro-bind / / true 2>/dev/null; then
    say "  ok  bwrap sandbox"
  else
    say "  FIX bwrap cannot create user namespaces (Ubuntu AppArmor); see QUICKSTART.md"
  fi
else
  say "  FIX bwrap: sudo apt install bubblewrap"
fi
if have claude || [ -x "$HOME/.local/bin/claude" ]; then
  say "  ok  claude CLI (log in once with: claude auth login)"
else
  say "  FIX claude CLI (the planner, on your subscription):"
  say "      curl -fsSL https://claude.ai/install.sh | bash && claude auth login"
fi
if have dsh; then
  say "  ok  dsh CLI found: DSF requests go through it"
else
  say "  ok  no dsh CLI: DSF calls DeepSeek's API directly with your key"
fi
say ""
say "next:"
say "  ./orch keys dsh        # DeepSeek API key + CNY budget, typed here, never echoed"
say "  ./orch keys jev        # optional: Vercel AI Gateway key for Jev"
say "  ./orch doctor          # every prerequisite, each with its fix"
say "  ./orch demo ~/orch-demo"
