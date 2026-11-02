#!/usr/bin/env bash
# Installed as og-restart. Never restart unless the service has frozen admission.
set -euo pipefail
API="${API:-http://127.0.0.1:8765}"
WAIT="${WAIT:-900}"
[[ "$WAIT" =~ ^[0-9]+$ ]] || { echo "WAIT must be seconds" >&2; exit 1; }
exec 9>"${TMPDIR:-/tmp}/orchestragent-restart-${UID}.lock"
flock -n 9 || { echo "another restart is in progress" >&2; exit 1; }
requested=0
release_gate() {
  if [[ "$requested" == 1 ]]; then
    curl -fsS --max-time 15 -o /dev/null -X POST "$API/maintenance/drain" \
      -H 'Content-Type: application/json' -d '{"enabled":false}' || true
  fi
}
trap release_gate EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

deadline=$((SECONDS + WAIT))
while true; do
  requested=1
  code=$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' \
    -X POST "$API/maintenance/drain" -H 'Content-Type: application/json' \
    -d '{"enabled":true}') || { echo "cannot verify idle state; refusing to restart" >&2; exit 1; }
  case "$code" in
    200) break ;;
    409) ;;
    *) echo "restart gate unavailable (HTTP $code); refusing to restart" >&2; exit 1 ;;
  esac
  if (( SECONDS >= deadline )); then
    echo "still busy after ${WAIT}s; refusing to restart" >&2
    exit 1
  fi
  remaining=$((deadline - SECONDS))
  sleep "$((remaining < 10 ? remaining : 10))"
done
sudo systemctl restart orchestragent
requested=0
sleep 3
curl -fsS --max-time 15 "$API/health"
echo
