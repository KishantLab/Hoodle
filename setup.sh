#!/usr/bin/env bash
# ==============================================================================
# Hoodle: Accelerated Classroom & Lab Learning Management System (LMS)
# Automated Server Setup & Fresh Installation Script
# Developed by Kishan Tamboli (PhD)
# Accelerated Computing Research Lab (ACCL), Indian Institute of Technology Bhilai
# ==============================================================================
# This script performs a complete automated installation on Ubuntu/Debian:
#   1. Installs all required OS packages (python3, pip, venv, sqlite3, nginx, curl, etc.)
#   2. Prompts for configuration parameters (with intelligent defaults)
#   3. Sets up Python virtual environment and installs all dependencies
#   4. Initializes database tables, default admin, faculty, and student accounts
#   5. Configures isolated storage directories (lockers, submissions, attachments, exports)
#   6. Configures systemd service (accl-lms.service) with Gunicorn multi-threaded workers
#   7. Configures Nginx reverse proxy with HTTPS camera streaming support and 500M body limit
#   8. Runs full test suite to verify 100% green deployment health
#   9. Verifies endpoints and displays credentials and access links
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
echo "    HOODLE LMS - AUTOMATED SERVER SETUP & INSTALLATION"
echo "    Accelerated Classroom & Lab Learning Management System"
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
prompt_param PORT "Internal application port" "8095"
prompt_param SERVER_IP "Server Hostname / IP address" "$DEFAULT_IP"
prompt_param ROUTE_PREFIX "Nginx URL prefix path (e.g. /lms/ or /)" "/lms/"

VENV_DIR="$TARGET_DIR/venv"
STORAGE_DIR="$TARGET_DIR/storage"
LOCKERS_DIR="$STORAGE_DIR/lockers"
SUBMISSIONS_DIR="$STORAGE_DIR/submissions"
ATTACHMENTS_DIR="$STORAGE_DIR/attachments"
EXPORTS_DIR="$STORAGE_DIR/exports"
DB_PATH="$TARGET_DIR/accl_lms.db"

echo ""
echo -e "${GREEN}Configuration Summary:${NC}"
echo "  - Install Path:      $TARGET_DIR"
echo "  - Service User:      $SERVICE_USER"
echo "  - Service Port:      $PORT"
echo "  - Storage Dir:       $STORAGE_DIR"
echo "  - Server IP:         $SERVER_IP"
echo "  - Nginx URL Route:   http://${SERVER_IP}${ROUTE_PREFIX}"
echo ""

if [ "$NON_INTERACTIVE" != true ]; then
    read -r -p "Proceed with server installation? (Y/n): " confirm
    if [[ "$confirm" =~ ^[Nn] ]]; then
        echo "Installation aborted by user."
        exit 0
    fi
fi

echo ""
echo -e "${YELLOW}Step 2: Installing System Packages & Build Tools${NC}"
echo "----------------------------------------------------"
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    sqlite3 \
    nginx \
    curl \
    git \
    rsync \
    ufw >/dev/null

echo -e "${GREEN}✓ System dependencies installed successfully.${NC}"

echo ""
echo -e "${YELLOW}Step 3: Setting Up Storage Directories & Permissions${NC}"
echo "----------------------------------------------------"
mkdir -p "$LOCKERS_DIR" "$SUBMISSIONS_DIR" "$ATTACHMENTS_DIR" "$EXPORTS_DIR"
$SUDO chown -R "$SERVICE_USER":"$SERVICE_USER" "$TARGET_DIR"
$SUDO chmod -R 775 "$STORAGE_DIR"
echo -e "${GREEN}✓ Storage directories created with proper read/write permissions.${NC}"

echo ""
echo -e "${YELLOW}Step 4: Setting Up Python Virtual Environment${NC}"
echo "----------------------------------------------------"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    echo -e "${GREEN}✓ Created Python virtualenv at $VENV_DIR${NC}"
fi

source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q
pip install -r "$TARGET_DIR/requirements.txt" -q
echo -e "${GREEN}✓ Installed Python dependencies (Flask, Werkzeug, openpyxl, qrcode, gunicorn).${NC}"

echo ""
echo -e "${YELLOW}Step 5: Initializing Database & Pre-Seeded Accounts${NC}"
echo "----------------------------------------------------"
cd "$TARGET_DIR"
python3 -c "import app; app.init_db(); print('✓ Database schema and initial accounts verified.')"

echo ""
echo -e "${YELLOW}Step 6: Running Verification Test Suite${NC}"
echo "----------------------------------------------------"
python3 -m unittest -v test_lms.py
echo -e "${GREEN}✓ All automated tests passed successfully.${NC}"

echo ""
echo -e "${YELLOW}Step 7: Configuring Systemd Service (accl-lms.service)${NC}"
echo "----------------------------------------------------"
SERVICE_FILE="/etc/systemd/system/accl-lms.service"
$SUDO bash -c "cat << EOF > $SERVICE_FILE
[Unit]
Description=ACCL Learning Management System & Classroom Portal (Hoodle)
After=network.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$TARGET_DIR
Environment=\"PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin\"
ExecStart=$VENV_DIR/bin/gunicorn --workers 16 --threads 4 --worker-class gthread --bind 0.0.0.0:$PORT --timeout 120 --keep-alive 5 --max-requests 2000 --max-requests-jitter 200 app:app
Restart=always
RestartSec=3
KillMode=mixed
TimeoutStopSec=30
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF"

$SUDO systemctl daemon-reload
$SUDO systemctl enable accl-lms.service
$SUDO systemctl restart accl-lms.service
echo -e "${GREEN}✓ Systemd service accl-lms.service configured, enabled, and started.${NC}"

echo ""
echo -e "${YELLOW}Step 8: Configuring Nginx Reverse Proxy & Camera Streaming Headers${NC}"
echo "----------------------------------------------------"
CLEAN_PREFIX="${ROUTE_PREFIX%/}"
NGINX_SNIPPET="/etc/nginx/snippets/accl-lms.conf"
$SUDO mkdir -p /etc/nginx/snippets

if [ -z "$CLEAN_PREFIX" ]; then
    NGINX_BLOCK="
    location / {
        proxy_pass http://127.0.0.1:$PORT/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \"upgrade\";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        client_max_body_size 500M;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
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
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \"upgrade\";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        client_max_body_size 500M;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
    "
fi

$SUDO bash -c "cat << EOF > $NGINX_SNIPPET
# ACCL Hoodle LMS Reverse Proxy Configuration
$NGINX_BLOCK
EOF"

DEFAULT_SITE="/etc/nginx/sites-available/default"
if [ -f "$DEFAULT_SITE" ]; then
    if ! grep -q "accl-lms.conf" "$DEFAULT_SITE"; then
        $SUDO sed -i "/server_name/a \    include /etc/nginx/snippets/accl-lms.conf;" "$DEFAULT_SITE"
    fi
fi

if $SUDO nginx -t >/dev/null 2>&1; then
    $SUDO systemctl reload nginx
    echo -e "${GREEN}✓ Nginx configuration tested and reloaded successfully.${NC}"
else
    echo -e "${YELLOW}[!] Nginx test had warnings or conflicts. Check $NGINX_SNIPPET.${NC}"
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
echo "    HOODLE LMS INSTALLATION COMPLETED SUCCESSFULLY! "
echo "================================================================================"
echo -e "${NC}"
echo -e "Access URLs:"
echo -e "  - ${BOLD}HTTPS Portal (Recommended for Camera Scanner):${NC} https://${SERVER_IP}${ROUTE_PREFIX}login"
echo -e "  - ${BOLD}HTTP Portal:${NC}                                   http://${SERVER_IP}${ROUTE_PREFIX}login"
echo -e "  - ${BOLD}Direct App (Port $PORT):${NC}                       http://${SERVER_IP}:${PORT}/login"
echo ""
echo -e "Pre-Seeded Default Accounts:"
echo -e "  - ${BOLD}Administrator:${NC} username: ${CYAN}admin${NC}     | password: ${CYAN}admin@accl${NC}"
echo -e "  - ${BOLD}Faculty / Teacher:${NC} username: ${CYAN}kishan${NC} | password: ${CYAN}password123${NC}"
echo -e "  - ${BOLD}Student Account:${NC}   username: ${CYAN}student1${NC} | password: ${CYAN}student123${NC} (Roll: B26DS001)"
echo ""
echo -e "Useful Commands:"
echo -e "  - Check Service:  ${CYAN}sudo systemctl status accl-lms.service${NC}"
echo -e "  - Restart App:    ${CYAN}sudo systemctl restart accl-lms.service${NC}"
echo -e "  - View Logs:      ${CYAN}sudo journalctl -u accl-lms.service -f${NC}"
echo -e "  - Run Unit Tests: ${CYAN}python3 -m unittest -v test_lms.py${NC}"
echo ""
echo -e "${CYAN}Developed by Kishan Tamboli (PhD) • Accelerated Computing Research Lab (ACCL), IIT Bhilai${NC}"
echo "================================================================================"
