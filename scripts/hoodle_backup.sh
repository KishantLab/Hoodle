#!/usr/bin/env bash
# ==============================================================================
# Hoodle LMS - Automated Offsite Disaster Recovery Backup System
# Accelerated Computing Research Lab (ACCL) • IIT Bhilai
# Author: Kishan Tamboli (PhD)
# ==============================================================================
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_DIR="${APP_DIR}/backups"
LOG_FILE="${BACKUP_DIR}/backup.log"
CONFIG_ENV="${BACKUP_DIR}/backup_config.env"

# Source persistent admin configuration if available
if [ -f "${CONFIG_ENV}" ]; then
    # shellcheck disable=SC1090
    source "${CONFIG_ENV}"
fi

REMOTE_HOST="${REMOTE_HOST:-gpu2}"
REMOTE_USER="${REMOTE_USER:-kishan}"
REMOTE_DIR="${REMOTE_DIR:-/data2/kishan/hoodle_backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
ARCHIVE_NAME="hoodle_backup_${TIMESTAMP}.tar.gz"
ARCHIVE_PATH="${BACKUP_DIR}/${ARCHIVE_NAME}"
LATEST_PATH="${BACKUP_DIR}/hoodle_backup_latest.tar.gz"

mkdir -p "${BACKUP_DIR}"

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg"
    echo "$msg" >> "${LOG_FILE}" 2>/dev/null || true
}

log "======================================================================"
log "🚀 Starting Hoodle LMS Full Offsite Backup: ${TIMESTAMP}"
log "Application Root: ${APP_DIR}"
log "Offsite Target:   ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}"
log "======================================================================"

# 1. Verify Database Exists
DB_PATH="${APP_DIR}/accl_lms.db"
if [ ! -f "${DB_PATH}" ]; then
    log "❌ ERROR: Database file not found at ${DB_PATH}!"
    exit 1
fi

# 2. Create Staging Directory
STAGING_DIR=$(mktemp -d -t hoodle_backup_staging_XXXXXX)
cleanup() {
    if [ -d "${STAGING_DIR}" ]; then
        rm -rf "${STAGING_DIR}"
    fi
}
trap cleanup EXIT

log "📁 Created temporary staging directory: ${STAGING_DIR}"

# 3. Hot SQLite Backup (Safe against live write locks & WAL transactions)
log "📦 Taking hot SQLite snapshot of accl_lms.db..."
python3 - "${DB_PATH}" "${STAGING_DIR}/accl_lms.db" << 'EOF'
import sqlite3
import sys

src_path = sys.argv[1]
dst_path = sys.argv[2]

try:
    src_conn = sqlite3.connect(src_path, timeout=30.0)
    dst_conn = sqlite3.connect(dst_path)
    src_conn.backup(dst_conn)
    dst_conn.close()
    src_conn.close()
    print("SUCCESS: Hot SQLite backup completed.")
except Exception as e:
    print(f"ERROR: SQLite backup failed: {e}", file=sys.stderr)
    sys.exit(1)
EOF

# 4. Verify SQLite Snapshot Integrity & Record Counts
log "🔍 Verifying SQLite snapshot integrity..."
MANIFEST_DATA=$(python3 - "${STAGING_DIR}/accl_lms.db" << 'EOF'
import sqlite3
import hashlib
import json
import sys

db_path = sys.argv[1]
conn = sqlite3.connect(db_path)
check = conn.execute("PRAGMA integrity_check;").fetchone()[0]
if check != "ok":
    print(f"CORRUPT: Integrity check returned: {check}", file=sys.stderr)
    sys.exit(1)

tables = [
    "users", "courses", "course_enrollments", "coursework",
    "submissions", "student_locker_files", "announcements",
    "comments", "attendance_sessions", "attendance_logs",
    "course_grading_categories", "course_invitations"
]
counts = {}
for t in tables:
    try:
        cnt = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        counts[t] = cnt
    except Exception:
        counts[t] = 0
conn.close()

with open(db_path, "rb") as f:
    sha256 = hashlib.sha256(f.read()).hexdigest()

print(json.dumps({"status": "ok", "sha256": sha256, "counts": counts}))
EOF
)

log "📊 DB Snapshot Verified: ${MANIFEST_DATA}"

# 5. Copy Storage (Lockers, Submissions, Attachments)
if [ -d "${APP_DIR}/storage" ]; then
    log "💾 Backing up all storage directories (lockers, submissions, attachments)..."
    mkdir -p "${STAGING_DIR}/storage"
    cp -r "${APP_DIR}/storage"/* "${STAGING_DIR}/storage/" 2>/dev/null || true
    STORAGE_SIZE=$(du -sh "${STAGING_DIR}/storage" 2>/dev/null | cut -f1)
    log "   Storage directory size: ${STORAGE_SIZE}"
else
    log "⚠️ Storage directory does not exist, creating empty placeholder."
    mkdir -p "${STAGING_DIR}/storage"
fi

# 6. Copy Uploads (Course Banners & Assets)
if [ -d "${APP_DIR}/static/uploads" ]; then
    mkdir -p "${STAGING_DIR}/static"
    cp -r "${APP_DIR}/static/uploads" "${STAGING_DIR}/static/" 2>/dev/null || true
fi

# 7. Copy Environment File (.env) if present
if [ -f "${APP_DIR}/.env" ]; then
    log "🔐 Backing up .env configuration..."
    cp "${APP_DIR}/.env" "${STAGING_DIR}/.env"
    chmod 600 "${STAGING_DIR}/.env"
fi

# 8. Write Manifest Metadata
cat << EOF > "${STAGING_DIR}/manifest.json"
{
  "system": "Hoodle LMS",
  "backup_timestamp": "${TIMESTAMP}",
  "db_integrity": "OK",
  "db_manifest": ${MANIFEST_DATA},
  "created_at_iso": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}
EOF

# 9. Build Compressed Tar.gz Archive
log "📦 Compressing full backup bundle into ${ARCHIVE_NAME}..."
tar -czf "${ARCHIVE_PATH}" -C "${STAGING_DIR}" .

# Verify archive integrity
tar -tzf "${ARCHIVE_PATH}" > /dev/null
ARCHIVE_SIZE=$(du -sh "${ARCHIVE_PATH}" | cut -f1)
log "✅ Archive successfully created and verified! Total Size: ${ARCHIVE_SIZE}"

# Update Latest Copy
cp -f "${ARCHIVE_PATH}" "${LATEST_PATH}"

# 10. Sync Offsite to Remote Server
log "🌐 Syncing backup offsite to ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/..."
if rsync -avz -e "ssh -o BatchMode=yes -o ConnectTimeout=15" \
    "${ARCHIVE_PATH}" "${LATEST_PATH}" \
    "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"; then
    log "✅ Offsite synchronization to ${REMOTE_HOST} succeeded!"
else
    log "⚠️ Warning: Rsync failed. Attempting fallback scp..."
    scp -o BatchMode=yes -o ConnectTimeout=15 "${ARCHIVE_PATH}" "${LATEST_PATH}" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/" || {
        log "❌ Remote backup sync to ${REMOTE_HOST} failed. Check network or SSH keys."
    }
fi

# 11. Retention & Pruning (> retention days)
log "🧹 Pruning local backups older than ${RETENTION_DAYS} days..."
find "${BACKUP_DIR}" -maxdepth 1 -name "hoodle_backup_*.tar.gz" -mtime "+${RETENTION_DAYS}" -exec rm -f {} + || true

log "🧹 Pruning remote ${REMOTE_HOST} backups older than ${RETENTION_DAYS} days..."
ssh -o BatchMode=yes -o ConnectTimeout=15 "${REMOTE_USER}@${REMOTE_HOST}" \
    "find ${REMOTE_DIR} -maxdepth 1 -name 'hoodle_backup_*.tar.gz' -mtime +${RETENTION_DAYS} -delete" 2>/dev/null || true

log "🎉 Hoodle LMS Backup completed successfully at $(date '+%Y-%m-%d %H:%M:%S')!"
log "======================================================================"
