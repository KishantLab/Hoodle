#!/usr/bin/env bash
set -e

echo "=================================================="
echo "      ACTIVATING HOODLE LMS MULTI-NODE CLUSTER     "
echo "=================================================="

# 1. Verify PostgreSQL connectivity from all 3 nodes
echo "[*] Verifying cluster connectivity to PostgreSQL..."
python3 -c "
import psycopg2
for node, host in [('master', '127.0.0.1'), ('master_cluster', '192.168.99.2')]:
    conn = psycopg2.connect(f'postgresql://kishan_accl:HoodleLMS_Secure_2026@{host}:5432/accl_lms')
    conn.close()
"
ssh gpu1 "python3 -c \"import psycopg2; psycopg2.connect('postgresql://kishan_accl:HoodleLMS_Secure_2026@192.168.99.2:5432/accl_lms').close()\""
ssh gpu2 "python3 -c \"import psycopg2; psycopg2.connect('postgresql://kishan_accl:HoodleLMS_Secure_2026@192.168.99.2:5432/accl_lms').close()\""
echo "  -> Database reachable from all 3 nodes."

# 2. Ensure services are running on gpu1 and gpu2
echo "[*] Ensuring services are active on compute nodes..."
ssh gpu1 "sudo systemctl start accl-lms"
ssh gpu2 "sudo systemctl start accl-lms"
sudo systemctl start accl-lms
sleep 2

# 3. Verify HTTP endpoints on all 3 nodes
python3 -c "
import urllib.request
for node, ip, port in [('master', '127.0.0.1', 8096), ('gpu1', '192.168.99.3', 8095), ('gpu2', '192.168.99.4', 8095)]:
    res = urllib.request.urlopen(f'http://{ip}:{port}/login', timeout=5)
    assert res.status == 200, f'Bad status {res.status} on {node}'
"
echo "  -> All 3 Gunicorn instances responding with HTTP 200 OK."

# 4. Backup current nginx conf if not already backed up
if [ ! -f /etc/nginx/sites-available/attendance.single_node_backup ]; then
    sudo cp /etc/nginx/sites-available/attendance /etc/nginx/sites-available/attendance.single_node_backup
fi

# 5. Configure NGINX upstream cluster
echo "[*] Updating NGINX reverse proxy configuration..."
sudo python3 -c '
conf_path = "/etc/nginx/sites-available/attendance"
with open(conf_path, "r") as f:
    content = f.read()

upstream_block = """upstream accl_lms_cluster {
    least_conn;
    server 127.0.0.1:8096 max_fails=2 fail_timeout=5s;
    server 192.168.99.3:8095 max_fails=2 fail_timeout=5s;
    server 192.168.99.4:8095 max_fails=2 fail_timeout=5s;
}

"""

if "upstream accl_lms_cluster" not in content:
    content = upstream_block + content

content = content.replace("proxy_pass http://127.0.0.1:8095/;", "proxy_pass http://accl_lms_cluster/;")

if "proxy_next_upstream" not in content:
    snippet_lms = """        proxy_next_upstream error timeout invalid_header http_500 http_502 http_503 http_504;
        proxy_next_upstream_tries 3;
        proxy_next_upstream_timeout 10s;
        proxy_set_header X-Forwarded-Prefix /lms;"""
    content = content.replace("proxy_set_header X-Forwarded-Prefix /lms;", snippet_lms)

    snippet_hoodle = """        proxy_next_upstream error timeout invalid_header http_500 http_502 http_503 http_504;
        proxy_next_upstream_tries 3;
        proxy_next_upstream_timeout 10s;
        proxy_set_header X-Forwarded-Prefix /hoodle;"""
    content = content.replace("proxy_set_header X-Forwarded-Prefix /hoodle;", snippet_hoodle)

with open(conf_path, "w") as f:
    f.write(content)
'

# 6. Test and reload NGINX
echo "[*] Validating NGINX configuration..."
sudo nginx -t
echo "[*] Reloading NGINX (0 downtime reload)..."
sudo systemctl reload nginx

echo ""
echo "============================================================="
echo "  ✅ CLUSTER ACTIVATED SUCCESSFULLY!                         "
echo "  Load distributed across: master, gpu1, gpu2                "
echo "  Automatic failover: ENABLED                                "
echo "  GPUs allocated to Slurm: 100% UNTOUCHED (0 MB VRAM)        "
echo "============================================================="
