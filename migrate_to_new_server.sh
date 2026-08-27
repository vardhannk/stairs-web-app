#!/bin/bash
# ============================================================
# STAIRS — Export everything from current server
# Run this on your CURRENT server before migrating
# ============================================================

set -e
DATE=$(date +%Y%m%d_%H%M)
EXPORT_FILE="/tmp/stairs_export_$DATE.tar.gz"
APP_DIR="/opt/stairs-web-app"

echo "Creating full export..."

tar czf "$EXPORT_FILE" \
    -C "$APP_DIR" \
    app.py \
    templates/ \
    static/ \
    wsgi.py \
    gunicorn.conf.py \
    requirements.txt \
    stairs_state.db \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='backups' \
    --exclude='*.pyc'

# Export .env separately (sensitive)
cp "$APP_DIR/.env" "/tmp/stairs_env_$DATE.env"

echo ""
echo "Export complete!"
echo ""
echo "  App export: $EXPORT_FILE"
echo "  Env file:   /tmp/stairs_env_$DATE.env"
echo ""
echo "  Download both files to your local machine:"
echo "  scp sravani@152.42.171.198:$EXPORT_FILE ~/Downloads/"
echo "  scp sravani@152.42.171.198:/tmp/stairs_env_$DATE.env ~/Downloads/"
echo ""
echo "  On new server:"
echo "  1. Copy files: scp ~/Downloads/stairs_export_*.tar.gz newserver:/tmp/stairs_restore.tar.gz"
echo "  2. Run: sudo DOMAIN=stairstomillionaire.com bash install.sh"
echo "  3. Copy .env: scp ~/Downloads/stairs_env_*.env newserver:/opt/stairs-web-app/.env"
echo "  4. Restart: sudo systemctl restart stairs-web-app"
