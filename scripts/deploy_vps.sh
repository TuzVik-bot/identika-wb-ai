#!/usr/bin/env bash
# Deploy Identika to the BY VPS behind eurasia-transline.online (/identika/).
#
# Usage:
#   export SSHPASS='<tbot password>'
#   ./scripts/deploy_vps.sh
#
# Optional: VPS_HOST, VPS_PORT, VPS_USER, REMOTE_APP
#
# Nginx notes (openresty proxy in front of Identika):
# - Dynamic routes must not be cached. The app sends Cache-Control: no-store on
#   /jobs/*, /v1/generation/jobs/*, and /health.
# - In the server block for /identika/, add:
#     proxy_no_cache 1;
#     proxy_cache_bypass 1;
# - Use a single site-wide Basic Auth (WB Tool). Do NOT set a separate
#   auth_basic "Identika" on /identika/ — remove identika.htpasswd if present.
# - Keep /identika/static/ with auth_basic off so CSS loads without challenge.
# - Do not set IDENTIKA_UI_PASSWORD in the app .env (inherit nginx auth only).

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

echo "→ nginx: unified auth + no-cache for /identika/ dynamic routes"
run_ssh "echo '$SSHPASS' | sudo -S sed -i '/auth_basic \"Identika\";/d' /etc/nginx/sites-available/wb-tool 2>/dev/null || true"
run_ssh "echo '$SSHPASS' | sudo -S sed -i '/auth_basic_user_file \\/etc\\/nginx\\/identika.htpasswd;/d' /etc/nginx/sites-available/wb-tool 2>/dev/null || true"
run_ssh "grep -q 'proxy_no_cache 1;' /etc/nginx/sites-available/wb-tool 2>/dev/null || echo '$SSHPASS' | sudo -S sed -i '/location \\/identika\\/ {/a\\        proxy_no_cache 1;\\n        proxy_cache_bypass 1;' /etc/nginx/sites-available/wb-tool"
run_ssh "echo '$SSHPASS' | sudo -S rm -f /etc/nginx/identika.htpasswd 2>/dev/null || true"
run_ssh "echo '$SSHPASS' | sudo -S nginx -t && echo '$SSHPASS' | sudo -S systemctl reload nginx"

echo "→ ensure IDENTIKA_UI_PASSWORD unset in app .env"
run_ssh "grep -q '^IDENTIKA_UI_PASSWORD=' $REMOTE_APP/.env 2>/dev/null && sed -i '/^IDENTIKA_UI_PASSWORD=/d' $REMOTE_APP/.env || true"

echo "→ Health check"
run_ssh "curl -sf http://127.0.0.1:8787/health && echo && wc -l $REMOTE_APP/app/identika/static/app.css && curl -sfI http://127.0.0.1:8787/identika/static/app.css | head -3"

echo "✅ Deploy complete. Public URL: https://eurasia-transline.online/identika/"
