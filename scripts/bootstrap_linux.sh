#!/usr/bin/env bash
# Prepare an Ubuntu 24.04 host to run Orchestragent.
# Idempotent: safe to re-run. Does not log in to any provider and does not open
# any public port; the service binds loopback and is reached through Tailscale.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/orchestragent}"
DATA_DIR="${DATA_DIR:-/var/lib/orchestragent}"
SERVICE_USER="${SERVICE_USER:-orchestra}"
WITH_CLIS="${WITH_CLIS:-1}"
WITH_TAILSCALE="${WITH_TAILSCALE:-1}"

say() { printf '\n== %s\n' "$*"; }

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root: sudo bash $0" >&2
  exit 1
fi

say "checking base system"
. /etc/os-release
if [[ "${VERSION_ID:-}" != "24.04" ]]; then
  echo "tested on Ubuntu 24.04; found ${PRETTY_NAME:-unknown}" >&2
fi
python3 --version
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  git bubblewrap python3 python3-venv curl ca-certificates rsync jq >/dev/null

say "creating service user and directories"
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP_DIR" "$DATA_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR/logs"

if [[ "$WITH_CLIS" == "1" ]]; then
  say "installing Node.js 22 and provider CLIs"
  if ! command -v node >/dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs >/dev/null
  fi
  npm install -g --silent @anthropic-ai/claude-code @openai/codex @moonshot-ai/kimi-code >/dev/null
  # Cursor ships an official Linux tarball via its installer script.
  if ! command -v cursor-agent >/dev/null; then
    ( curl -fsS https://cursor.com/install | bash ) >/dev/null 2>&1 || echo "cursor installer failed; install manually"
  fi
  command -v claude codex kimi cursor-agent 2>/dev/null || true
fi

if [[ "$WITH_TAILSCALE" == "1" ]] && ! command -v tailscale >/dev/null; then
  say "installing Tailscale (run 'tailscale up' yourself afterwards)"
  curl -fsSL https://tailscale.com/install.sh | sh >/dev/null
fi

say "installing systemd unit"
cat >/etc/systemd/system/orchestragent.service <<UNIT
[Unit]
Description=Orchestragent coordinator (loopback only)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONPATH=$APP_DIR/src
Environment=ORCHESTRA_WORKERS=3
Environment=ORCHESTRA_NATIVE_TIMEOUT=0
Environment=ORCHESTRA_KEYCHAIN=0
ExecStart=$APP_DIR/.venv/bin/python -m orchestragent.server --live --remote-port 8766
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
UNIT

say "done"
cat <<NEXT

Next steps:
  1. copy the repository:   rsync -a --exclude .data --exclude .venv ./ $SERVICE_USER@host:$APP_DIR/
  2. create the venv:       sudo -u $SERVICE_USER python3 -m venv $APP_DIR/.venv
  3. copy mutable state:    rsync -a .data/ $SERVICE_USER@host:$DATA_DIR/.data/
  4. tailscale up --ssh     (authenticate once, in your browser)
  5. sudo systemctl enable --now orchestragent
  6. tailscale serve --bg --https=8443 http://127.0.0.1:8766
  7. log in to each provider: claude / codex / kimi / cursor-agent login
NEXT
