#!/usr/bin/env bash
# Deploy Identika to the BY VPS behind eurasia-transline.online (/identika/).
#
# Usage:
#   export SSHPASS='<tbot password>'
#   ./scripts/deploy_vps.sh
#
# Optional: VPS_HOST, VPS_PORT, VPS_USER, REMOTE_APP, DRY_RUN=1
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
DRY_RUN="${DRY_RUN:-0}"
LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date +%F-%H%M)"

is_dry_run() {
  [[ "$DRY_RUN" == "1" || "$DRY_RUN" == "true" || "$DRY_RUN" == "yes" ]]
}

if [[ -z "${SSHPASS:-}" && ! is_dry_run ]]; then
  echo "Set SSHPASS for $VPS_USER@$VPS_HOST before running." >&2
  exit 1
fi

export SSHPASS
SSH_E=(ssh -o StrictHostKeyChecking=no -o LogLevel=ERROR -p "$VPS_PORT")
RSYNC_E=(ssh -o StrictHostKeyChecking=no -o LogLevel=ERROR -p "$VPS_PORT")

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

if ! is_dry_run; then
  require_command sshpass
  require_command rsync
fi

run_ssh() {
  if is_dry_run; then
    echo "[dry-run] ssh $VPS_USER@$VPS_HOST $*"
    return 0
  fi
  sshpass -e "${SSH_E[@]}" "$VPS_USER@$VPS_HOST" "$@"
}

run_sudo() {
  local remote_cmd="$1"
  if is_dry_run; then
    echo "[dry-run] sudo ssh $VPS_USER@$VPS_HOST $remote_cmd"
    return 0
  fi
  printf '%s\n' "$SSHPASS" | sshpass -e "${SSH_E[@]}" "$VPS_USER@$VPS_HOST" \
    "sudo -S -p '' bash -lc $(printf '%q' "$remote_cmd")"
}

run_rsync() {
  if is_dry_run; then
    echo "[dry-run] rsync ${*: -2:1} ${*: -1}"
    return 0
  fi
  sshpass -e rsync "$@"
}

echo "=== Deploy Identika → $VPS_HOST:$VPS_PORT ($STAMP) ==="

echo "→ Backup SQLite"
run_ssh "mkdir -p /home/tbot/backups && cp /home/tbot/.identika/identika.sqlite /home/tbot/backups/identika-predeploy-$STAMP.sqlite 2>/dev/null || true"

echo "→ rsync app/identika"
run_rsync -avz --delete \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
  -e "${RSYNC_E[*]}" \
  "$LOCAL_ROOT/app/identika/" \
  "$VPS_USER@$VPS_HOST:$REMOTE_APP/app/identika/"

run_rsync -avz \
  -e "${RSYNC_E[*]}" \
  "$LOCAL_ROOT/pyproject.toml" \
  "$VPS_USER@$VPS_HOST:$REMOTE_APP/pyproject.toml"

echo "→ rsync scripts/"
run_rsync -avz \
  -e "${RSYNC_E[*]}" \
  "$LOCAL_ROOT/scripts/" \
  "$VPS_USER@$VPS_HOST:$REMOTE_APP/scripts/"

echo "→ pip install + restart identika.service"
run_ssh "cd $REMOTE_APP && .venv/bin/pip install -q ."
run_sudo "systemctl restart identika"
run_ssh "sleep 3"

echo "→ nginx: unified auth + no-cache for /identika/ dynamic routes"
run_sudo "sed -i '/auth_basic \"Identika\";/d' /etc/nginx/sites-available/wb-tool 2>/dev/null || true"
run_sudo "sed -i '/auth_basic_user_file \\/etc\\/nginx\\/identika.htpasswd;/d' /etc/nginx/sites-available/wb-tool 2>/dev/null || true"
if ! is_dry_run; then
  if ! run_ssh "grep -q 'proxy_no_cache 1;' /etc/nginx/sites-available/wb-tool 2>/dev/null"; then
    run_sudo "sed -i '/location \\/identika\\/ {/a\\        proxy_no_cache 1;\\n        proxy_cache_bypass 1;' /etc/nginx/sites-available/wb-tool"
  fi
else
  run_sudo "sed -i '/location \\/identika\\/ {/a\\        proxy_no_cache 1;\\n        proxy_cache_bypass 1;' /etc/nginx/sites-available/wb-tool"
fi
run_sudo "rm -f /etc/nginx/identika.htpasswd 2>/dev/null || true"
run_sudo "nginx -t"
run_sudo "systemctl reload nginx"

echo "→ ensure IDENTIKA_UI_PASSWORD unset in app .env"
run_ssh "grep -q '^IDENTIKA_UI_PASSWORD=' $REMOTE_APP/.env 2>/dev/null && sed -i '/^IDENTIKA_UI_PASSWORD=/d' $REMOTE_APP/.env || true"

echo "→ Health check"
run_ssh "curl -sf http://127.0.0.1:8787/health && echo && wc -l $REMOTE_APP/app/identika/static/app.css && curl -sfI http://127.0.0.1:8787/identika/static/app.css | head -3"

if is_dry_run; then
  echo "✅ Dry run complete. No files were copied and no remote commands were executed."
else
  echo "✅ Deploy complete. Public URL: https://eurasia-transline.online/identika/"
fi
