#!/usr/bin/env bash
# ==============================================================================
# Lab Exam File Submission Portal - Automated Server Setup Script
# Developed by Kishan Tamboli (PhD)
# Accelerated Computing Research Lab (ACCL), Indian Institute of Technology Bhilai
# ==============================================================================
# This script performs a complete installation on a brand new Ubuntu/Debian server:
#   1. Installs all required OS packages (python3, pip, venv, sqlite3, nginx, ufw, etc.)
#   2. Prompts for configuration parameters (with intelligent defaults)
#   3. Sets up Python virtual environment and installs Flask / Werkzeug
#   4. Configures systemd service (auto-restart on crash and enable on boot)
#   5. Configures Nginx reverse proxy block with 250 MB file upload limit
#   6. Initializes SQLite database tables with administrator accounts
#   7. Verifies health and displays access links & credentials
# ==============================================================================

set -e

# Color definitions
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

echo -e "${CYAN}${BOLD}"
echo "================================================================================"
echo "    LAB EXAM SUBMISSION PORTAL - AUTOMATED SERVER SETUP"
echo "    Developed by Kishan Tamboli (PhD)"
echo "    Accelerated Computing Research Lab (ACCL), IIT Bhilai"
echo "================================================================================"
echo -e "${NC}"

# Check for root / sudo privileges
if [ "$EUID" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        echo -e "${RED}[ERROR] This setup script must be run as root or with sudo.${NC}"
        exit 1
    fi
else
    SUDO=""
fi

# Detect Server IP
DEFAULT_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$DEFAULT_IP" ]; then
    DEFAULT_IP="127.0.0.1"
fi

CURRENT_USER=$(logname 2>/dev/null || echo "$USER")
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Parse Command-line arguments if provided
NON_INTERACTIVE=false
while [[ "$#" -gt 0 ]]; do
    case $1 in
        -y|--yes|--non-interactive) NON_INTERACTIVE=true; shift ;;
        --dir) TARGET_DIR="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --user) SERVICE_USER="$2"; shift 2 ;;
        --prefix) ROUTE_PREFIX="$2"; shift 2 ;;
        *) echo "Unknown parameter: $1"; shift ;;
    esac
done

echo -e "${YELLOW}Step 1: Configuration & Environment Settings${NC}"
echo "----------------------------------------------------"

prompt_param() {
    local var_name="$1"
    local prompt_text="$2"
    local default_val="$3"
    local val=""

    if [ "$NON_INTERACTIVE" = true ]; then
        eval "$var_name=\"$default_val\""
        echo -e "  $prompt_text [default]: ${CYAN}$default_val${NC}"
    else
        read -r -p "  $prompt_text [$default_val]: " val
        val="${val:-$default_val}"
        eval "$var_name=\"$val\""
    fi
}

TARGET_DIR="${TARGET_DIR:-$SCRIPT_DIR}"
prompt_param TARGET_DIR "Installation directory path" "$TARGET_DIR"
prompt_param SERVICE_USER "System user to run the service" "$CURRENT_USER"
prompt_param PORT "Internal application port" "8090"
prompt_param SERVER_IP "Server Hostname / IP address" "$DEFAULT_IP"
prompt_param ROUTE_PREFIX "Nginx URL prefix path (e.g. /lab_exam/ or /)" "/lab_exam/"
prompt_param ADMIN_PASS "Initial Admin Password (username: admin)" "admin@accl"

SUBMISSIONS_DIR="$TARGET_DIR/submissions"
VENV_DIR="$TARGET_DIR/venv"
DB_PATH="$TARGET_DIR/submissions.db"
SECRET_KEY=$(head -c 32 /dev/urandom | xxd -p 2>/dev/null || echo "accl_lab_exam_portal_key_$(date +%s)")

echo ""
echo -e "${GREEN}Configuration Summary:${NC}"
echo "  - Install Path:      $TARGET_DIR"
echo "  - Service User:      $SERVICE_USER"
echo "  - Service Port:      $PORT"
echo "  - Submissions Dir:   $SUBMISSIONS_DIR"
echo "  - Server IP:         $SERVER_IP"
echo "  - Nginx URL Route:   http://${SERVER_IP}${ROUTE_PREFIX}"
echo "  - Admin Account:     admin / $ADMIN_PASS"
echo ""

if [ "$NON_INTERACTIVE" != true ]; then
    read -r -p "Proceed with server installation? (Y/n): " confirm
    if [[ "$confirm" =~ ^[Nn] ]]; then
        echo "Installation aborted by user."
        exit 0
    fi
fi

echo ""
echo -e "${YELLOW}Step 2: Installing Required System Packages (APT)${NC}"
echo "----------------------------------------------------"
$SUDO apt-get update -y
$SUDO apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    sqlite3 \
    nginx \
    curl \
    rsync \
    git \
    ufw \
    xxd

echo -e "${GREEN}✓ System packages installed successfully.${NC}"

echo ""
echo -e "${YELLOW}Step 3: Creating Directory Structure & Setting Permissions${NC}"
echo "----------------------------------------------------"
$SUDO mkdir -p "$TARGET_DIR"
$SUDO mkdir -p "$SUBMISSIONS_DIR"
$SUDO mkdir -p "$TARGET_DIR/static/uploads"
$SUDO mkdir -p "$TARGET_DIR/static/images"
$SUDO mkdir -p "$TARGET_DIR/templates"

# If current directory is different from TARGET_DIR, copy application files
if [ "$SCRIPT_DIR" != "$TARGET_DIR" ]; then
    echo "Copying portal files into $TARGET_DIR..."
    $SUDO cp -ru "$SCRIPT_DIR/app.py" "$TARGET_DIR/"
    $SUDO cp -ru "$SCRIPT_DIR/requirements.txt" "$TARGET_DIR/" 2>/dev/null || true
    $SUDO cp -ru "$SCRIPT_DIR/static" "$TARGET_DIR/"
    $SUDO cp -ru "$SCRIPT_DIR/templates" "$TARGET_DIR/"
fi

# Ensure correct ownership
$SUDO chown -R "$SERVICE_USER":"$SERVICE_USER" "$TARGET_DIR"
echo -e "${GREEN}✓ Directory structure created and ownership configured for '$SERVICE_USER'.${NC}"

echo ""
echo -e "${YELLOW}Step 4: Setting Up Python Virtual Environment${NC}"
echo "----------------------------------------------------"
if [ ! -d "$VENV_DIR" ]; then
    $SUDO -u "$SERVICE_USER" python3 -m venv "$VENV_DIR"
fi

$SUDO -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --upgrade pip
$SUDO -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install Flask>=3.0.0 Werkzeug>=3.0.0
echo -e "${GREEN}✓ Python virtual environment created with Flask & Werkzeug installed.${NC}"

echo ""
echo -e "${YELLOW}Step 5: Initializing Database & Administrator Account${NC}"
echo "----------------------------------------------------"
export SUBMISSION_DIR="$SUBMISSIONS_DIR"
export DB_PATH="$DB_PATH"
export PORT="$PORT"
export HOST="0.0.0.0"
export SECRET_KEY="$SECRET_KEY"

$SUDO -u "$SERVICE_USER" "$VENV_DIR/bin/python3" -c "
from app import init_db, get_db
from werkzeug.security import generate_password_hash
init_db()
conn = get_db()
cursor = conn.cursor()
pwd_hash = generate_password_hash('$ADMIN_PASS')
cursor.execute(\"UPDATE users SET password_hash = ? WHERE username = 'admin'\", (pwd_hash,))
conn.commit()
conn.close()
print('✓ Database initialized with admin password.')
"
echo -e "${GREEN}✓ Database schema migration & admin seed completed.${NC}"

echo ""
echo -e "${YELLOW}Step 6: Configuring Systemd Service (lab-exam.service)${NC}"
echo "----------------------------------------------------"
SERVICE_FILE="/etc/systemd/system/lab-exam.service"
$SUDO bash -c "cat << EOF > $SERVICE_FILE
[Unit]
Description=Lab Exam File Submission Portal
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$TARGET_DIR
ExecStart=$VENV_DIR/bin/python3 $TARGET_DIR/app.py
Restart=always
RestartSec=3
Environment=PORT=$PORT
Environment=HOST=0.0.0.0
Environment=SUBMISSION_DIR=$SUBMISSIONS_DIR
Environment=DB_PATH=$DB_PATH
Environment=SECRET_KEY=$SECRET_KEY

[Install]
WantedBy=multi-user.target
EOF"

$SUDO systemctl daemon-reload
$SUDO systemctl enable lab-exam.service
$SUDO systemctl restart lab-exam.service
echo -e "${GREEN}✓ Systemd service configured, enabled on boot, and started.${NC}"

echo ""
echo -e "${YELLOW}Step 7: Configuring Nginx Reverse Proxy${NC}"
echo "----------------------------------------------------"
# Create Nginx proxy location snippet
NGINX_SNIPPET="/etc/nginx/snippets/lab-exam.conf"
$SUDO mkdir -p /etc/nginx/snippets

CLEAN_PREFIX="${ROUTE_PREFIX%/}"
if [ -z "$CLEAN_PREFIX" ] || [ "$CLEAN_PREFIX" = "/" ]; then
    NGINX_BLOCK="
    location / {
        proxy_pass http://127.0.0.1:$PORT/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        client_max_body_size 250M;
        proxy_read_timeout 120s;
    }
    "
else
    NGINX_BLOCK="
    location = $CLEAN_PREFIX {
        return 301 $CLEAN_PREFIX/;
    }

    location $CLEAN_PREFIX/ {
        proxy_pass http://127.0.0.1:$PORT/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        client_max_body_size 250M;
        proxy_read_timeout 120s;
    }
    "
fi

$SUDO bash -c "cat << EOF > $NGINX_SNIPPET
# Accelerated Computing Research Lab (ACCL) Lab Exam Portal
$NGINX_BLOCK
EOF"

# Check if default site exists
DEFAULT_SITE="/etc/nginx/sites-available/default"
if [ -f "$DEFAULT_SITE" ]; then
    if ! grep -q "lab-exam.conf" "$DEFAULT_SITE"; then
        # Insert snippet include before closing brace of server block
        $SUDO sed -i "/server_name/a \    include /etc/nginx/snippets/lab-exam.conf;" "$DEFAULT_SITE"
    fi
fi

if $SUDO nginx -t >/dev/null 2>&1; then
    $SUDO systemctl reload nginx
    echo -e "${GREEN}✓ Nginx configuration tested and reloaded successfully.${NC}"
else
    echo -e "${YELLOW}[!] Nginx test had warnings or conflicts. Check $NGINX_SNIPPET and /etc/nginx/sites-enabled/default.${NC}"
fi

echo ""
echo -e "${YELLOW}Step 8: Configuring Firewall (UFW)${NC}"
echo "----------------------------------------------------"
if command -v ufw >/dev/null 2>&1; then
    $SUDO ufw allow 'Nginx Full' >/dev/null 2>&1 || true
    $SUDO ufw allow 80/tcp >/dev/null 2>&1 || true
    $SUDO ufw allow 443/tcp >/dev/null 2>&1 || true
    $SUDO ufw allow 22/tcp >/dev/null 2>&1 || true
    echo -e "${GREEN}✓ Firewall rules updated for HTTP, HTTPS, and SSH.${NC}"
fi

echo ""
echo -e "${YELLOW}Step 9: Health Verification & Service Status${NC}"
echo "----------------------------------------------------"
sleep 2

STATUS_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/login" || echo "000")
if [ "$STATUS_CODE" = "200" ]; then
    echo -e "${GREEN}✓ Direct App Port ($PORT): HTTP 200 OK${NC}"
else
    echo -e "${YELLOW}[!] Direct App Port ($PORT) returned status: $STATUS_CODE${NC}"
fi

NGINX_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1${ROUTE_PREFIX}login" || echo "000")
if [ "$NGINX_CODE" = "200" ]; then
    echo -e "${GREEN}✓ Nginx Reverse Proxy ($ROUTE_PREFIX): HTTP 200 OK${NC}"
else
    echo -e "${YELLOW}[!] Nginx Reverse Proxy ($ROUTE_PREFIX) returned status: $NGINX_CODE${NC}"
fi

echo ""
echo -e "${CYAN}${BOLD}"
echo "================================================================================"
echo "    INSTALLATION COMPLETED SUCCESSFULLY! "
echo "================================================================================"
echo -e "${NC}"
echo -e "Access URLs:"
echo -e "  - ${BOLD}Instructor & Admin Login:${NC} http://${SERVER_IP}${ROUTE_PREFIX}login"
echo -e "  - ${BOLD}Direct App (Port):${NC}        http://${SERVER_IP}:${PORT}/login"
echo ""
echo -e "Default Admin Credentials:"
echo -e "  - ${BOLD}Username:${NC} admin"
echo -e "  - ${BOLD}Password:${NC} $ADMIN_PASS"
echo -e "  ${YELLOW}(Please change your password immediately after logging in!)${NC}"
echo ""
echo -e "Useful Commands:"
echo -e "  - Check Service:  ${CYAN}sudo systemctl status lab-exam.service${NC}"
echo -e "  - Restart App:    ${CYAN}sudo systemctl restart lab-exam.service${NC}"
echo -e "  - View Logs:      ${CYAN}journalctl -u lab-exam.service -f${NC}"
echo -e "  - Reload Nginx:   ${CYAN}sudo systemctl reload nginx${NC}"
echo ""
echo -e "${CYAN}Developed by Kishan Tamboli (PhD) • Accelerated Computing Research Lab (ACCL), IIT Bhilai${NC}"
echo "================================================================================"
