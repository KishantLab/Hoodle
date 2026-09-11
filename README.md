# Hoodle: Accelerated Classroom & Lab Learning Management System

<div align="center">

<img src="static/images/hoodle_banner.png" alt="Hoodle Banner" style="max-width: 100%; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.1);">

<br><br>

**Accelerated Classroom & Lab Learning (Hoodle)**  
*Developed by **Kishan Tamboli (PhD)** • **Accelerated Computing Research Lab (ACCL)** • **Indian Institute of Technology Bhilai***

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Flask 3.0](https://img.shields.io/badge/framework-Flask%203.0-green.svg)](https://palletsprojects.com/p/flask/)
[![Tests](https://img.shields.io/badge/tests-20%20passing-brightgreen.svg)](test_lms.py)
[![Brand Kit](https://img.shields.io/badge/brand-Hoodle%20Kit%20(SVG%2FPNG)-blueviolet.svg)](static/images/hoodle_brand_kit.zip)
[![License](https://img.shields.io/badge/license-ACCL%20IIT%20Bhilai-red.svg)](README.md)

</div>

---

## 📖 Overview

**Hoodle** is an enterprise-grade, locally deployable Learning Management System (LMS) specifically architected for university academic instruction, engineering computing laboratories, practical examinations, and continuous assessment.

Combining the classroom simplicity of **Google Classroom** with the evaluation power of **Canvas LMS**, Hoodle brings modern, secure, and privacy-preserving education technology to local intranet environments:
- **In-Portal Live Camera QR Code Attendance Scanner** with rotating anti-proxy tokens.
- **Canvas-Style Weighted Grading Engine** out of 100 with category percentages, Excel/CSV bulk import, and customizable mathematical formulas.
- **Strict Exam Lockdown Mode** with timed windows, ZIP validation, auto-expiration, and cryptographic submission freeze.
- **Cryptographic Digital Submission Receipts** with SHA-256 verification and scannable QR tokens.
- **Integrated In-App PDF Opener / Reader** for seamless in-browser viewing of reference files, student submissions, and locker documents without downloading.
- **Isolated Student Private Space ("My Cloud Locker")** with code preview and disk quotas.
- **Granular Multi-Tier Role Governance** (Admin, Primary Faculty, Co-Teacher/TA, Student).

---

## ✨ Core Functionalities & System Modules

### 1. 📚 6-Tab Coursework Environment
Every course in Hoodle provides a Google Classroom-style tab navigation:
- **📢 Stream**: Real-time course announcements with rich text, file attachments (with in-app PDF preview), pinned notices, and threaded discussions.
- **📝 Classwork**: Topic-organized curriculum units, assignment creation with max points, deadlines, allowed file types, and reference material distribution.
- **👥 People**: Complete course roster for instructors and co-teachers. Features inline role switching (`Student` $\leftrightarrow$ `Co-Teacher` $\leftrightarrow$ `Faculty`), student invitation via class code, and strict student privacy safeguards (students never see classmates' submission status or counts).
- **📅 Attendance**: Real-time attendance dashboard featuring projector screen mode, student check-in telemetry, audit filter bar, CSV export, and bulk manual marking.
- **📊 Grades**: Gradebook matrix with weighted evaluation out of 100, itemized student scorecards, Excel/CSV grade import, pre-filled template generator, and category weighting controls.

---

### 2. 📷 In-Portal Live Camera QR Attendance Scanner & Projector
- **Classroom Projector Screen (`/courses/<id>/attendance/projector`)**:
  - Fullscreen display for lecture halls and lab projectors.
  - Generates SVG QR codes using rotating cryptographic tokens.
  - **1-Second Live Polling Stream**: Automatically displays attending students as they scan in real time with audio-visual check-in feedback.
- **In-Portal Camera Scanner (`#qrScannerModal`)**:
  - Embedded HTML5 camera viewfinder opening directly from any page.
  - Auto-selects the environment (rear) camera on mobile devices with an instant **🔄 Flip Camera** toggle.
  - **Android & iOS Camera Permission Guides**: Modal guidance tabs for Chrome (Android) and Safari (iOS) if permissions were previously blocked.
  - **Instant Phone Camera Fallback**: Allows students to snap a quick photo using their native camera app, scanning the QR code client-side without requiring WebRTC video streaming permissions.
- **Anti-Fraud Dynamic Rotation**:
  - Tokens rotate dynamically based on timestamped HMAC secrets. Screenshots shared over chat expire within seconds, preventing proxy attendance.

---

### 3. ⚖️ Weighted Assessment Engine & Gradebook
- **Final Marks Out of 100**:
  - Instructors configure category weight percentages (e.g. End-Sem: 20%, Mid-Sem: 20%, Lab Assessments: 45%, Lab Exam: 10%, Live Attendance: 5%).
  - Automatically factors in live attendance percentages calculated from dynamic QR scans.
- **Custom Mathematical Formula Support**:
  - Optional formula engine allowing custom expressions (e.g. `0.20*EndSemesterExam + 0.10*LabExams + 0.05*Attendance`).
- **Student Personal Scorecard**:
  - Displays **Weighted Total (Out of 100)** and running score without confusing letter grades.
  - Prioritizes itemized coursework breakdown first, placing category weight reference down below.
- **Bulk Spreadsheet Import & Export**:
  - Upload grades via **Excel (`.xlsx`)**, **CSV**, or **TSV** exported from Google Sheets or Excel.
  - Auto-matches student rows by Roll Number, Username, or Email, and columns by coursework title.
  - 1-click **Download Pre-Filled Course Template (`.csv`)** with enrolled students already populated.

---

### 4. 🔒 Strict Exam Lockdown Mode
- **Zero-Resource Lockdown**:
  - When enabled, students entering the course are strictly locked out of lecture materials, stream announcements, and their private cloud locker.
  - Students are automatically redirected to the dedicated Exam Portal.
- **Strict ZIP Archive Validation**:
  - Enforces `.zip` file submissions with magic-byte header inspection (`PK\x03\x04`).
- **Auto-Expiration & Cryptographic Lockout**:
  - Real-time countdown timer synchronized with server time.
  - Lockdown automatically lifts when the exam end time passes.
  - Submissions are permanently locked; students cannot unsubmit or overwrite work once the exam concludes.

---

### 5. 📜 Cryptographic Digital Submission Receipts
- Every assignment and exam submission generates an immutable digital receipt.
- Contains student roll number, lab venue, submission timestamp, version count, and a **SHA-256 cryptographic checksum**.
- Includes a verifiable digital receipt URL and scannable QR verification code.

---

### 6. 📕 Integrated In-App PDF Opener / Reader
- **Zero-Download Document Inspection**:
  - High-performance embedded PDF reader modal (`#pdfViewerModal`) accessible across the entire LMS.
  - Features a clean toolbar with **⛶ Fullscreen toggle**, **↗ Open in New Tab**, **⬇ Download File**, and **✕ Close** (also supports `Escape` key).
- **Available Across All Workflows**:
  - **Coursework Reference Files**: Teachers attach PDFs; students view them directly in-app.
  - **Teacher Submissions Roster**: Instructors can click **👁 View PDF** on student submissions to grade lab reports immediately without downloading files to disk.
  - **Student "Your Work"**: Students can inspect their submitted PDF solution directly.
  - **Stream Announcements**: In-app reading for PDF notices.
  - **Student Locker**: Previewing any locker `.pdf` file opens the in-app PDF reader.

---

### 7. 🗄️ Student Cloud Locker ("My Private Space")
- Every student has an isolated private workspace on the server (`storage/lockers/<user_id>/`).
- Upload code files (`.c`, `.cpp`, `.cu`, `.py`, `.sh`, `.txt`, `.md`, `.pdf`, `.json`, `.sql`).
- In-browser code preview with line numbers and syntax styling.
- 1-click **Attach from Locker** when submitting coursework assignments.
- Storage quota meter (default 500 MB) with 1-click whole-locker `.zip` backup download.

---

### 8. 🛡️ Role-Based Access Control (RBAC)
| Role | Permissions & Capabilities |
| :--- | :--- |
| **Administrator (`admin`)** | Full system directory (`/admin/users`), password resets, role assignment, user creation/deletion, system storage auditing. |
| **Faculty / Instructor (`teacher`)** | Create and manage courses, author coursework, grade submissions, configure weights, export gradebooks, manage class rosters. |
| **Co-Teacher / TA (`ta`)** | Course-level teaching privileges: grade coursework, inspect submissions, view and manage attendance logs. |
| **Student (`student`)** | Join courses with 6-character class codes, view personal grades and weighted total out of 100, scan attendance QR codes, turn in work, access personal locker. Classmate submission privacy is strictly preserved. |

---

### 9. 🎨 Official Brand Identity & Downloadable Logo Kit
- **Official Visual Identity Portal (`/brand`)**:
  - Dedicated brand page providing direct 1-click downloads for instructors, students, developers, and event organizers.
  - **Scalable Vector Logo (`hoodle_logo.svg`)**: Infinite-resolution brand lockup with typography and ACCL lab credentials for print, posters, and web.
  - **High-Resolution Raster Emblem (`hoodle_logo.png`)**: 1024×1024 px academic mortarboard, stylized 'H', open book wings, and neural computing network nodes.
  - **Horizontal Presentation Banner (`hoodle_banner.png`)**: 16:9 HD banner for presentations, publications, slide decks, and GitHub repositories.
  - **Dark Squircle App Icon (`hoodle_app_icon.png` & `hoodle_mark.svg`)**: Mobile homescreen icon, PWA badge, and web favicon.
  - **Downloadable Brand Kit (`/brand/download/kit`)**: Bundled `.zip` archive containing all SVG, PNG, and partner logos (ACCL Lab, IIT Bhilai) along with official color specifications (`#1D4ED8`, `#06B6D4`, `#F59E0B`, `#0F172A`).

---

## 🏗️ Architecture & Technology Stack

```
                                 [ Client Browser / Mobile ]
                                             │
                                   HTTPS / Port 443 / 8443
                                             ▼
                                     [ Nginx Reverse Proxy ]
                                             │  (Proxy pass to 127.0.0.1:8095)
                                             ▼
                               [ Gunicorn WSGI Application Server ]
                                  (16 Workers • 64 Threads)
                                             │
                                       [ Flask App ]
                   ┌─────────────────────────┼─────────────────────────┐
                   ▼                         ▼                         ▼
         [ SQLite3 Database ]      [ Storage Directory ]      [ Static & Templates ]
          (accl_lms.db)             • lockers/                 • HTML5 / Jinja2
                                    • submissions/             • Vanilla JS / CSS3
                                    • attachments/             • HTML5-QRCode Scanner
                                    • exports/
```

- **Backend**: Python 3.10+, Flask 3.0, Werkzeug 3.0, Gunicorn 21.2.
- **Database**: SQLite3 with WAL mode, foreign keys, and cascading indexes.
- **Frontend**: Responsive HTML5, CSS Variables, Flexbox/Grid, Vanilla JavaScript.
- **Barcode & QR Engine**: `html5-qrcode` (client video stream), `qrcode[pil]` (server SVG/PNG QR generation).
- **Spreadsheet Processing**: `openpyxl` (Excel `.xlsx`), standard `csv` engine.

---

## 🚀 Fresh Installation & Setup Guide

### Method 1: Automated 1-Command Setup (Recommended)

Run the included interactive setup script:

```bash
git clone https://github.com/KishantLab/Hoodle.git
cd Hoodle
chmod +x setup.sh
./setup.sh
```

The script will automatically:
1. Install all necessary OS packages (`python3`, `pip`, `venv`, `sqlite3`, `nginx`, `curl`).
2. Create isolated storage directories with permissions.
3. Configure the Python virtual environment and install all dependencies.
4. Initialize the SQLite database schema and seed default accounts.
5. Run the full 19-test automated test suite to verify system integrity.
6. Configure and start the systemd service (`accl-lms.service`).
7. Configure the Nginx reverse proxy with HTTPS camera streaming headers.

---

### Method 2: Manual Installation Step-by-Step

#### 1. Clone the Repository & Setup Virtual Environment
```bash
git clone https://github.com/KishantLab/Hoodle.git /data/admin/Hoodle
cd /data/admin/Hoodle

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

#### 2. Create Storage Directories
```bash
mkdir -p storage/lockers storage/submissions storage/attachments storage/exports
chmod -R 775 storage
```

#### 3. Initialize Database
```bash
python3 -c "import app; app.init_db(); print('Database initialized.')"
```

#### 4. Run Verification Tests
```bash
python3 -m unittest -v test_lms.py
```

#### 5. Configure Systemd Service
Create `/etc/systemd/system/accl-lms.service`:
```ini
[Unit]
Description=ACCL Learning Management System & Classroom Portal (Hoodle)
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/data/admin/Hoodle
Environment="PATH=/data/admin/Hoodle/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/data/admin/Hoodle/venv/bin/gunicorn --workers 16 --threads 4 --worker-class gthread --bind 0.0.0.0:8095 --timeout 120 --keep-alive 5 --max-requests 2000 --max-requests-jitter 200 app:app
Restart=always
RestartSec=3
KillMode=mixed
TimeoutStopSec=30
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable accl-lms.service
sudo systemctl start accl-lms.service
```

#### 6. Configure Nginx Reverse Proxy (with HTTPS Camera Support)
In `/etc/nginx/sites-available/default` (or your SSL server block):
```nginx
location = /lms {
    return 301 /lms/;
}

location /lms/ {
    proxy_pass http://127.0.0.1:8095/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 500M;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
```

Reload Nginx:
```bash
sudo nginx -t && sudo systemctl reload nginx
```

---

## 👤 Pre-Seeded Default Accounts

| Account Role | Username / Roll | Default Password | Notes |
| :--- | :--- | :--- | :--- |
| **System Administrator** | `admin` | `admin@accl` | Global management, user directory, system configuration |
| **Primary Faculty** | `kishan` | `password123` | Course author, instructor controls, gradebook |
| **Student Account** | `student1` | `student123` | Roll No: `B26DS001` (Aarav Sharma) |

> [!IMPORTANT]
> Change the default administrator and faculty passwords immediately upon initial server deployment.

---

## 🧪 Automated Testing Suite

Hoodle includes 19 end-to-end integration and unit tests covering all core workflows:
```bash
python3 -m unittest -v test_lms.py
```

### Verified Test Matrix
```text
test_admin_user_directory ........................................... ok
test_announcements_and_comments ..................................... ok
test_assignment_submission_and_receipt ............................... ok
test_attendance_projector_and_apis .................................. ok
test_brand_assets_and_logo_downloads ................................. ok
test_bulk_manual_attendance_and_csv_export ........................... ok
test_canvas_weighted_grading_and_bulk_import ........................ ok
test_coteacher_attendance_log_access ................................. ok
test_course_join_by_code ............................................ ok
test_default_accounts_exist ......................................... ok
test_dynamic_token_rotation ......................................... ok
test_exam_lockdown_auto_expire_and_locked_submission ................. ok
test_gradebook_and_csv_export ....................................... ok
test_in_app_pdf_opener_and_inline_routes ............................ ok
test_role_management_and_coteacher_workflow ......................... ok
test_strict_exam_mode_lockdown_and_zip_only .......................... ok
test_student_attendance_flow_and_duplicate_prevention ................ ok
test_student_locker_upload_and_preview ............................... ok
test_student_login_and_dashboard .................................... ok
test_student_registration ........................................... ok

----------------------------------------------------------------------
Ran 20 tests in 58.644s - ALL OK
```

---

## 📄 License & Credits

Developed by:  
**Kishan Tamboli (PhD)**  
*Accelerated Computing Research Lab (ACCL)*  
*Department of Computer Science & Engineering*  
*Indian Institute of Technology Bhilai (IIT Bhilai), India*  

*Accelerated Classroom & Lab Learning • ACCL IIT Bhilai*
