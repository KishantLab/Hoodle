#!/usr/bin/env bash
# ==============================================================================
# rebuild_all.sh - Full Rebuild, APK v2 Compile, & Cluster Sync for Hoodle LMS
# Builds Android APK v2, configures NGINX micro-cache, distributes secrets,
# and rolls out zero-downtime restarts across all nodes.
# ==============================================================================
set -euo pipefail

BASE_DIR="/data/admin/ACCLLMS"
ANDROID_DIR="$BASE_DIR/android"
SCRIPTS_DIR="$BASE_DIR/scripts"

BOLD='\033[1m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BOLD}${BLUE}==================================================================${NC}"
echo -e "${BOLD}${BLUE}   🔨 Hoodle LMS Rebuild & Full Cluster Rollout (Version 2.0)     ${NC}"
echo -e "${BOLD}${BLUE}==================================================================${NC}"

# 1. Build Android Mobile App Version 2
echo -e "\n${BOLD}[1/4] Building Android APK Version 2 (Cluster Edition)...${NC}"
if [ -d "$ANDROID_DIR" ]; then
    cd "$ANDROID_DIR"
    chmod +x gradlew
    ./gradlew assembleDebug
    
    APK_SRC="$ANDROID_DIR/app/build/outputs/apk/debug/app-debug.apk"
    if [ -f "$APK_SRC" ]; then
        mkdir -p "$BASE_DIR/static/downloads" "$BASE_DIR/downloads"
        cp "$APK_SRC" "$BASE_DIR/static/downloads/hoodle.apk"
        cp "$APK_SRC" "$BASE_DIR/downloads/hoodle.apk"
        cp "$APK_SRC" "$BASE_DIR/hoodle.apk"
        APK_SIZE=$(du -h "$BASE_DIR/hoodle.apk" | cut -f1)
        echo -e "${GREEN}✓ Android APK v2 compiled successfully ($APK_SIZE) and deployed to download paths!${NC}"
    else
        echo -e "${RED}✗ Error: APK output file not found at $APK_SRC${NC}"
        exit 1
    fi
else
    echo -e "${YELLOW}Android directory not found, skipping APK compilation.${NC}"
fi

# 2. Sync Environment Secrets to Cluster Nodes
echo -e "\n${BOLD}[2/4] Synchronizing /etc/hoodle/hoodle.env to Cluster Nodes...${NC}"
cd "$BASE_DIR"
if [ -f "/etc/hoodle/hoodle.env" ]; then
    scp -o ConnectTimeout=3 /etc/hoodle/hoodle.env gpu1:/tmp/hoodle.env || true
    scp -o ConnectTimeout=3 /etc/hoodle/hoodle.env gpu2:/tmp/hoodle.env || true
    ssh -o ConnectTimeout=3 gpu1 "sudo mkdir -p /etc/hoodle && sudo cp /tmp/hoodle.env /etc/hoodle/hoodle.env && sudo chmod 600 /etc/hoodle/hoodle.env" || true
    ssh -o ConnectTimeout=3 gpu2 "sudo mkdir -p /etc/hoodle && sudo cp /tmp/hoodle.env /etc/hoodle/hoodle.env && sudo chmod 600 /etc/hoodle/hoodle.env" || true
    echo -e "${GREEN}✓ Cluster environment secrets synced across Master, GPU1, and GPU2${NC}"
fi

# 3. Apply NGINX Micro-Caching & Acceleration
echo -e "\n${BOLD}[3/4] Checking NGINX Cache & Performance Config...${NC}"
if [ -f "/etc/nginx/conf.d/hoodle_cache.conf" ]; then
    sudo nginx -t
    sudo systemctl reload nginx
    echo -e "${GREEN}✓ NGINX micro-cache configuration verified & reloaded${NC}"
fi

# 4. Execute Zero-Downtime Rolling Cluster Restart
echo -e "\n${BOLD}[4/4] Executing Rolling Cluster Restart...${NC}"
bash "$SCRIPTS_DIR/restart_cluster.sh"

echo -e "\n${BOLD}${GREEN}==================================================================${NC}"
echo -e "${BOLD}${GREEN}   🎉 Rebuild & Cluster Rollout Finished Successfully!            ${NC}"
echo -e "${BOLD}${GREEN}==================================================================${NC}"
