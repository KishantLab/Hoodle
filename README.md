# ACCL Lab Exam File Submission Portal

Developed by **Advanced Computing & Communications Laboratory (ACCL)**, Department of CSE & EE, IIT Bhilai.

A robust, self-hosted web portal for lab exam submissions featuring:
- **Teacher Administration Portal**: Manage courses, create exam submission links, set submission policies, and download files.
- **Direct Student Submission**: Students open course-specific links directly without login.
- **Single-Folder Flat Storage**: Exactly one file per student (`<ROLL_NUMBER>.zip`), stored directly in `/data/admin/lab_exam/submissions/<COURSE_FOLDER>/`.
- **Bulk & Single Downloads**: One-click bulk download of all submissions as `.ZIP` and single file downloads for each student.

---

## Deployment Information
- **Server**: `10.10.14.104` (User: `kishan`)
- **App Path**: `/data/admin/lab_exam`
- **Port**: `8090` (Service: `lab-exam.service`)
- **Nginx Reverse Proxy**:
  - Main URL: `http://10.10.14.104/lab_exam/`
  - Teacher Login: `http://10.10.14.104/lab_exam/login`
  - Teacher Dashboard: `http://10.10.14.104/lab_exam/admin`
  - Student Exam Link: `http://10.10.14.104/lab_exam/exam/<exam-slug>`
  - Direct Access: `http://10.10.14.104:8090/`

---

## Default Teacher Credentials
- **Username**: `kishan`
- **Password**: `password123`
*(Password can be changed in the teacher dashboard)*

---

## Key Features

1. **Teacher Course & Exam Creation**:
   - Create exam pages with Course Code (e.g. `CSL100`), Course Name, Exam Title, and Instructions.
   - Dedicated folder automatically generated in `/data/admin/lab_exam/submissions/<folder_name>/`.
   - Option to upload a custom lab logo (defaults to official IIT Bhilai logo).
   - Permanent ACCL attribution on all pages.

2. **Exam Policies**:
   - **Allowed File Types**: Restrict to `.zip` (default) or comma-separated list (`zip,tar.gz,py,c`) or `any`.
   - **Submission Mode**:
     - *Multiple Submissions Allowed*: Latest submission replaces previous file (only 1 file kept per student).
     - *Single Submission Only*: Subsequent submission attempts by the same roll number are blocked.

3. **Student Direct Submission**:
   - No student login required.
   - Roll number automatically converted to uppercase alphanumeric (`A-Z0-9_-`).
   - Drag & drop or file browse.
   - Live upload progress bar.
   - Verification receipt with SHA-256 hash.

4. **Bulk and Single File Downloads**:
   - Teacher dashboard features:
     - **Download All (.ZIP)**: Instant packaging and download of all student files for the selected exam.
     - **Individual Download**: Click "Download" on any student's row to download just their file.

---

## Managing the Service

```bash
# Check status
ssh kishan@10.10.14.104 "systemctl status lab-exam.service"

# Restart
ssh kishan@10.10.14.104 "sudo systemctl restart lab-exam.service"

# View logs
ssh kishan@10.10.14.104 "sudo journalctl -u lab-exam.service -f"
```
