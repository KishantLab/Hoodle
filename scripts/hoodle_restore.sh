#!/usr/bin/env bash
# ==============================================================================
# Hoodle LMS - Full Disaster Recovery Restoration System
# Accelerated Computing Research Lab (ACCL) • IIT Bhilai
# Author: Kishan Tamboli (PhD)
# ==============================================================================
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_DIR="${APP_DIR}/backups"
LOG_FILE="${BACKUP_DIR}/restore.log"
CONFIG_ENV="${BACKUP_DIR}/backup_config.env"

# Source persistent admin configuration if available
if [ -f "${CONFIG_ENV}" ]; then
    # shellcheck disable=SC1090
    source "${CONFIG_ENV}"
fi

REMOTE_HOST="${REMOTE_HOST:-gpu2}"
REMOTE_USER="${REMOTE_USER:-kishan}"
REMOTE_DIR="${REMOTE_DIR:-/data2/kishan/hoodle_backups}"

mkdir -p "${BACKUP_DIR}"

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg"
    echo "$msg" >> "${LOG_FILE}" 2>/dev/null || true
}

log "======================================================================"
log "🔄 Hoodle LMS Disaster Recovery Restoration Process"
log "Application Root: ${APP_DIR}"
log "======================================================================"

TARGET_ARCHIVE="$1"

if [ -z "${TARGET_ARCHIVE}" ] || [ "${TARGET_ARCHIVE}" == "--from-remote" ] || [ "${TARGET_ARCHIVE}" == "--from-gpu2" ] || [ "${TARGET_ARCHIVE}" == "--latest" ]; then
    log "📥 Fetching latest verified backup from Remote Server: ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/hoodle_backup_latest.tar.gz..."
    LOCAL_FETCHED="${BACKUP_DIR}/hoodle_backup_latest_from_remote.tar.gz"
    
    if rsync -avz -e "ssh -o BatchMode=yes -o ConnectTimeout=15" \
        "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/hoodle_backup_latest.tar.gz" \
        "${LOCAL_FETCHED}"; then
        log "✅ Downloaded latest backup archive from remote server (${REMOTE_HOST})."
        TARGET_ARCHIVE="${LOCAL_FETCHED}"
    else
        log "⚠️ Rsync failed. Attempting fallback scp from ${REMOTE_HOST}..."
        if scp -o BatchMode=yes -o ConnectTimeout=15 \
            "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/hoodle_backup_latest.tar.gz" \
            "${LOCAL_FETCHED}"; then
            log "✅ Downloaded latest backup archive from ${REMOTE_HOST} via scp."
            TARGET_ARCHIVE="${LOCAL_FETCHED}"
        elif [ -f "${BACKUP_DIR}/hoodle_backup_latest.tar.gz" ]; then
            log "⚠️ Could not reach ${REMOTE_HOST} directly. Falling back to local latest backup: ${BACKUP_DIR}/hoodle_backup_latest.tar.gz"
            TARGET_ARCHIVE="${BACKUP_DIR}/hoodle_backup_latest.tar.gz"
        else
            log "❌ ERROR: No remote or local backup archive found to restore!"
            exit 1
        fi
    fi
fi

if [ ! -f "${TARGET_ARCHIVE}" ]; then
    log "❌ ERROR: Target backup file not found: ${TARGET_ARCHIVE}"
    exit 1
fi

log "🔍 Verifying backup archive integrity: ${TARGET_ARCHIVE}..."
if ! tar -tzf "${TARGET_ARCHIVE}" > /dev/null 2>&1; then
    log "❌ ERROR: Archive is corrupted or not a valid tar.gz file!"
    exit 1
fi
log "✅ Archive integrity verified."

# 1. Take Pre-Restore Safety Snapshot of current state if exists
PRE_RESTORE_DIR="${BACKUP_DIR}/pre_restore_safety_$(date +'%Y%m%d_%H%M%S')"
mkdir -p "${PRE_RESTORE_DIR}"
if [ -f "${APP_DIR}/accl_lms.db" ]; then
    log "🛡️ Creating safety rollback snapshot of existing database in ${PRE_RESTORE_DIR}..."
    cp "${APP_DIR}/accl_lms.db" "${PRE_RESTORE_DIR}/accl_lms.db.bak"
fi

# 2. Stop systemd service if running to release database locks
SERVICE_STOPPED=false
if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet accl-lms.service 2>/dev/null; then
        log "⏹️ Stopping accl-lms.service during restoration..."
        if sudo systemctl stop accl-lms.service 2>/dev/null || systemctl stop accl-lms.service 2>/dev/null; then
            SERVICE_STOPPED=true
            log "   Service stopped successfully."
        fi
    fi
fi

# 3. Unpack Archive into Staging Directory
STAGING_DIR=$(mktemp -d -t hoodle_restore_staging_XXXXXX)
cleanup() {
    if [ -d "${STAGING_DIR}" ]; then
        rm -rf "${STAGING_DIR}"
    fi
}
trap cleanup EXIT

log "📦 Extracting backup archive into staging..."
tar -xzf "${TARGET_ARCHIVE}" -C "${STAGING_DIR}"

# 4. Verify Restored Database
if [ ! -f "${STAGING_DIR}/accl_lms.db" ]; then
    log "❌ ERROR: No accl_lms.db found in backup archive!"
    exit 1
fi

log "🔍 Running SQLite PRAGMA integrity check on restored database..."
INTEGRITY=$(python3 - "${STAGING_DIR}/accl_lms.db" << 'EOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
res = conn.execute("PRAGMA integrity_check;").fetchone()[0]
conn.close()
print(res)
EOF
)

if [ "${INTEGRITY}" != "ok" ]; then
    log "❌ ERROR: Restored database failed integrity check: ${INTEGRITY}!"
    exit 1
fi
log "✅ Restored database integrity is 100% OK."

# 5. Restore Database & Storage
log "🚚 Restoring accl_lms.db to application root..."
cp -f "${STAGING_DIR}/accl_lms.db" "${APP_DIR}/accl_lms.db"
chmod 600 "${APP_DIR}/accl_lms.db"

if [ -d "${STAGING_DIR}/storage" ]; then
    log "🚚 Restoring storage directories (submissions, lockers, attachments)..."
    mkdir -p "${APP_DIR}/storage"
    cp -rf "${STAGING_DIR}/storage"/* "${APP_DIR}/storage/" 2>/dev/null || true
    chmod -R 755 "${APP_DIR}/storage"
fi

if [ -d "${STAGING_DIR}/static/uploads" ]; then
    log "🚚 Restoring uploaded course media..."
    mkdir -p "${APP_DIR}/static/uploads"
    cp -rf "${STAGING_DIR}/static/uploads"/* "${APP_DIR}/static/uploads/" 2>/dev/null || true
fi

if [ -f "${STAGING_DIR}/.env" ] && [ ! -f "${APP_DIR}/.env" ]; then
    log "🔐 Restoring environment configuration (.env)..."
    cp "${STAGING_DIR}/.env" "${APP_DIR}/.env"
    chmod 600 "${APP_DIR}/.env"
fi

# 6. Restart Service if it was running
if [ "${SERVICE_STOPPED}" = true ]; then
    log "▶️ Restarting accl-lms.service..."
    sudo systemctl start accl-lms.service 2>/dev/null || systemctl start accl-lms.service 2>/dev/null || true
    sleep 2
    if systemctl is-active --quiet accl-lms.service 2>/dev/null; then
        log "✅ accl-lms.service is ACTIVE and running!"
    else
        log "⚠️ Notice: Check service status with 'systemctl status accl-lms.service'."
    fi
fi

log "======================================================================"
log "🎉 DISASTER RECOVERY RESTORATION COMPLETE!"
log "All databases, student lockers, coursework submissions, and files restored."
log "======================================================================"
