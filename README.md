# ACCLLMS: Learning Management System & Classroom Portal

Developed by **Kishan Tamboli (PhD)**  
**Accelerated Computing Research Lab (ACCL)**  
**Indian Institute of Technology Bhilai (IIT Bhilai)**  

---

## 📖 Overview

**ACCLLMS** is an enterprise-grade, locally hosted Learning Management System tailored for university laboratory coursework, dynamic coursework grading, student personal workspaces, and secure timed practical exams. Designed with Google Classroom feature-parity and enhanced with an isolated student private locker system and strict exam lockdown capabilities, ACCLLMS provides zero-friction access while upholding strict academic integrity.

---

## ✨ Key Features & Capabilities

### 1. Multi-Role Authentication & Access Control
- **Student Portal**:
  - Sign up with Full Name, standardized Roll Number (e.g. `B26DS001`), and password.
  - Course enrollment via unique 6-character **Class Codes** (e.g. `ACCL26`).
  - View upcoming deadlines, submission history, and instructor feedback.
- **Instructor / Faculty Portal**:
  - Course authoring and curriculum management.
  - Class code generation and reset.
  - Gradebook matrix with inline scoring and 1-click CSV export.
  - Batch archive download for student submissions.
- **Administrator Console**:
  - Global user directory, role promotion (Student $\leftrightarrow$ Teacher $\leftrightarrow$ Admin), and credential resets.

### 2. 🔒 Strict Exam Mode Lockdown (Anti-Cheating & Exam Windows)
- **Zero Resource Access**: During active exam windows or when Strict Exam Lockdown is enabled, enrolled students are **strictly locked out** of all course stream announcements, lecture notes, class materials, and their personal locker.
- **Single Submission Link**: Navigation automatically redirects to the dedicated Exam Portal.
- **Strict ZIP-Only Validation**: Exam submissions strictly permit only valid `.zip` archives (verified on both client and server via ZIP magic byte headers `PK\x03\x04`).
- **Cryptographic Receipts**: Immediate generation of a tamper-evident digital receipt featuring student roll number, lab venue, submission timestamp, version, and **SHA-256 cryptographic verification checksum**.

### 3. 🔒 Student Private Space ("My Cloud Locker")
- Each student receives an isolated private workspace (`storage/lockers/<user_id>/`).
- **File Manager**: Upload code files (`.c`, `.cpp`, `.cu`, `.py`, `.sh`), text documents, PDFs, and datasets.
- **In-Browser Code Preview**: Inspect code files with line numbers and syntax formatting.
- **Attach from Locker**: When turning in assignments in Classwork, students can select files directly from their private locker in one click without re-uploading from disk.
- **Storage Quota**: Visual progress bar tracking storage usage against quota (default 500 MB).
- **Locker Backup**: 1-click download of the entire locker as a `.zip` archive.

### 4. 📚 Google Classroom Tab Architecture
- **Stream**: Real-time announcement feed with rich text, file attachments, pinned notices, and threaded discussions.
- **Classwork (Topic-Organized)**:
  - Grouped by topics / modules (e.g. *Week 1: Fundamentals*, *Lab Assessments*).
  - Assignments with deadlines, maximum points, allowed file types, and resubmission policies.
  - Materials & lecture slides sharing.
- **People**: Course roster showing instructors and enrolled students with enrollment dates and submission stats.
- **Grades**: Teacher Gradebook matrix (Students $\times$ Coursework) with inline score editing, missing badges, late flags, and CSV export.

---

## ⚙️ Architecture & Port Mapping on Host (10.10.14.104)

| Service | Port | Nginx Path | Description | Status |
| :--- | :--- | :--- | :--- | :--- |
| **accl-monitor** | 8080 | `/accl-gpu/` | ACCL GPU Cluster Monitor | Untouched |
| **accl-portal** | 8085 | `/accl/` | ACCL Research Lab Portal | Untouched |
| **lab-exam** | 8090 | `/lab_exam/` | Lab Exam File Submission Portal | Untouched |
| **accl-lms** | **8095** | `/lms/` | **ACCLLMS Classroom & LMS Portal** | **NEW** |

---

## 🚀 Quick Start & Deployment

```bash
# Clone or navigate to the repository
cd /data/admin/ACCLLMS

# Install dependencies
pip3 install -r requirements.txt

# Run the deployment script
bash deploy.sh
```

### Systemd Service Management

```bash
sudo systemctl status accl-lms.service
sudo systemctl restart accl-lms.service
sudo journalctl -u accl-lms.service -f
```

---

## 👤 Default Pre-Seeded Accounts

- **Superadmin**: `admin` / `admin@accl`
- **Faculty / Instructor**: `kishan` / `password123`
- **Student**: `student1` / `student123` (Roll: `B26DS001`)
