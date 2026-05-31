#!/usr/bin/env bash
# Deploy Identika to the BY VPS behind eurasia-transline.online (/identika/).
#
# Usage:
#   export SSHPASS='<tbot password>'
#   ./scripts/deploy_vps.sh
#
# Optional: VPS_HOST, VPS_PORT, VPS_USER, REMOTE_APP

set -euo pipefail

VPS_HOST="${VPS_HOST:-213.184.248.155}"
VPS_PORT="${VPS_PORT:-23023}"
VPS_USER="${VPS_USER:-tbot}"
REMOTE_APP="${REMOTE_APP:-/home/tbot/identika}"
LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date +%F-%H%M)"

if [[ -z "${SSHPASS:-}" ]]; then
  echo "Set SSHPASS for $VPS_USER@$VPS_HOST before running." >&2
  exit 1
fi

export SSHPASS
SSH_E=(ssh -o StrictHostKeyChecking=no -o LogLevel=ERROR -p "$VPS_PORT")
RSYNC_E=(ssh -o StrictHostKeyChecking=no -o LogLevel=ERROR -p "$VPS_PORT")

run_ssh() {
  sshpass -e "${SSH_E[@]}" "$VPS_USER@$VPS_HOST" "$@"
}

echo "=== Deploy Identika → $VPS_HOST:$VPS_PORT ($STAMP) ==="

echo "→ Backup SQLite"
run_ssh "mkdir -p /home/tbot/backups && cp /home/tbot/.identika/identika.sqlite /home/tbot/backups/identika-predeploy-$STAMP.sqlite 2>/dev/null || true"

echo "→ rsync app/identika"
sshpass -e rsync -avz --delete \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
  -e "${RSYNC_E[*]}" \
  "$LOCAL_ROOT/app/identika/" \
  "$VPS_USER@$VPS_HOST:$REMOTE_APP/app/identika/"

sshpass -e rsync -avz \
  -e "${RSYNC_E[*]}" \
  "$LOCAL_ROOT/pyproject.toml" \
  "$VPS_USER@$VPS_HOST:$REMOTE_APP/pyproject.toml"

echo "→ pip install + restart identika.service"
run_ssh "cd $REMOTE_APP && .venv/bin/pip install -q . && echo '$SSHPASS' | sudo -S systemctl restart identika && sleep 3"

echo "→ Health check"
run_ssh "curl -sf http://127.0.0.1:8787/health && echo && wc -l $REMOTE_APP/app/identika/static/app.css"

echo "✅ Deploy complete. Public URL: https://eurasia-transline.online/identika/"
