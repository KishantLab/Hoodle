# Exam Submission Portal

Developed by **Kishan Tamboli (PhD)**  
**Accelerated Computing Research Lab (ACCL)**  
Indian Institute of Technology Bhilai (IIT Bhilai)  

[![Python](https://img.shields.io/badge/Python-3.8%2B-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.0%2B-lightgrey.svg)](https://flask.palletsprojects.com/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![GitHub](https://img.shields.io/badge/GitHub-KishantLab%2FExam__Submission__Portal-black.svg)](https://github.com/KishantLab/Exam_Submission_Portal)

---

## 📖 Overview

The **Exam Submission Portal** is an enterprise-grade, high-concurrency web application tailored for university laboratory examinations. Engineered for zero-friction student access, data integrity, and instructor oversight, the platform eliminates the need for student credentials while maintaining strict course confidentiality, robust verification hashes, real-time live monitoring, and automated deadline enforcement.

---

## ✨ Key Features & Functionalities

### 1. Student Submission Experience
- **Direct Submission (No Login Required)**: Students open a clean, short URL (e.g. `http://<server-ip>/lab_exam/csl100`) and immediately upload their exam archives.
- **Automatic Roll Number Standardization**: Roll numbers are automatically converted to uppercase alphanumeric characters on the client and server (e.g., `b26ds003` $\rightarrow$ `B26DS003`).
- **Flexible File Formats**: Configurable per exam—accept `.zip` archives, specific extensions, or any file format up to 200 MB.
- **Submission Policies**:
  - *Multiple Submissions Mode*: Keeps only the single latest file (`<ROLL_NUMBER>.zip`) directly in the course folder; older attempts are cleanly overwritten.
  - *Single Submission Mode*: Prevents accidental re-uploads once a valid file has been submitted.
- **Tamper-Evident Digital Receipt**: Upon successful upload, students receive an instant receipt detailing roll number, filename, filesize, upload timestamp, lab venue, timing status, and an official **SHA-256 cryptographic verification checksum**.

### 2. Exam Scheduling & Post-Deadline Policies
- **Exam Window Control**: Set optional **Start Time** and **End Time / Deadline** (`datetime-local`).
- **Pre-Exam Holding Screen**: If a student accesses the portal before the scheduled start time, the upload form is locked with an informational countdown banner.
- **Configurable Post-Deadline Handling**:
  - **Allow Late Submissions**: Submissions remain open past the deadline. The system automatically calculates and records the delay (e.g., `12 min late`, `1h 5m late`). Both student receipts and teacher dashboards display a prominent `⚠️ Late Submission (<X>m Late)` badge.
  - **Strict Cut-Off**: Submissions are strictly locked the moment the deadline passes. Further upload attempts are blocked with `HTTP 403 Forbidden`.
- **On-the-Fly Schedule Modification**: Instructors can edit the schedule in real time from the dashboard—e.g. granting 15 extra minutes or switching policies during an active exam.

### 3. Live Auto-Refresh Dashboard
- **Dynamic Live Polling**: Built-in AJAX polling (`GET /api/exam/<id>/submissions-poll`) updates the dashboard every 5 seconds.
- **Instant Visual Metrics**:
  - Live counters for total submissions and individual lab venues increment dynamically.
  - New submissions appear in the table with animated pulse indicators without requiring manual page reloads.
- **Search & Filter Preservation**: Active roll number search queries and lab venue filters remain intact during live background refreshes.
- **One-Click Toggle**: Instructors can pause or resume auto-refresh (`⚡ Auto-Refresh: ON (5s)` / `⏸ Auto-Refresh: OFF`) at any time.

### 4. Multi-Lab Venue Segregation
- **Simultaneous Labs**: Exams can run concurrently across multiple physical computer labs (e.g., `Lab 1, Lab 2, Lab 3, CC-101`).
- **Venue Tagging**: Students select their seated lab from an instructor-configured dropdown.
- **Interactive Lab Pills**: Filter submissions by venue with a single click to monitor lab-by-lab attendance and submission density.

### 5. High-Speed Bulk & Single Downloads
- **Near-Instant ZIP Packaging**: Bulk download uses zero-recompression storage (`ZIP_STORED`), packaging 50+ MB archives in ~1.3 seconds compared to 15–20s with standard deflate algorithms.
- **Individual File Downloads**: Single-click download links next to each student entry for rapid on-the-spot evaluation.

### 6. Role-Based Access Control (RBAC) & Privacy
- **System Administrator (`admin`)**:
  - Registers new teacher accounts.
  - Resets passwords for any instructor with optional forced password change upon next login.
  - Deletes instructor accounts.
  - Oversees all courses and cleans up completed exams.
- **Instructor Course Isolation**: Instructors can only view, download, edit, or delete exams that they created. Submissions remain strictly confidential.
- **Self-Service Password Management**: Both administrators and instructors can update their own passwords at any time.

### 7. Permanent Course Server Disk Clean-Up
- When an exam is finished and archived, instructors can permanently delete the course.
- The portal purges database records and completely removes the course folder from disk to recover server storage.

---

## 🏗 System Architecture

```text
               +-------------------------------------------+
               |              Student Browser              |
               |  (Direct URL: /lab_exam/<course_code>)    |
               +---------------------+---------------------+
                                     |
                                     v HTTP (Port 80/443)
                        +------------+------------+
                        |      Nginx Web Server   |
                        |   Reverse Proxy Gateway |
                        | (client_max_body: 250M) |
                        +------------+------------+
                                     |
                                     v HTTP (127.0.0.1:8090)
             +-----------------------+-----------------------+
             |                                               |
             v                                               v
+------------------------+                      +------------------------+
| Flask Application      |                      | Teacher / Admin Portal |
| - Upload Engine        |                      | - Live Polling API     |
| - Timing Enforcer      |                      | - Schedule Editor      |
| - SHA-256 Checksum     |                      | - Fast ZIP Generator   |
+-----------+------------+                      +-----------+------------+
            |                                               |
            +-----------------------+-----------------------+
                                    |
                                    v
          +-------------------------+-------------------------+
          |                                                   |
          v                                                   v
+--------------------+                             +---------------------+
| SQLite3 Database   |                             | Server File Storage |
| (submissions.db)   |                             | (/data/admin/...)   |
| - Dynamic Schema   |                             | - Flat Roll.zip     |
+--------------------+                             +---------------------+
```

---

## 🚀 Server Installation & Quick Start

### Automated Setup on a Fresh Server (Recommended)

The included `setup.sh` script automates the complete installation on a brand new Ubuntu/Debian server.

1. **Clone the repository**:
   ```bash
   git clone https://github.com/KishantLab/Exam_Submission_Portal.git
   cd Exam_Submission_Portal
   ```

2. **Run the setup script**:
   ```bash
   sudo ./setup.sh
   ```
   *For unattended / non-interactive installation with smart defaults:*
   ```bash
   sudo ./setup.sh -y
   ```

The script will automatically:
- Install system packages (`python3`, `python3-pip`, `python3-venv`, `sqlite3`, `nginx`, `ufw`, `curl`).
- Set up a isolated Python virtual environment.
- Install Flask and Werkzeug dependencies.
- Initialize database tables and seed the `admin` account.
- Configure and start the `systemd` service (`lab-exam.service`).
- Generate and reload Nginx reverse proxy configurations with 250 MB upload limits.
- Update UFW firewall rules.
- Run automated self-health verification tests.

---

### Manual Step-by-Step Installation

If you prefer to configure the server manually, follow these steps:

#### Step 1: Install OS Dependencies
```bash
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv sqlite3 nginx curl rsync ufw
```

#### Step 2: Set Up Directory & Virtual Environment
```bash
sudo mkdir -p /data/admin/lab_exam/submissions
sudo chown -R $USER:$USER /data/admin/lab_exam

cd /data/admin/lab_exam
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install Flask>=3.0.0 Werkzeug>=3.0.0
```

#### Step 3: Initialize Database
```bash
./venv/bin/python3 -c "from app import init_db; init_db()"
```

#### Step 4: Configure Systemd Service
Create `/etc/systemd/system/lab-exam.service`:
```ini
[Unit]
Description=Lab Exam File Submission Portal
After=network.target

[Service]
Type=simple
User=kishan
WorkingDirectory=/data/admin/lab_exam
ExecStart=/data/admin/lab_exam/venv/bin/python3 /data/admin/lab_exam/app.py
Restart=always
RestartSec=3
Environment=PORT=8090
Environment=HOST=0.0.0.0
Environment=SUBMISSION_DIR=/data/admin/lab_exam/submissions
Environment=DB_PATH=/data/admin/lab_exam/submissions.db
Environment=SECRET_KEY=replace_with_a_secure_secret_key

[Install]
WantedBy=multi-user.target
```
Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable lab-exam.service
sudo systemctl restart lab-exam.service
```

#### Step 5: Configure Nginx Reverse Proxy
Add the following block to your Nginx site configuration (e.g., `/etc/nginx/sites-available/default`):
```nginx
location = /lab_exam {
    return 301 /lab_exam/;
}

location /lab_exam/ {
    proxy_pass http://127.0.0.1:8090/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 250M;
    proxy_read_timeout 120s;
}
```
Test and reload Nginx:
```bash
sudo nginx -t
sudo systemctl reload nginx
```

---

## 🔐 Default Credentials

| Role | Username | Default Password | Notes |
|---|---|---|---|
| **System Administrator** | `admin` | `admin@accl` | Change immediately via "🔑 Change Password" |
| **Default Instructor** | `kishan` | `password123` | Can be reset or modified by admin |

---

## 🧪 Testing & Verification

The test suite covers end-to-end functionality including RBAC permissions, teacher course isolation, timing start/end windows, strict cut-off, late delay calculations, dynamic schema migrations, and real-time polling APIs:

```bash
python3 test_app.py
```

Expected output:
```text
.....
----------------------------------------------------------------------
Ran 5 tests in ~2.4s

OK
```

---

## 🛠 Service Management Commands

```bash
# Check service status
sudo systemctl status lab-exam.service

# Restart application
sudo systemctl restart lab-exam.service

# View live application logs
sudo journalctl -u lab-exam.service -f

# Check Nginx status
sudo systemctl status nginx
```

---

## 📁 Repository Structure

```text
Exam_Submission_Portal/
├── app.py                  # Core Flask application, routes, models & logic
├── requirements.txt        # Python package dependencies
├── setup.sh                # Interactive / automated server setup script
├── deploy.sh               # Quick continuous deployment synchronization script
├── test_app.py             # Full end-to-end integration test suite
├── nginx-lab-exam.conf     # Nginx reverse proxy location configuration
├── lab-exam.service        # Systemd service unit definition
├── static/
│   ├── style.css           # Modern, responsive UI stylesheet
│   ├── script.js          # Client upload engine, AJAX receipt & drag-and-drop
│   ├── images/
│   │   ├── accl_logo.png   # ACCL laboratory insignia
│   │   └── iitbhilai_logo.png # IIT Bhilai official emblem
│   └── uploads/            # Custom course badges & instructor logos
└── templates/
    ├── index.html          # Student file submission portal & digital receipt
    ├── admin.html          # Instructor & Administrator live dashboard
    ├── login.html          # Secure login gateway
    └── set_password.html   # Forced first-login password initialization
```

---

## 👨‍💻 Developer & Attribution

- **Developer**: **Kishan Tamboli (PhD)**
- **Laboratory**: **Accelerated Computing Research Lab (ACCL)**
- **Institution**: **Indian Institute of Technology Bhilai (IIT Bhilai)**
- **Repository**: [https://github.com/KishantLab/Exam_Submission_Portal](https://github.com/KishantLab/Exam_Submission_Portal)
