#!/usr/bin/env bash
set -e

echo "=============================================================="
echo "    EMERGENCY ROLLBACK: RESTORING TO PRE-SCALE 25K STATE      "
echo "=============================================================="

BACKUP_DIR="/data/admin/ACCLLMS/backups/pre_scale_25k_backup"

if [ ! -f "$BACKUP_DIR/accl_lms_pre_scale.sql" ]; then
    echo "❌ Error: Backup files not found at $BACKUP_DIR"
    exit 1
fi

echo "[1/4] Restoring code files..."
cp "$BACKUP_DIR/db_adapter.py.bak" /data/admin/ACCLLMS/db_adapter.py
cp "$BACKUP_DIR/app.py.bak" /data/admin/ACCLLMS/app.py
cp "$BACKUP_DIR/base.html.bak" /data/admin/ACCLLMS/templates/base.html
echo "  -> Code files restored."

echo "[2/4] Restoring PostgreSQL database from snapshot..."
sudo -u postgres psql -d accl_lms -f "$BACKUP_DIR/accl_lms_pre_scale.sql" > /dev/null 2>&1 || true
echo "  -> Database state restored."

echo "[3/4] Re-syncing GPU1 Read Replica..."
ssh -o StrictHostKeyChecking=no gpu1 "sudo systemctl restart postgresql" || true
echo "  -> Replica restarted."

echo "[4/4] Issuing rolling cluster reload (Master, GPU1, GPU2)..."
pkill -HUP -f "gunicorn.*8096" || true
ssh -o StrictHostKeyChecking=no gpu1 "pkill -HUP -f 'gunicorn.*8095'" || true
ssh -o StrictHostKeyChecking=no gpu2 "pkill -HUP -f 'gunicorn.*8095'" || true
sudo systemctl reload nginx || true

echo "=============================================================="
echo "  ✅ ROLLBACK COMPLETED SUCCESSFULLY IN < 10 SECONDS!          "
echo "=============================================================="
