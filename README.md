# Lab Exam Portal

Developed & Maintained by **Advanced Computing & Communications Laboratory (ACCL)**, Department of CSE & EE, IIT Bhilai.

A university-level web portal for laboratory examination submissions with:
- **System Administrator Account**: Full authority to register teachers, reset/change teacher passwords, and delete accounts.
- **Teacher Course Management**: Teachers can create and manage their own courses with complete data confidentiality.
- **Multi-Lab Venues**: Support for multiple concurrent computer labs (e.g. Lab 1, Lab 2, CC-1) with real-time filtering.
- **Server Disk Clean-Up**: Teachers can permanently delete completed courses and remove all files from disk.
- **Direct Student Submission**: Students use short URLs (e.g. `http://10.10.14.104/lab_exam/csl100`) without login.
- **Single-Folder Flat Storage**: Exactly one file per student (`<ROLL_NUMBER>.zip`), stored in `/data/admin/lab_exam/submissions/<COURSE_FOLDER>/`.
- **Fast Bulk & Single Downloads**: One-click bulk download of all submissions as `.ZIP` and individual student file downloads.

---

## Deployment Information
- **Server**: `10.10.14.104` (User: `kishan`)
- **App Path**: `/data/admin/lab_exam`
- **Port**: `8090` (Service: `lab-exam.service`)
- **Nginx Reverse Proxy**:
  - Main Portal: `http://10.10.14.104/lab_exam/`
  - Teacher & Admin Login: `http://10.10.14.104/lab_exam/login`
  - Dashboard: `http://10.10.14.104/lab_exam/admin`
  - Student Exam Short Link: `http://10.10.14.104/lab_exam/<course_code>` (e.g. `/csl100`)

---

## Administrator Credentials
- **Username**: `admin`
- **Password**: `admin@accl`
*(Password can be changed anytime in the dashboard via "🔑 Change Password")*

### Admin Powers
1. **Register Teachers**: Add new teacher accounts with custom username, name, and password.
2. **Reset/Change Passwords**: Set a new password for any teacher, with option to require the teacher to set their own new password upon next login.
3. **Delete Teachers**: Permanently delete teacher accounts.
4. **Oversee All Courses**: Admin can view and manage all course exams across the institution.

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
