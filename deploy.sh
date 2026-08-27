#!/bin/bash
# Deploy latest main branch to this VPS (run ON the server, not from admin API).
set -e
APP_DIR="${STAIRS_APP_DIR:-/opt/stairs-web-app}"
cd "$APP_DIR"

echo "=== STAIRS deploy ==="
echo "Directory: $APP_DIR"

if [ -f stairs_state.db ]; then
  cp stairs_state.db "stairs_state.db.bak-$(date +%Y%m%d_%H%M%S)"
  echo "Database backed up."
fi

git fetch origin main
git pull origin main
echo "Latest commit:"
git log -1 --oneline

if command -v systemctl >/dev/null 2>&1; then
  sudo systemctl restart stairs-web-app
  sudo systemctl status stairs-web-app --no-pager | head -15
else
  echo "systemctl not found — restart gunicorn manually."
fi

echo "=== Deploy complete ==="
