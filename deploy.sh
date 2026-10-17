#!/usr/bin/env bash
# ==============================================================================
# deploy.sh - Deployment & Cluster Synchronization for Hoodle LMS (v2.0)
# ==============================================================================
set -euo pipefail

TARGET_DIR="/data/admin/ACCLLMS"
SCRIPTS_DIR="$TARGET_DIR/scripts"

echo "======================================================"
echo "🚀 Deploying Hoodle LMS on $(hostname -I | awk '{print $1}')"
echo "======================================================"

# Ensure directories exist
mkdir -p "$TARGET_DIR/storage/lockers" \
         "$TARGET_DIR/storage/submissions" \
         "$TARGET_DIR/storage/attachments" \
         "$TARGET_DIR/static/downloads" \
         "$TARGET_DIR/downloads"

# Trigger full cluster rollout
if [ -f "$SCRIPTS_DIR/restart_cluster.sh" ]; then
    bash "$SCRIPTS_DIR/restart_cluster.sh"
else
    sudo systemctl restart accl-lms.service
fi
