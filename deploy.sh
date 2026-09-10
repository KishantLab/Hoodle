#!/usr/bin/env bash
# Deployment script for ACCLLMS on 10.10.14.104
set -euo pipefail

TARGET_DIR="/data/admin/ACCLLMS"
SERVICE_FILE="/etc/systemd/system/accl-lms.service"
NGINX_CONF="/etc/nginx/sites-available/attendance"

echo "======================================================"
echo "🚀 Deploying ACCLLMS on $(hostname -I | awk '{print $1}')"
echo "======================================================"

# Ensure target directory structure exists
sudo mkdir -p "$TARGET_DIR/storage/lockers" \
              "$TARGET_DIR/storage/submissions" \
              "$TARGET_DIR/storage/attachments" \
              "$TARGET_DIR/static/css" \
              "$TARGET_DIR/static/js" \
              "$TARGET_DIR/static/images" \
              "$TARGET_DIR/templates"

# Set permissions
sudo chown -R kishan:kishan "$TARGET_DIR"

# Copy systemd service file
echo "📋 Installing systemd service..."
sudo cp "$TARGET_DIR/accl-lms.service" "$SERVICE_FILE"
sudo systemctl daemon-reload

# Check and update Nginx configuration safely
echo "🌐 Configuring Nginx reverse proxy for /lms/..."
if ! grep -q "location /lms/" "$NGINX_CONF"; then
    sudo python3 - << 'PYEOF'
with open("/etc/nginx/sites-available/attendance", "r") as f:
    content = f.read()

lms_block = """    # ACCL Learning Management System (Port 8095)
    location = /lms {
        return 301 /lms/;
    }

    location /lms/ {
        proxy_pass http://127.0.0.1:8095/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Prefix /lms;
        client_max_body_size 300M;
        proxy_read_timeout 120s;
    }

"""

if "location / {" in content:
    idx = content.find("    # Default attendance proxy")
    if idx == -1:
        idx = content.find("    location / {")
    content = content[:idx] + lms_block + content[idx:]
    with open("/etc/nginx/sites-available/attendance", "w") as f:
        f.write(content)
    print("Added /lms/ block to /etc/nginx/sites-available/attendance")
else:
    print("Warning: location / not found in nginx config")
PYEOF
    sudo nginx -t
    sudo systemctl reload nginx
    echo "✅ Nginx reloaded successfully!"
else
    echo "ℹ️ /lms/ location block already present in Nginx configuration."
fi

# Enable and restart service
echo "🔄 Starting accl-lms.service..."
sudo systemctl enable accl-lms.service
sudo systemctl restart accl-lms.service

sleep 2

# Verification
echo "🔍 Checking service status..."
if systemctl is-active --quiet accl-lms.service; then
    echo "✅ accl-lms.service is ACTIVE and RUNNING!"
else
    echo "❌ Error: accl-lms.service failed to start. Logs:"
    journalctl -u accl-lms.service -n 20 --no-pager
    exit 1
fi

echo "======================================================"
echo "🎉 ACCLLMS deployment complete!"
echo "Direct Access: http://10.10.14.104:8095/"
echo "Nginx Proxy:   http://10.10.14.104/lms/"
echo "======================================================"
