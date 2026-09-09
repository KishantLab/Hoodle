# Lab Exam File Submission Portal

A lightweight, self-hosted web portal for students to submit lab exam files directly without login.

## Deployment Details
- **Server**: `10.10.14.104` (User: `kishan`)
- **Server Path**: `/data/admin/lab_exam`
- **Port**: `8090` (Service: `lab-exam.service`)
- **Nginx Reverse Proxy**:
  - `http://10.10.14.104/lab_exam/`
  - `http://10.10.14.104:8090/` (Direct)
- **Submissions Folder**: `/data/admin/lab_exam/submissions/`
- **Database**: `/data/admin/lab_exam/submissions.db`

---

## Features
1. **Direct Submission Without Login**:
   - Students directly open the portal URL and submit their work.
2. **Automatic Roll Number Uppercase**:
   - Auto-capitalizes and filters to alphanumeric characters (`A-Z0-9_-`) in real-time as the student types and in backend sanitization.
3. **Zip File Support**:
   - Accepts `.zip` archives (and tar/code files up to 150MB).
   - Drag-and-drop or file picker with real-time size validation.
4. **Multiple Submissions Allowed & Latest File Considered**:
   - Students can submit multiple times if they need to make corrections or updates.
   - All submissions are preserved version-by-version in `/data/admin/lab_exam/submissions/<ROLL_NUMBER>/`.
   - The latest file is automatically maintained as `<ROLL_NUMBER>_latest.<ext>`.
   - SQLite database keeps track of submission version numbers, timestamps, IP addresses, and SHA-256 checksums.
5. **Instant "Submitted" Feedback**:
   - Displays a clean success receipt showing:
     - Roll Number
     - Status: Latest Considered
     - Version count (`Submission #1`, `Submission #2 (Latest)`)
     - File size
     - Timestamp
     - Verification SHA-256 hash
6. **Instructor Dashboard & Bulk Export**:
   - Access `http://10.10.14.104/lab_exam/admin`
   - Single-click "Download All Latest (.ZIP)" to download a packaged ZIP archive containing each student's latest exam file.

---

## Service Management Commands

```bash
# Check status
ssh kishan@10.10.14.104 "systemctl status lab-exam.service"

# Restart portal
ssh kishan@10.10.14.104 "sudo systemctl restart lab-exam.service"

# View real-time service logs
ssh kishan@10.10.14.104 "sudo journalctl -u lab-exam.service -f"
```
