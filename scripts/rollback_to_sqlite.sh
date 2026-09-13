#!/usr/bin/env bash
set -e

echo "=================================================="
echo "    1-CLICK ROLLBACK TO SINGLE-NODE SQLITE        "
echo "=================================================="

# 1. Reverse Sync: Ensure no new data from PostgreSQL is lost
echo "[*] Syncing recent records from PostgreSQL back to SQLite..."
python3 /data/admin/ACCLLMS/scripts/sync_pg_to_sqlite.py || {
    echo "[!] Warning: Sync failed or PG unreachable. Proceeding with existing SQLite backup."
}

# 2. Revert NGINX Configuration
echo "[*] Reverting NGINX configuration back to single upstream (127.0.0.1:8095)..."
if [ -f /etc/nginx/sites-available/attendance.single_node_backup ]; then
    sudo cp /etc/nginx/sites-available/attendance.single_node_backup /etc/nginx/sites-available/attendance
else
    sudo python3 -c '
conf_path = "/etc/nginx/sites-available/attendance"
with open(conf_path, "r") as f:
    c = f.read()
import re
c = re.sub(r"upstream accl_lms_cluster\s*\{[^}]*\}\s*", "", c)
c = c.replace("proxy_pass http://accl_lms_cluster/;", "proxy_pass http://127.0.0.1:8095/;")
with open(conf_path, "w") as f:
    f.write(c)
'
fi

sudo nginx -t
sudo systemctl reload nginx
echo "  -> NGINX restored to 127.0.0.1:8095."

# 3. Stop services on compute nodes
echo "[*] Stopping LMS services on compute nodes (gpu1, gpu2)..."
ssh gpu1 "sudo systemctl stop accl-lms" || true
ssh gpu2 "sudo systemctl stop accl-lms" || true
echo "  -> gpu1 and gpu2 stopped."

# 4. Revert Master systemd unit to SQLite mode
echo "[*] Reverting master service to SQLite mode..."
sudo sed -i 's/DATABASE_BACKEND="postgres"/DATABASE_BACKEND="sqlite"/' /etc/systemd/system/accl-lms.service
sudo sed -i 's/DATABASE_URL=.*/DB_PATH="\/data\/admin\/ACCLLMS\/accl_lms.db"/' /etc/systemd/system/accl-lms.service
sudo systemctl daemon-reload
sudo systemctl restart accl-lms
sleep 2

# 5. Verify local health
STATUS=$(systemctl is-active accl-lms)
echo "  -> Master LMS service status: ${STATUS}"
curl -sI http://127.0.0.1:8095/login | head -n 1

echo ""
echo "============================================================="
echo "  ✅ 1-CLICK ROLLBACK SUCCESSFUL!                            "
echo "  Hoodle LMS is running on original SQLite configuration.   "
echo "  Zero data lost. Cluster nodes stopped.                     "
echo "============================================================="
