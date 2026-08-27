#!/bin/bash
DATE=$(date +%Y%m%d_%H%M)
BACKUP_DIR="/opt/stairs-web-app/backups"
APP_DIR="/opt/stairs-web-app"
mkdir -p "$BACKUP_DIR"
sqlite3 "$APP_DIR/stairs_state.db" ".backup '$BACKUP_DIR/stairs_state_$DATE.db'"
tar czf "$BACKUP_DIR/app_backup_$DATE.tar.gz" -C "$APP_DIR" app.py models_v2.py templates/ static/ 2>/dev/null
find "$BACKUP_DIR" -name "*.db" -mtime +7 -delete
find "$BACKUP_DIR" -name "*.tar.gz" -mtime +7 -delete
echo "[$(date)] Backup done: $DATE"
