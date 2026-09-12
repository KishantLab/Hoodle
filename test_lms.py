#!/usr/bin/env python3
"""
Test Suite for ACCLLMS Learning Management System
"""

import os
import shutil
import tempfile
import unittest
import io
import zipfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import app


class ACCLLMSTestCase(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="accl_lms_unit_")
        self.db_path = Path(self.test_dir) / "test_lms.db"
        self.storage_dir = Path(self.test_dir) / "storage"

        app.DB_PATH = self.db_path
        app.STORAGE_DIR = self.storage_dir
        app.LOCKERS_DIR = self.storage_dir / "lockers"
        app.SUBMISSIONS_DIR = self.storage_dir / "submissions"
        app.ATTACHMENTS_DIR = self.storage_dir / "attachments"
        app.app.config["TESTING"] = True
        app.app.config["WTF_CSRF_ENABLED"] = False

        for d in (app.STORAGE_DIR, app.LOCKERS_DIR, app.SUBMISSIONS_DIR, app.ATTACHMENTS_DIR):
            d.mkdir(parents=True, exist_ok=True)

        with app.app.app_context():
            app.init_db()

        self.client = app.app.test_client()

    def tearDown(self):
        try:
            shutil.rmtree(self.test_dir)
        except Exception:
            pass

    def login(self, identifier, password):
        return self.client.post("/login", data={
            "identifier": identifier,
            "password": password
        }, follow_redirects=True)

    def logout(self):
        return self.client.get("/logout", follow_redirects=True)

    def test_default_accounts_exist(self):
        """Verify pre-seeded admin and teacher accounts."""
        conn = app.get_db()
        admin = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
        teacher = conn.execute("SELECT * FROM users WHERE username = 'kishan'").fetchone()
        student = conn.execute("SELECT * FROM users WHERE username = 'student1'").fetchone()
        conn.close()

        self.assertIsNotNone(admin)
        self.assertEqual(admin["role"], "admin")
        self.assertIsNotNone(teacher)
        self.assertEqual(teacher["role"], "teacher")
        self.assertIsNotNone(student)
        self.assertEqual(student["role"], "student")

    def test_student_registration(self):
        """Test student registration with roll number standardization."""
        res = self.client.post("/register", data={
            "full_name": "Rohan Gupta",
            "roll_number": "b26cs099",
            "email": "b26cs099@iitbhilai.ac.in",
            "password": "mypassword123",
            "confirm_password": "mypassword123",
            "role": "student"
        }, follow_redirects=True)

        self.assertIn(b"Registration successful", res.data)

        conn = app.get_db()
        user = conn.execute("SELECT * FROM users WHERE username = 'b26cs099'").fetchone()
        conn.close()
        self.assertIsNotNone(user)
        self.assertEqual(user["roll_number"], "B26CS099")

    def test_student_login_and_dashboard(self):
        """Test student sign in and dashboard rendering."""
        res = self.login("student1", "student123")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Welcome back", res.data)
        self.assertIn(b"Enrolled Courses", res.data)

    def test_student_locker_upload_and_preview(self):
        """Test private student locker file upload, quota tracking, and preview."""
        self.login("student1", "student123")

        file_content = b'#include <stdio.h>\nint main() { printf("Hello ACCL\\n"); return 0; }\n'
        data = {
            "files": (io.BytesIO(file_content), "hello_accl.c")
        }
        res = self.client.post("/locker/upload", data=data, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn(b"file(s) saved to your Private Locker", res.data)
        self.assertIn(b"hello_accl.c", res.data)

        conn = app.get_db()
        f_row = conn.execute("SELECT id FROM student_locker_files WHERE original_filename = 'hello_accl.c'").fetchone()
        conn.close()
        self.assertIsNotNone(f_row)

        prev_res = self.client.get(f"/locker/preview/{f_row['id']}")
        self.assertEqual(prev_res.status_code, 200)
        json_data = prev_res.get_json()
        self.assertEqual(json_data["type"], "text")
        self.assertIn("Hello ACCL", json_data["content"])

    def test_course_join_by_code(self):
        """Test joining a course via 6-character class code."""
        self.client.post("/register", data={
            "full_name": "New Student",
            "roll_number": "B26EE010",
            "email": "b26ee010@iitbhilai.ac.in",
            "password": "password123",
            "confirm_password": "password123",
            "role": "student"
        })

        self.login("b26ee010", "password123")

        res = self.client.post("/courses/join", data={"join_code": "ACCL26"}, follow_redirects=True)
        self.assertIn(b"Successfully joined CSL100", res.data)

    def test_assignment_submission_and_receipt(self):
        """Test turning in an assignment and verifying SHA-256 digital receipt."""
        self.login("student1", "student123")

        conn = app.get_db()
        cw = conn.execute("SELECT id, course_id FROM coursework WHERE type = 'assignment' LIMIT 1").fetchone()
        conn.close()

        code_data = b'// Solution to lab assignment 1\n#include <stdlib.h>\n'
        res = self.client.post(f"/courses/{cw['course_id']}/coursework/{cw['id']}/submit", data={
            "source_type": "local",
            "submission_file": (io.BytesIO(code_data), "allocator.c"),
            "lab_name": "Lab 2"
        }, content_type="multipart/form-data", follow_redirects=True)

        self.assertIn(b"Work submitted successfully", res.data)
        self.assertIn(b"allocator.c", res.data)

        conn = app.get_db()
        sub = conn.execute("SELECT * FROM submissions WHERE coursework_id = ?", (cw["id"],)).fetchone()
        conn.close()
        self.assertIsNotNone(sub)
        self.assertEqual(sub["status"], "turned_in")
        self.assertTrue(len(sub["sha256"]) == 64)

        r_res = self.client.get(f"/receipt/{sub['receipt_token']}")
        self.assertEqual(r_res.status_code, 200)
        self.assertIn(b"Official Digital Submission Receipt", r_res.data)
        self.assertIn(sub["sha256"].encode(), r_res.data)

    def test_strict_exam_mode_lockdown_and_zip_only(self):
        """
        Test the user-requested feature:
        When Exam Mode is active:
        1. Student is locked out of materials, stream, and locker, and redirected to exam portal.
        2. Non-zip files are rejected.
        3. Only valid .zip files are accepted and generate cryptographic receipt.
        """
        self.login("kishan", "password123")

        conn = app.get_db()
        exam = conn.execute("SELECT id, course_id FROM coursework WHERE type = 'exam' LIMIT 1").fetchone()
        conn.close()

        self.client.post(f"/courses/{exam['course_id']}/coursework/{exam['id']}/toggle-exam-mode", follow_redirects=True)

        self.logout()

        self.login("student1", "student123")

        locker_res = self.client.get("/locker", follow_redirects=True)
        self.assertIn(b"STRICT EXAM LOCKDOWN ACTIVE", locker_res.data)
        self.assertIn(b"EXAM MODE LOCKDOWN", locker_res.data)

        stream_res = self.client.get(f"/courses/{exam['course_id']}/stream", follow_redirects=True)
        self.assertIn(b"STRICT EXAM LOCKDOWN ACTIVE", stream_res.data)

        fake_file = b"not a zip file"
        bad_res = self.client.post(f"/courses/{exam['course_id']}/exam/{exam['id']}/submit", data={
            "exam_file": (io.BytesIO(fake_file), "solution.c"),
            "lab_name": "Lab 1"
        }, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn(b"Only .zip files are allowed", bad_res.data)

        mem_zip = io.BytesIO()
        with zipfile.ZipFile(mem_zip, mode="w") as zf:
            zf.writestr("main.c", "#include <stdio.h>\nint main(){return 0;}\n")
        mem_zip.seek(0)

        good_res = self.client.post(f"/courses/{exam['course_id']}/exam/{exam['id']}/submit", data={
            "exam_file": (mem_zip, "B26DS001_exam.zip"),
            "lab_name": "Lab 1"
        }, content_type="multipart/form-data", follow_redirects=True)

        self.assertIn(b"Official Digital Submission Receipt", good_res.data)
        self.assertIn(b"B26DS001", good_res.data)

    def test_announcements_and_comments(self):
        """Test posting stream announcements and threaded comments."""
        self.login("kishan", "password123")
        conn = app.get_db()
        course = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()
        conn.close()

        res = self.client.post(f"/courses/{course['id']}/announcements", data={
            "content": "Important class update regarding midterm."
        }, follow_redirects=True)
        self.assertIn(b"Announcement published", res.data)

        self.logout()
        self.login("student1", "student123")

        conn = app.get_db()
        ann = conn.execute("SELECT id FROM announcements WHERE content LIKE '%Important class update%'").fetchone()
        conn.close()

        c_res = self.client.post(f"/announcements/{ann['id']}/comments", data={
            "content": "Thank you Prof. Kishan!"
        }, follow_redirects=True)
        self.assertIn(b"Thank you Prof. Kishan!", c_res.data)

    def test_gradebook_and_csv_export(self):
        """Test teacher gradebook view and CSV export."""
        self.login("kishan", "password123")
        conn = app.get_db()
        course = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()
        conn.close()

        gb_res = self.client.get(f"/courses/{course['id']}/grades")
        self.assertEqual(gb_res.status_code, 200)
        self.assertIn(b"Course Gradebook Matrix", gb_res.data)

        csv_res = self.client.get(f"/courses/{course['id']}/grades/export-csv")
        self.assertEqual(csv_res.status_code, 200)
        self.assertEqual(csv_res.mimetype, "text/csv")
        self.assertIn(b"Roll Number", csv_res.data)
        self.assertIn(b"Student Name", csv_res.data)

    def test_admin_user_directory(self):
        """Test admin user directory and role changes."""
        self.login("admin", "admin@accl")
        res = self.client.get("/admin/users")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"System User Directory", res.data)
        self.assertIn(b"student1", res.data)
        self.assertIn(b"kishan", res.data)

    def test_dynamic_token_rotation(self):
        """Test cryptographic token generation and time-window validation."""
        course_id = 1
        session_type = "Lecture"
        token_now = app.get_dynamic_attendance_token(course_id, session_type)
        self.assertIsInstance(token_now, str)
        self.assertEqual(len(token_now), 8)

        # Token should validate for current block
        self.assertTrue(app.validate_dynamic_attendance_token(course_id, session_type, token_now))

        # Invalid token should fail
        self.assertFalse(app.validate_dynamic_attendance_token(course_id, session_type, "INVALID8"))

    def test_attendance_projector_and_apis(self):
        """Test teacher projector screen, QR SVG generation, and live polling API."""
        self.login("kishan", "password123")
        conn = app.get_db()
        course = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()
        conn.close()

        # 1. Projector view
        res = self.client.get(f"/courses/{course['id']}/attendance/projector")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"SCAN TO RECORD ATTENDANCE", res.data)
        self.assertIn(b"secondsRemaining", res.data)

        # 2. Token API
        tok_res = self.client.get(f"/api/attendance/token/{course['id']}?type=Lecture")
        self.assertEqual(tok_res.status_code, 200)
        tok_data = tok_res.get_json()
        self.assertIn("token", tok_data)
        self.assertIn("seconds_remaining", tok_data)

        # 3. QR SVG API
        qr_res = self.client.get(f"/api/attendance/qr/{course['id']}?type=Lecture&token={tok_data['token']}")
        self.assertEqual(qr_res.status_code, 200)
        self.assertEqual(qr_res.mimetype, "image/svg+xml")
        self.assertIn(b"<svg", qr_res.data)

        # 4. Live poll API
        poll_res = self.client.get(f"/api/attendance/live-poll/{course['id']}?type=Lecture")
        self.assertEqual(poll_res.status_code, 200)
        poll_data = poll_res.get_json()
        self.assertIn("attendee_count", poll_data)

    def test_student_attendance_flow_and_duplicate_prevention(self):
        """Test student scanning QR code, confirmation, and duplicate prevention."""
        conn = app.get_db()
        course = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()
        conn.close()

        token = app.get_dynamic_attendance_token(course["id"], "Lecture")

        self.login("student1", "student123")

        # 1. Student lands on scan page
        scan_res = self.client.get(f"/attend/{course['id']}?type=Lecture&token={token}")
        self.assertEqual(scan_res.status_code, 200)
        self.assertIn(b"Confirm Attendance", scan_res.data)

        # 2. Student submits attendance
        sub_res = self.client.post(f"/attend/{course['id']}/submit", data={
            "token": token,
            "session_type": "Lecture"
        }, follow_redirects=True)
        self.assertEqual(sub_res.status_code, 200)
        self.assertIn(b"Attendance Recorded!", sub_res.data)

        # 3. Verify logged in DB
        conn = app.get_db()
        log = conn.execute("""
            SELECT * FROM attendance_logs WHERE course_id = ? AND student_name LIKE '%Aarav%'
        """, (course["id"],)).fetchone()
        conn.close()
        self.assertIsNotNone(log)
        self.assertEqual(log["method"], "QR_SCAN")

        # 4. Duplicate scan attempt should show already logged
        dup_res = self.client.get(f"/attend/{course['id']}?type=Lecture&token={token}")
        self.assertEqual(dup_res.status_code, 200)
        self.assertIn(b"Attendance Already Recorded", dup_res.data)

        # 5. Student checks personal attendance tab
        att_tab_res = self.client.get(f"/courses/{course['id']}/attendance")
        self.assertEqual(att_tab_res.status_code, 200)
        self.assertIn(b"My Attendance Record", att_tab_res.data)
        self.assertIn(b"100.0%", att_tab_res.data)

    def test_bulk_manual_attendance_and_csv_export(self):
        """Test teacher bulk manual attendance marking and CSV export."""
        self.login("kishan", "password123")
        conn = app.get_db()
        course = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()
        conn.close()

        # Bulk mark for student1 (B26DS001)
        res = self.client.post(f"/courses/{course['id']}/attendance/manual-bulk", data={
            "session_type": "Lab",
            "custom_date": "2026-09-01",
            "manual_identifiers": "B26DS001, unknown_student"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"1 marked successfully", res.data)

        # Export CSV
        csv_res = self.client.get(f"/courses/{course['id']}/attendance/export-csv")
        self.assertEqual(csv_res.status_code, 200)
        self.assertEqual(csv_res.mimetype, "text/csv")
        self.assertIn(b"Roll Number", csv_res.data)
        self.assertIn(b"Attendance Percentage", csv_res.data)
        self.assertIn(b"B26DS001", csv_res.data)

        # 5. Teacher inspects student detail API
        conn = app.get_db()
        st_user = conn.execute("SELECT id FROM users WHERE username = 'student1'").fetchone()
        conn.close()
        detail_res = self.client.get(f"/api/courses/{course['id']}/attendance/student/{st_user['id']}")
        self.assertEqual(detail_res.status_code, 200)
        detail_data = detail_res.get_json()
        self.assertIn("logs", detail_data)
        self.assertIn("attended_count", detail_data)

    def test_coteacher_attendance_log_access(self):
        """Test that a co-teacher / TA can open attendance management page and query student logs without errors."""
        conn = app.get_db()
        course = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()
        
        # Create a user with role='student' who is added as a 'ta' / 'co-teacher' in course_enrollments
        conn.execute("""
            INSERT INTO users (username, display_name, email, password_hash, role, created_at)
            VALUES ('coteacher_sam', 'Sam Wilson', 'sam@iitbhilai.ac.in', 'dummy', 'student', datetime('now'))
        """)
        sam_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("""
            INSERT INTO course_enrollments (course_id, user_id, role, enrolled_at)
            VALUES (?, ?, 'ta', datetime('now'))
        """, (course["id"], sam_id))
        st_user = conn.execute("SELECT id FROM users WHERE username = 'student1'").fetchone()
        conn.commit()
        conn.close()

        # Login as coteacher_sam
        self.login("coteacher_sam", "password123")
        
        # Override session to simulate coteacher_sam
        with self.client.session_transaction() as sess:
            sess["user_id"] = sam_id
            sess["username"] = "coteacher_sam"
            sess["role"] = "student"

        # 1. Co-teacher opens /courses/<id>/attendance - should render teacher roster view (HTTP 200), not throw 500 UndefinedError
        res = self.client.get(f"/courses/{course['id']}/attendance")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Class Attendance Management", res.data)
        self.assertIn(b"Launch QR Projector", res.data)

        # 2. Co-teacher queries student attendance detail API
        detail_res = self.client.get(f"/api/courses/{course['id']}/attendance/student/{st_user['id']}")
        self.assertEqual(detail_res.status_code, 200)
        detail_json = detail_res.get_json()
        self.assertIn("student", detail_json)
        self.assertIn("logs", detail_json)
        self.assertIn("attendance_pct", detail_json)

        # 3. Co-teacher can fetch dynamic token for projector
        token_res = self.client.get(f"/api/attendance/token/{course['id']}?type=Lecture")
        self.assertEqual(token_res.status_code, 200)
        self.assertIn("token", token_res.get_json())

    def test_exam_lockdown_auto_expire_and_locked_submission(self):
        """Test that exam lockdown automatically lifts when end_time passes, and submissions cannot be modified after end_time."""
        self.login("kishan", "password123")
        conn = app.get_db()
        course = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()

        now = datetime.now()
        start_str = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        end_str = (now + timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S")

        conn.execute("""
            INSERT INTO coursework (course_id, type, title, description, points, start_time, end_time, allowed_types, allow_multiple, allow_late, is_exam_mode, created_by, created_at)
            VALUES (?, 'exam', 'Quick Expiring Exam', 'Testing auto expiration', 100, ?, ?, 'zip', 1, 0, 1, 1, ?)
        """, (course["id"], start_str, end_str, start_str))
        exam_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE courses SET active_exam_id = ? WHERE id = ?", (exam_id, course["id"]))
        conn.commit()
        conn.close()

        # Student logs in
        self.logout()
        self.login("student1", "student123")

        # 1. While exam is ongoing, lockdown is active
        conn = app.get_db()
        st_user = conn.execute("SELECT id FROM users WHERE username = 'student1'").fetchone()
        conn.close()
        lockdown = app.get_active_exam_lockdown_for_student(st_user["id"])
        self.assertIsNotNone(lockdown)
        self.assertEqual(lockdown["coursework_id"], exam_id)

        # 2. Student submits a solution while active
        mem_zip = io.BytesIO()
        with zipfile.ZipFile(mem_zip, mode="w") as zf:
            zf.writestr("solution.c", "int main(){ return 0; }")
        mem_zip.seek(0)
        submit_res = self.client.post(f"/courses/{course['id']}/exam/{exam_id}/submit", data={
            "exam_file": (mem_zip, "B26DS001_sol.zip"),
            "lab_name": "Lab 1"
        }, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn(b"Official Digital Submission Receipt", submit_res.data)

        # 3. Wait until exam expires
        time.sleep(3)

        # Lockdown must now be automatically disabled in DB and return None
        lockdown_after = app.get_active_exam_lockdown_for_student(st_user["id"])
        self.assertIsNone(lockdown_after)

        # DB must have is_exam_mode=0 and active_exam_id=NULL
        conn = app.get_db()
        cw_row = conn.execute("SELECT is_exam_mode FROM coursework WHERE id = ?", (exam_id,)).fetchone()
        c_row = conn.execute("SELECT active_exam_id FROM courses WHERE id = ?", (course["id"],)).fetchone()
        conn.close()
        self.assertEqual(cw_row["is_exam_mode"], 0)
        self.assertIsNone(c_row["active_exam_id"])

        # 4. Attempt to modify submission after exam ended - must be strictly rejected
        mem_zip2 = io.BytesIO()
        with zipfile.ZipFile(mem_zip2, mode="w") as zf:
            zf.writestr("solution_modified.c", "int main(){ return 1; }")
        mem_zip2.seek(0)
        late_submit_res = self.client.post(f"/courses/{course['id']}/exam/{exam_id}/submit", data={
            "exam_file": (mem_zip2, "B26DS001_sol2.zip"),
            "lab_name": "Lab 1"
        }, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn(b"The exam deadline has passed. Modifying or re-submitting after the exam has ended is strictly locked", late_submit_res.data)

    def test_role_management_and_coteacher_workflow(self):
        """Test user role modification by admin/teacher, default student roles, and co-teacher workflows."""
        # 1. New user registration defaults to student
        self.client.post("/register", data={
            "full_name": "Test User Alpha",
            "roll_number": "B26CS777",
            "email": "b26cs777@iitbhilai.ac.in",
            "password": "password123",
            "confirm_password": "password123",
            "role": "student"
        })
        conn = app.get_db()
        user_alpha = conn.execute("SELECT * FROM users WHERE username = 'b26cs777'").fetchone()
        conn.close()
        self.assertIsNotNone(user_alpha)
        self.assertEqual(user_alpha["role"], "student")

        # 2. Student cannot access /admin/users
        self.login("b26cs777", "password123")
        res_stud = self.client.get("/admin/users", follow_redirects=True)
        self.assertIn(b"Administrator privileges required", res_stud.data)
        self.logout()

        # 3. User directory is shown ONLY for admin; professor does NOT see or access it
        self.login("kishan", "password123")
        res_users = self.client.get("/admin/users", follow_redirects=True)
        self.assertIn(b"Administrator privileges required", res_users.data)

        # Professor navbar does NOT contain User Directory
        dash_res = self.client.get("/dashboard")
        self.assertNotIn(b"User Directory", dash_res.data)

        # BUT the professor CAN change the role of registered students
        # Teacher promotes registered student to teacher
        res_promote = self.client.post(f"/teacher/students/{user_alpha['id']}/role", data={"role": "teacher"}, follow_redirects=True)
        self.assertIn(b"Updated", res_promote.data)

        conn = app.get_db()
        user_alpha = conn.execute("SELECT * FROM users WHERE id = ?", (user_alpha["id"],)).fetchone()
        conn.close()
        self.assertEqual(user_alpha["role"], "teacher")

        # Teacher demotes teacher back to student
        res_demote = self.client.post(f"/teacher/students/{user_alpha['id']}/role", data={"role": "student"}, follow_redirects=True)
        self.assertIn(b"Updated", res_demote.data)

        conn = app.get_db()
        user_alpha = conn.execute("SELECT * FROM users WHERE id = ?", (user_alpha["id"],)).fetchone()
        conn.close()
        self.assertEqual(user_alpha["role"], "student")

        # Teacher promotes student to teacher again
        res_promote2 = self.client.post(f"/teacher/students/{user_alpha['id']}/role", data={"role": "teacher"}, follow_redirects=True)
        self.assertIn(b"Updated", res_promote2.data)

        conn = app.get_db()
        user_alpha = conn.execute("SELECT * FROM users WHERE id = ?", (user_alpha["id"],)).fetchone()
        conn.close()
        self.assertEqual(user_alpha["role"], "teacher")

        # Teacher cannot promote someone to admin (only Superadmin can)
        res_promote_admin = self.client.post(f"/admin/users/{user_alpha['id']}/role", data={"role": "admin"}, follow_redirects=True)
        self.assertIn(b"Only an administrator can assign the Administrator role", res_promote_admin.data)
        self.logout()

        # 4. Student joining class with class code defaults to student even if attempting to request 'ta'
        conn = app.get_db()
        course = conn.execute("SELECT * FROM courses WHERE code = 'CSL100'").fetchone()
        conn.close()

        self.client.post("/register", data={
            "full_name": "Test User Beta",
            "roll_number": "B26CS888",
            "email": "b26cs888@iitbhilai.ac.in",
            "password": "password123",
            "confirm_password": "password123",
            "role": "student"
        })
        self.login("b26cs888", "password123")
        join_res = self.client.post("/courses/join", data={
            "join_code": course["join_code"],
            "enrollment_role": "ta"  # Student attempts to join as TA
        }, follow_redirects=True)
        self.assertIn(b"as Student", join_res.data)

        conn = app.get_db()
        beta_user = conn.execute("SELECT id FROM users WHERE username = 'b26cs888'").fetchone()
        beta_enr = conn.execute("SELECT role FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course["id"], beta_user["id"])).fetchone()
        conn.close()
        self.assertEqual(beta_enr["role"], "student")
        self.logout()

        # 5. Teacher (user_alpha) joining class can choose to join as Co-Teacher ('ta')
        self.login("b26cs777", "password123")
        join_res2 = self.client.post("/courses/join", data={
            "join_code": course["join_code"],
            "enrollment_role": "ta"
        }, follow_redirects=True)
        self.assertIn(b"as Co-Teacher", join_res2.data)

        conn = app.get_db()
        alpha_enr = conn.execute("SELECT role FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course["id"], user_alpha["id"])).fetchone()
        conn.close()
        self.assertEqual(alpha_enr["role"], "ta")
        self.logout()

        # 6. Primary teacher manages roles within course people roster
        self.login("kishan", "password123")

        # Promote beta student to Co-Teacher
        res_toggle = self.client.post(f"/courses/{course['id']}/people/{beta_user['id']}/role", data={"role": "ta"}, follow_redirects=True)
        self.assertIn(b"Updated Test User Beta&#39;s role in CSL100 to Co-Teacher", res_toggle.data)

        conn = app.get_db()
        beta_enr2 = conn.execute("SELECT role FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course["id"], beta_user["id"])).fetchone()
        conn.close()
        self.assertEqual(beta_enr2["role"], "ta")

        # Demote beta back to Student
        res_toggle2 = self.client.post(f"/courses/{course['id']}/people/{beta_user['id']}/role", data={"role": "student"}, follow_redirects=True)
        self.assertIn(b"Updated Test User Beta&#39;s role in CSL100 to Student", res_toggle2.data)

        conn = app.get_db()
        beta_enr3 = conn.execute("SELECT role FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course["id"], beta_user["id"])).fetchone()
        conn.close()
        self.assertEqual(beta_enr3["role"], "student")

        # Add Co-Teacher by identifier
        res_add_ct = self.client.post(f"/courses/{course['id']}/people/add-coteacher", data={
            "identifier": "B26CS888"
        }, follow_redirects=True)
        self.assertIn(b"is now configured as Co-Teacher", res_add_ct.data)

        conn = app.get_db()
        beta_enr4 = conn.execute("SELECT role FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course["id"], beta_user["id"])).fetchone()
        conn.close()
        self.assertEqual(beta_enr4["role"], "ta")

        # Cannot demote primary teacher
        res_demote_primary = self.client.post(f"/courses/{course['id']}/people/{course['teacher_id']}/role", data={"role": "student"}, follow_redirects=True)
        self.assertIn(b"The primary course instructor cannot be demoted to student", res_demote_primary.data)

        # Cannot remove primary teacher
        res_remove_primary = self.client.post(f"/courses/{course['id']}/people/remove/{course['teacher_id']}", follow_redirects=True)
        self.assertIn(b"Cannot remove the primary course instructor", res_remove_primary.data)

    def test_canvas_weighted_grading_and_bulk_import(self):
        """Test Canvas-inspired weighted grading out of 100, bulk grade import, class code privacy, and student scorecards."""
        conn = app.get_db()
        course = conn.execute("SELECT * FROM courses WHERE code = 'CSL100'").fetchone()
        student = conn.execute("SELECT * FROM users WHERE username = 'student1'").fetchone()
        cw = conn.execute("SELECT * FROM coursework WHERE course_id = ? AND type = 'assignment' LIMIT 1", (course["id"],)).fetchone()
        conn.close()

        # 1. Privacy Checks: Student cannot see class code or turned-in counts
        self.login("student1", "student123")
        stream_res = self.client.get(f"/courses/{course['id']}/stream")
        # Class invitation card must not be shown to student
        self.assertNotIn(b"stream-code-box", stream_res.data)
        self.assertNotIn(b"Share this code with students", stream_res.data)

        people_res = self.client.get(f"/courses/{course['id']}/people")
        self.assertNotIn(b"Class Invitation Code", people_res.data)
        # Turned In column must not be shown in people roster
        self.assertNotIn(b"<th>Turned In</th>", people_res.data)
        self.assertNotIn(b"badge-turned-in", people_res.data)
        self.logout()

        # 2. Instructor can see Class Code
        self.login("kishan", "password123")
        inst_people = self.client.get(f"/courses/{course['id']}/people")
        self.assertIn(b"Class Invitation Code", inst_people.data)
        self.assertIn(course["join_code"].encode(), inst_people.data)

        # 3. Canvas Weighted Grade Scheme & Default Categories
        grades_res = self.client.get(f"/courses/{course['id']}/grades")
        self.assertEqual(grades_res.status_code, 200)
        self.assertIn(b"Course Gradebook Matrix & Assessment Weights", grades_res.data)
        self.assertIn(b"Assignments &amp; Quizzes", grades_res.data)
        self.assertIn(b"Attendance", grades_res.data)
        self.assertIn(b"100.0%", grades_res.data)

        # Update category weights (e.g. End Sem 20%, Mid Sem 20%, Lab 10%, Assignments 45%, Attendance 5%)
        conn = app.get_db()
        cats = conn.execute("SELECT id, name, weight FROM course_grading_categories WHERE course_id = ? ORDER BY id ASC", (course["id"],)).fetchall()
        conn.close()

        cat_ids = [str(c["id"]) for c in cats]
        cat_names = [c["name"] for c in cats]
        cat_weights = ["40.0", "15.0", "20.0", "20.0", "5.0"]  # Sum = 100.0
        data = {
            "cat_id": cat_ids,
            "name": cat_names,
            "weight": cat_weights
        }
        res_weights = self.client.post(f"/courses/{course['id']}/grades/categories", data=data, follow_redirects=True)
        self.assertIn(b"Course grading scheme and category weights saved successfully", res_weights.data)

        # 4. Download pre-filled grading template (CSV)
        tpl_res = self.client.get(f"/courses/{course['id']}/grades/template")
        self.assertEqual(tpl_res.status_code, 200)
        self.assertEqual(tpl_res.mimetype, "text/csv")
        self.assertIn(b"Roll Number", tpl_res.data)
        self.assertIn(student["roll_number"].encode(), tpl_res.data)

        # 5. Bulk Grade Import from CSV
        csv_content = (
            f'Roll Number,Student Name,Email,"{cw["title"]}"\n'
            f'{student["roll_number"]},{student["display_name"]},{student["email"]},88.5\n'
        ).encode("utf-8")

        import_res = self.client.post(f"/courses/{course['id']}/grades/import", data={
            "grades_file": (io.BytesIO(csv_content), "csl100_grades.csv")
        }, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn(b"Bulk Grade Import Successful", import_res.data)

        # Verify grade saved in database
        conn = app.get_db()
        sub_row = conn.execute("SELECT grade, status FROM submissions WHERE coursework_id = ? AND student_id = ?", (cw["id"], student["id"])).fetchone()
        conn.close()
        self.assertIsNotNone(sub_row)
        self.assertEqual(sub_row["grade"], 88.5)
        self.assertEqual(sub_row["status"], "graded")

        # 6. Quick Grade Update via SpeedGrader endpoint
        quick_res = self.client.post(f"/courses/{course['id']}/grades/quick-update", data={
            "student_id": student["id"],
            "coursework_id": cw["id"],
            "grade": "94.0",
            "feedback": "Outstanding pointer implementation!"
        }, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(quick_res.status_code, 200)
        self.assertTrue(quick_res.get_json()["success"])

        conn = app.get_db()
        sub_updated = conn.execute("SELECT grade, feedback FROM submissions WHERE coursework_id = ? AND student_id = ?", (cw["id"], student["id"])).fetchone()
        conn.close()
        self.assertEqual(sub_updated["grade"], 94.0)
        self.assertEqual(sub_updated["feedback"], "Outstanding pointer implementation!")

        # 7. Student Personal Scorecard View (Weighted Evaluation, No Canvas Standard)
        self.logout()
        self.login("student1", "student123")
        student_grades = self.client.get(f"/courses/{course['id']}/grades")
        self.assertEqual(student_grades.status_code, 200)
        self.assertIn(b"Student Grade Report \xe2\x80\xa2 Weighted Evaluation", student_grades.data)
        self.assertNotIn(b"Canvas Standard", student_grades.data)
        self.assertIn(b"Weighted Total (Out of 100)", student_grades.data)
        self.assertIn(b"Assessment Weighting Scheme", student_grades.data)
        self.assertIn(b"Outstanding pointer implementation!", student_grades.data)

    def test_in_app_pdf_opener_and_inline_routes(self):
        """Test In-App PDF Opener inline delivery routes for attachments, submissions, and locker files."""
        self.login("kishan", "password123")
        conn = app.get_db()
        teacher = conn.execute("SELECT id FROM users WHERE username = 'kishan'").fetchone()
        course = conn.execute("SELECT id FROM courses WHERE teacher_id = ?", (teacher["id"],)).fetchone()
        student = conn.execute("SELECT id, username, roll_number, display_name FROM users WHERE username = 'student1'").fetchone()

        # Create dummy PDF file
        pdf_bytes = b"%PDF-1.4 1 0 obj << /Type /Catalog >> endobj xref 0 1 0000000000 65535 f trailer << /Root 1 0 R >> %%EOF"

        # 1. Coursework Attachment
        cw_res = conn.execute("""
            INSERT INTO coursework (course_id, created_by, title, description, points, type, created_at)
            VALUES (?, ?, 'Lab 4 PDF Brief', 'Follow instructions in attached PDF', 50, 'assignment', datetime('now'))
        """, (course["id"], teacher["id"]))
        cw_id = cw_res.lastrowid

        att_path = os.path.join(str(app.ATTACHMENTS_DIR), f"test_brief_{cw_id}.pdf")
        with open(att_path, "wb") as f:
            f.write(pdf_bytes)

        att_res = conn.execute("""
            INSERT INTO coursework_attachments (coursework_id, original_filename, stored_filename, file_path, file_size, uploaded_at)
            VALUES (?, 'Lab4_Instructions.pdf', ?, ?, ?, datetime('now'))
        """, (cw_id, f"test_brief_{cw_id}.pdf", att_path, len(pdf_bytes)))
        att_id = att_res.lastrowid

        # 2. Student PDF Submission
        sub_path = os.path.join(str(app.SUBMISSIONS_DIR), f"test_sub_{cw_id}.pdf")
        with open(sub_path, "wb") as f:
            f.write(pdf_bytes)

        sub_res = conn.execute("""
            INSERT INTO submissions (coursework_id, student_id, roll_number, student_name, original_filename, stored_filename, file_path, file_size, sha256, receipt_token, submitted_at)
            VALUES (?, ?, ?, ?, 'Student_Solution.pdf', ?, ?, ?, 'dummyhash', 'receipt_pdf_123', datetime('now'))
        """, (cw_id, student["id"], student["roll_number"], student["display_name"], f"test_sub_{cw_id}.pdf", sub_path, len(pdf_bytes)))
        sub_id = sub_res.lastrowid

        # 3. Student Locker PDF
        locker_path = os.path.join(str(app.LOCKERS_DIR), f"test_locker_{student['id']}.pdf")
        with open(locker_path, "wb") as f:
            f.write(pdf_bytes)

        lock_res = conn.execute("""
            INSERT INTO student_locker_files (user_id, original_filename, stored_filename, file_path, file_size, mime_type, uploaded_at)
            VALUES (?, 'Research_Paper.pdf', ?, ?, ?, 'application/pdf', datetime('now'))
        """, (student["id"], f"test_locker_{student['id']}.pdf", locker_path, len(pdf_bytes)))
        lock_id = lock_res.lastrowid
        conn.commit()
        conn.close()

        # Teacher tests inline viewing of attachment
        res_att = self.client.get(f"/view/attachment/{att_id}")
        self.assertEqual(res_att.status_code, 200)
        self.assertEqual(res_att.content_type, "application/pdf")
        self.assertIn("inline", res_att.headers.get("Content-Disposition", ""))

        # Teacher tests inline viewing of student submission
        res_sub_teacher = self.client.get(f"/view/submission/{sub_id}")
        self.assertEqual(res_sub_teacher.status_code, 200)
        self.assertEqual(res_sub_teacher.content_type, "application/pdf")
        self.assertIn("inline", res_sub_teacher.headers.get("Content-Disposition", ""))

        # Switch to student
        self.logout()
        self.login("student1", "student123")

        # Student tests inline viewing of their own submission
        res_sub_student = self.client.get(f"/view/submission/{sub_id}")
        self.assertEqual(res_sub_student.status_code, 200)
        self.assertEqual(res_sub_student.content_type, "application/pdf")

        # Student tests inline viewing of locker file
        res_lock_view = self.client.get(f"/locker/view/{lock_id}")
        self.assertEqual(res_lock_view.status_code, 200)
        self.assertEqual(res_lock_view.content_type, "application/pdf")
        self.assertIn("inline", res_lock_view.headers.get("Content-Disposition", ""))

        # Student tests locker_preview API returning type 'pdf'
        res_lock_prev = self.client.get(f"/locker/preview/{lock_id}")
        self.assertEqual(res_lock_prev.status_code, 200)
        prev_data = res_lock_prev.get_json()
        self.assertEqual(prev_data["type"], "pdf")
        self.assertEqual(prev_data["filename"], "Research_Paper.pdf")
        self.assertIn(f"/locker/view/{lock_id}", prev_data["url"])

    def test_brand_assets_and_logo_downloads(self):
        """Test brand assets page and downloads are restricted strictly to administrators."""
        self.logout()

        # 1. Unauthenticated request to /brand redirects to login
        res_anon = self.client.get("/brand")
        self.assertEqual(res_anon.status_code, 302)
        self.assertIn("/login", res_anon.headers.get("Location", ""))

        # 2. Student access to /brand is rejected (redirected to dashboard)
        self.login("student1", "student123")
        res_student = self.client.get("/brand", follow_redirects=True)
        self.assertIn(b"Administrator privileges required", res_student.data)
        res_down_student = self.client.get("/brand/download/logo-png", follow_redirects=True)
        self.assertIn(b"Administrator privileges required", res_down_student.data)
        self.logout()

        # 3. Admin access succeeds
        self.login("admin", "admin@accl")
        res = self.client.get("/brand")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Hoodle Brand Assets", res.data)
        self.assertIn(b"Download Complete Brand Kit", res.data)

        # 4. Test downloading logo & icon PNG (Transparent)
        res_png = self.client.get("/brand/download/logo-png")
        self.assertEqual(res_png.status_code, 200)
        self.assertIn("image/png", res_png.content_type)
        self.assertIn("attachment", res_png.headers.get("Content-Disposition", ""))

        res_icon = self.client.get("/brand/download/icon-png")
        self.assertEqual(res_icon.status_code, 200)
        self.assertIn("image/png", res_icon.content_type)

        # 5. Test downloading logo SVG
        res_svg = self.client.get("/brand/download/logo-svg")
        self.assertEqual(res_svg.status_code, 200)
        self.assertIn("svg", res_svg.content_type)
        self.assertIn("Hoodle_Logo_Full.svg", res_svg.headers.get("Content-Disposition", ""))

        # 6. Test downloading brand kit ZIP
        res_kit = self.client.get("/brand/download/kit")
        self.assertEqual(res_kit.status_code, 200)
        self.assertIn("zip", res_kit.content_type)
        self.assertIn("Hoodle_Brand_Kit.zip", res_kit.headers.get("Content-Disposition", ""))

        # 7. Test invalid asset name returns 404
        res_404 = self.client.get("/brand/download/non-existent-asset")
        self.assertEqual(res_404.status_code, 404)
        self.logout()

    def test_exam_live_submissions_telemetry_api(self):
        """Test the live 1-second exam submissions telemetry API."""
        conn = app.get_db()
        course = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()
        cw = conn.execute("""
            SELECT id FROM coursework 
            WHERE course_id = ? AND is_exam_mode = 1 
            LIMIT 1
        """, (course["id"],)).fetchone()
        
        if not cw:
            # Create an exam coursework
            conn.execute("""
                INSERT INTO coursework (course_id, title, type, points, is_exam_mode, start_time, end_time, allowed_types, created_by, created_at)
                VALUES (?, 'Final Lab Exam', 'exam', 100, 1, datetime('now', '-10 minutes'), datetime('now', '+50 minutes'), 'zip', 1, datetime('now'))
            """, (course["id"],))
            cw_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
        else:
            cw_id = cw["id"]
        conn.close()

        # 1. Test teacher access -> 200 OK with on-time and total submission metrics
        self.login("kishan", "password123")
        res = self.client.get(f"/api/courses/{course['id']}/coursework/{cw_id}/live-submissions")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("total_submissions", data)
        self.assertIn("on_time_count", data)
        self.assertIn("late_count", data)
        self.assertIn("pending_count", data)
        self.assertIn("submissions", data)

        # 2. Test student access -> 403 Forbidden (student privacy preservation)
        self.logout()
        self.login("student1", "student123")
        res_st = self.client.get(f"/api/courses/{course['id']}/coursework/{cw_id}/live-submissions")
        self.assertEqual(res_st.status_code, 403)
        self.logout()

    def test_admin_create_teacher(self):
        """Test admin registering a new teacher account from User Directory."""
        self.logout()
        self.login("admin", "admin@accl")

        res = self.client.post("/admin/teachers/create", data={
            "display_name": "Dr. Sunita Rao",
            "username": "srao",
            "roll_number": "FAC099",
            "email": "srao@iitbhilai.ac.in",
            "password": "teacherpass123",
            "role": "teacher"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Teacher account for Dr. Sunita Rao created successfully", res.data)

        # Verify teacher can log in
        self.logout()
        res_login = self.login("srao", "teacherpass123")
        self.assertEqual(res_login.status_code, 200)
        self.assertIn(b"Dr. Sunita Rao", res_login.data)
        self.logout()

    def test_course_invitations_and_student_join(self):
        """Test bulk student invitation by roll number, signup auto-linking, and home screen join."""
        conn = app.get_db()
        course = conn.execute("SELECT id, code, title FROM courses LIMIT 1").fetchone()
        course_id = course["id"]

        # Ensure student1 is not already enrolled for this test
        st1 = conn.execute("SELECT id FROM users WHERE username = 'student1'").fetchone()
        conn.execute("DELETE FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course_id, st1["id"]))
        conn.commit()
        conn.close()

        # 1. Teacher bulk invites: student1 (registered) and B26CS777 (unregistered)
        self.logout()
        self.login("kishan", "password123")
        res_inv = self.client.post(f"/courses/{course_id}/invite", data={
            "students_input": "B26DS001, B26CS777"
        }, follow_redirects=True)
        self.assertEqual(res_inv.status_code, 200)
        self.assertIn(b"invitation(s) saved", res_inv.data)
        self.logout()

        # Verify invitations exist in database
        conn = app.get_db()
        inv_reg = conn.execute("SELECT * FROM course_invitations WHERE course_id = ? AND UPPER(student_roll) = 'B26DS001'", (course_id,)).fetchone()
        inv_unreg = conn.execute("SELECT * FROM course_invitations WHERE course_id = ? AND UPPER(student_roll) = 'B26CS777'", (course_id,)).fetchone()
        conn.close()
        self.assertIsNotNone(inv_reg)
        self.assertEqual(inv_reg["student_id"], st1["id"])
        self.assertIsNotNone(inv_unreg)
        self.assertIsNone(inv_unreg["student_id"])

        # 2. Registered student (student1) logs in: home screen shows invitation banner and Join button
        self.login("student1", "student123")
        res_dash = self.client.get("/dashboard")
        self.assertEqual(res_dash.status_code, 200)
        self.assertIn(b"Pending Course Invitations", res_dash.data)
        self.assertIn(b"Accept &amp; Join Class", res_dash.data)

        # Student clicks Accept & Join Class
        res_accept = self.client.post(f"/invitations/{inv_reg['id']}/accept", follow_redirects=True)
        self.assertEqual(res_accept.status_code, 200)
        self.assertIn(b"Welcome! You have successfully joined", res_accept.data)

        # Verify enrolled in database and invitation accepted
        conn = app.get_db()
        enrolled = conn.execute("SELECT * FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course_id, st1["id"])).fetchone()
        inv_updated = conn.execute("SELECT status FROM course_invitations WHERE id = ?", (inv_reg["id"],)).fetchone()
        conn.close()
        self.assertIsNotNone(enrolled)
        self.assertEqual(inv_updated["status"], "accepted")
        self.logout()

        # 3. Unregistered student signs up with roll number B26CS777
        res_reg = self.client.post("/register", data={
            "full_name": "Pooja Verma",
            "roll_number": "B26CS777",
            "email": "b26cs777@iitbhilai.ac.in",
            "password": "poojapassword",
            "confirm_password": "poojapassword",
            "role": "student"
        }, follow_redirects=True)
        self.assertIn(b"Registration successful", res_reg.data)

        # Verify invitation was auto-linked on signup
        conn = app.get_db()
        new_student = conn.execute("SELECT id FROM users WHERE username = 'b26cs777'").fetchone()
        inv_linked = conn.execute("SELECT * FROM course_invitations WHERE id = ?", (inv_unreg["id"],)).fetchone()
        conn.close()
        self.assertIsNotNone(new_student)
        self.assertEqual(inv_linked["student_id"], new_student["id"])

        # Pooja logs in and sees pending invitation on Home Screen
        self.login("b26cs777", "poojapassword")
        res_pooja_dash = self.client.get("/dashboard")
        self.assertEqual(res_pooja_dash.status_code, 200)
        self.assertIn(b"Pending Course Invitations", res_pooja_dash.data)
        self.assertIn(b"Accept &amp; Join Class", res_pooja_dash.data)

        # Pooja accepts invitation
        res_pooja_accept = self.client.post(f"/invitations/{inv_linked['id']}/accept", follow_redirects=True)
        self.assertEqual(res_pooja_accept.status_code, 200)
        self.assertIn(b"Welcome! You have successfully joined", res_pooja_accept.data)
        self.logout()

    def test_invitation_accept_by_token_link(self):
        """Test accepting course invitation directly via unique email token link."""
        conn = app.get_db()
        course = conn.execute("SELECT id, code, title FROM courses LIMIT 1").fetchone()
        token_str = "test_secure_token_12345"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            INSERT INTO course_invitations (course_id, invited_by, student_roll, student_email, token, status, sent_at)
            VALUES (?, 1, 'B26EE888', 'b26ee888@iitbhilai.ac.in', ?, 'pending', ?)
        """, (course["id"], token_str, now_str))
        conn.commit()
        conn.close()

        # Logged in user clicks link
        self.logout()
        self.login("student1", "student123")
        res = self.client.get(f"/invitations/accept/{token_str}", follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Successfully joined", res.data)
        self.logout()

    def test_registration_forces_student_role(self):
        """Verify public self-registration strictly assigns student role even if attacker specifies teacher/admin."""
        self.logout()
        res = self.client.post("/register", data={
            "full_name": "Attacker Role Test",
            "roll_number": "HACK999",
            "email": "hack999@test.com",
            "password": "password123",
            "confirm_password": "password123",
            "role": "teacher"  # Exploit attempt: try registering as teacher
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        conn = app.get_db()
        user = conn.execute("SELECT role FROM users WHERE username = 'hack999'").fetchone()
        conn.close()
        self.assertIsNotNone(user)
        self.assertEqual(user["role"], "student", "Registration must strictly force student role")

    def test_favicon_route(self):
        """Verify /favicon.ico route serves valid Hoodle PNG icon."""
        res = self.client.get("/favicon.ico")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "image/png")

    def test_security_headers(self):
        """Verify defensive HTTP security headers are injected."""
        res = self.client.get("/login")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(res.headers.get("X-Frame-Options"), "SAMEORIGIN")

    def test_admin_backup_endpoints_restricted(self):
        """Verify student cannot access or trigger admin backup/restore."""
        self.login("student1", "student123")
        res_get = self.client.get("/admin/backup", follow_redirects=True)
        self.assertIn(b"Administrator privileges required", res_get.data)

        res_post = self.client.post("/admin/backup/trigger", follow_redirects=True)
        self.assertIn(b"Administrator privileges required", res_post.data)
        self.logout()

        # Admin can access
        self.login("admin", "admin@accl")
        res_admin = self.client.get("/admin/backup")
        self.assertEqual(res_admin.status_code, 200)
        self.assertIn(b"Offsite Backup &amp; Disaster Recovery", res_admin.data)
        self.logout()

    def test_admin_backup_dynamic_settings(self):
        """Verify administrator can dynamically update remote backup host, user, path, and retention."""
        self.login("admin", "admin@accl")
        res = self.client.post("/admin/backup/settings", data={
            "remote_host": "backup-server.local",
            "remote_user": "accladmin",
            "remote_dir": "/storage/accl_backups",
            "retention_days": "45"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"backup-server.local", res.data)

    def test_exam_submission_locking_after_deadline(self):
        """Verify students cannot submit or modify exam work after the exam cutoff time has passed."""
        # Setup: Create an exam with end_time in the past
        past_time = (datetime.now() - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M")
        conn = app.get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO coursework (course_id, type, title, description, points, start_time, end_time, allowed_types, allow_multiple, allow_late, is_exam_mode, created_by, created_at)
            VALUES (1, 'exam', 'Past Midterm Exam', 'Strictly timed', 100, '2026-01-01T10:00', ?, 'zip', 1, 0, 1, 2, '2026-01-01 09:00:00')
        """, (past_time,))
        exam_id = c.lastrowid
        conn.commit()
        conn.close()

        # Student logs in and tries to submit a ZIP file after exam ended
        self.login("student1", "student123")
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("solution.c", "int main() { return 0; }")
        zip_buffer.seek(0)

        res = self.client.post(f"/courses/1/exam/{exam_id}/submit", data={
            "lab_name": "Lab 1",
            "exam_file": (zip_buffer, "solution.zip")
        }, follow_redirects=True)

        self.assertIn(b"The exam deadline has passed", res.data)
        self.assertIn(b"strictly locked", res.data)

        # Confirm nothing was saved in database
        conn = app.get_db()
        sub = conn.execute("SELECT * FROM submissions WHERE coursework_id = ?", (exam_id,)).fetchone()
        conn.close()
        self.assertIsNone(sub)
        self.logout()

    def test_exam_resubmission_during_active_time(self):
        """Verify resubmissions are allowed during active exam time ONLY if allow_multiple=1."""
        # Create active exam with allow_multiple = 1
        future_time = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        past_start = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        conn = app.get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO coursework (course_id, type, title, description, points, start_time, end_time, allowed_types, allow_multiple, allow_late, is_exam_mode, created_by, created_at)
            VALUES (1, 'exam', 'Active Midterm Multi', 'Timed active', 100, ?, ?, 'zip', 1, 0, 1, 2, '2026-01-01 09:00:00')
        """, (past_start, future_time))
        exam_multi_id = c.lastrowid

        # Create active exam with allow_multiple = 0 (single submission)
        c.execute("""
            INSERT INTO coursework (course_id, type, title, description, points, start_time, end_time, allowed_types, allow_multiple, allow_late, is_exam_mode, created_by, created_at)
            VALUES (1, 'exam', 'Active Midterm Single', 'Timed active', 100, ?, ?, 'zip', 0, 0, 1, 2, '2026-01-01 09:00:00')
        """, (past_start, future_time))
        exam_single_id = c.lastrowid
        conn.commit()
        conn.close()

        self.login("student1", "student123")

        # 1. Multi-submission exam: First submission -> version 1
        zip1 = io.BytesIO()
        with zipfile.ZipFile(zip1, "w") as zf:
            zf.writestr("code1.c", "int v1() { return 1; }")
        zip1.seek(0)
        res1 = self.client.post(f"/courses/1/exam/{exam_multi_id}/submit", data={
            "lab_name": "Lab 1",
            "exam_file": (zip1, "code1.zip")
        }, follow_redirects=True)
        self.assertIn(b"Digital Submission Receipt", res1.data)

        # Multi-submission exam: Second submission during active time -> version 2
        zip2 = io.BytesIO()
        with zipfile.ZipFile(zip2, "w") as zf:
            zf.writestr("code2.c", "int v2() { return 2; }")
        zip2.seek(0)
        res2 = self.client.post(f"/courses/1/exam/{exam_multi_id}/submit", data={
            "lab_name": "Lab 1",
            "exam_file": (zip2, "code2.zip")
        }, follow_redirects=True)
        self.assertIn(b"Digital Submission Receipt", res2.data)

        conn = app.get_db()
        sub_multi = conn.execute("SELECT * FROM submissions WHERE coursework_id = ?", (exam_multi_id,)).fetchone()
        self.assertEqual(sub_multi["version"], 2)

        # 2. Single-submission exam: First submission -> success
        zip3 = io.BytesIO()
        with zipfile.ZipFile(zip3, "w") as zf:
            zf.writestr("code_single.c", "int main() { return 0; }")
        zip3.seek(0)
        res3 = self.client.post(f"/courses/1/exam/{exam_single_id}/submit", data={
            "lab_name": "Lab 1",
            "exam_file": (zip3, "code_single.zip")
        }, follow_redirects=True)
        self.assertIn(b"Digital Submission Receipt", res3.data)

        # Single-submission exam: Second submission -> REJECTED
        zip4 = io.BytesIO()
        with zipfile.ZipFile(zip4, "w") as zf:
            zf.writestr("code_single_revised.c", "int main() { return 9; }")
        zip4.seek(0)
        res4 = self.client.post(f"/courses/1/exam/{exam_single_id}/submit", data={
            "lab_name": "Lab 1",
            "exam_file": (zip4, "code_single_revised.zip")
        }, follow_redirects=True)
        self.assertIn(b"Single submission policy", res4.data)

        sub_single = conn.execute("SELECT * FROM submissions WHERE coursework_id = ?", (exam_single_id,)).fetchone()
        self.assertEqual(sub_single["version"], 1)
        conn.close()
        self.logout()

    def test_exam_unsubmit_strictly_prohibited(self):
        """Verify student cannot unsubmit exam work under any circumstance."""
        conn = app.get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO coursework (course_id, type, title, description, points, is_exam_mode, created_by, created_at)
            VALUES (1, 'exam', 'Lockdown Lab Exam', 'Description', 100, 1, 2, '2026-01-01 09:00:00')
        """)
        exam_id = c.lastrowid
        c.execute("""
            INSERT INTO submissions (coursework_id, student_id, roll_number, student_name, status, original_filename, stored_filename, file_path, file_size, sha256, ip_address, submitted_at, version, is_late, late_minutes, receipt_token)
            VALUES (?, 3, '101', 'Test Student', 'turned_in', 'exam.zip', '101.zip', '/dummy', 1024, 'dummyhash', '127.0.0.1', '2026-01-01 10:00:00', 1, 0, 0, 'tok123')
        """, (exam_id,))
        conn.commit()
        conn.close()

        self.login("student1", "student123")
        res = self.client.post(f"/courses/1/coursework/{exam_id}/unsubmit", follow_redirects=True)
        self.assertIn(b"Submissions for exams cannot be unsubmitted", res.data)

        conn = app.get_db()
        sub = conn.execute("SELECT * FROM submissions WHERE coursework_id = ?", (exam_id,)).fetchone()
        conn.close()
        self.assertEqual(sub["status"], "turned_in")
        self.logout()

    def test_coursework_unsubmit_locked_after_due_date(self):
        """Verify regular coursework cannot be unsubmitted after the due date has passed."""
        past_due = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
        conn = app.get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO coursework (course_id, type, title, description, points, due_date, created_by, created_at)
            VALUES (1, 'assignment', 'Past Due Homework', 'Desc', 100, ?, 2, '2026-01-01 09:00:00')
        """, (past_due,))
        cw_id = c.lastrowid
        c.execute("""
            INSERT INTO submissions (coursework_id, student_id, roll_number, student_name, status, original_filename, stored_filename, file_path, file_size, sha256, ip_address, submitted_at, version, is_late, late_minutes, receipt_token)
            VALUES (?, 3, '101', 'Test Student', 'turned_in', 'hw.c', 'hw.c', '/dummy', 500, 'hash123', '127.0.0.1', '2026-01-01 10:00:00', 1, 0, 0, 'tok456')
        """, (cw_id,))
        conn.commit()
        conn.close()

        self.login("student1", "student123")
        res = self.client.post(f"/courses/1/coursework/{cw_id}/unsubmit", follow_redirects=True)
        self.assertIn(b"Submission deadline has passed", res.data)

        conn = app.get_db()
        sub = conn.execute("SELECT * FROM submissions WHERE coursework_id = ?", (cw_id,)).fetchone()
        conn.close()
        self.assertEqual(sub["status"], "turned_in")
        self.logout()

    def test_teacher_can_edit_coursework_timings_and_assignments(self):
        """Verify teacher has full access to update due_date, start_time, end_time, title, and points of assigned work."""
        orig_due = "2026-10-15T23:59"
        orig_start = "2026-10-15T14:00"
        orig_end = "2026-10-15T17:00"

        conn = app.get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO coursework (course_id, type, title, description, points, due_date, start_time, end_time, created_by, created_at)
            VALUES (1, 'exam', 'Original Exam Title', 'Original Desc', 100, ?, ?, ?, 2, '2026-01-01 09:00:00')
        """, (orig_due, orig_start, orig_end))
        cw_id = c.lastrowid
        conn.commit()
        conn.close()

        # Teacher logs in and updates timings + title + points
        self.login("kishan", "password123")
        new_due = "2026-12-31T23:59"
        new_start = "2026-12-31T10:00"
        new_end = "2026-12-31T12:00"
        res = self.client.post(f"/courses/1/coursework/{cw_id}/edit", data={
            "title": "Updated Exam Title",
            "description": "Updated instructions",
            "points": "150",
            "due_date": new_due,
            "start_time": new_start,
            "end_time": new_end,
            "allow_multiple": "1"
        }, follow_redirects=True)

        self.assertEqual(res.status_code, 200)
        self.assertIn(b"updated successfully", res.data)

        # Verify DB: title, points, and timings are all successfully updated
        conn = app.get_db()
        cw = conn.execute("SELECT * FROM coursework WHERE id = ?", (cw_id,)).fetchone()
        conn.close()
        self.assertEqual(cw["title"], "Updated Exam Title")
        self.assertEqual(cw["points"], 150)
        self.assertEqual(cw["due_date"], new_due)
        self.assertEqual(cw["start_time"], new_start)
        self.assertEqual(cw["end_time"], new_end)
        self.logout()

    def test_teacher_can_edit_all_class_settings(self):
        """Verify teacher can edit course code, title, section, description, and theme color."""
        self.login("kishan", "password123")
        res = self.client.post("/courses/1/settings", data={
            "code": "ACCL701",
            "title": "GPU Architecture & Deep Learning Acceleration",
            "section": "Section B (Ph.D)",
            "description": "Comprehensive lab course covering CUDA and TensorRT acceleration.",
            "theme_color": "emerald"
        }, follow_redirects=True)

        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Class settings updated successfully", res.data)

        # Verify DB
        conn = app.get_db()
        course = conn.execute("SELECT * FROM courses WHERE id = 1").fetchone()
        conn.close()
        self.assertEqual(course["code"], "ACCL701")
        self.assertEqual(course["title"], "GPU Architecture & Deep Learning Acceleration")
        self.assertEqual(course["section"], "Section B (Ph.D)")
        self.assertEqual(course["theme_color"], "emerald")
        self.logout()

    def test_quick_join_authenticated_user(self):
        """Verify authenticated student using /j/<join_code> is enrolled and redirected to stream."""
        conn = app.get_db()
        course = conn.execute("SELECT * FROM courses WHERE id = 1").fetchone()
        join_code = course["join_code"]
        conn.close()

        # Login as student
        self.login("student1", "student123")
        res = self.client.get(f"/j/{join_code}", follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        # Verify enrolled in DB
        conn = app.get_db()
        enroll = conn.execute("SELECT * FROM course_enrollments WHERE course_id = 1 AND user_id = 3").fetchone()
        conn.close()
        self.assertIsNotNone(enroll)
        self.assertEqual(enroll["role"], "student")
        self.logout()

    def test_quick_join_unauthenticated_user(self):
        """Verify unauthenticated user using /j/<join_code> is redirected to login with pending code stored."""
        conn = app.get_db()
        course = conn.execute("SELECT * FROM courses WHERE id = 1").fetchone()
        join_code = course["join_code"]
        conn.close()

        res = self.client.get(f"/j/{join_code}", follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Classroom Invitation", res.data)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("pending_join_code"), join_code.upper())

    def test_login_with_pending_join_code(self):
        """Verify logging in with pending_join_code completes enrollment and forwards to course stream."""
        conn = app.get_db()
        course = conn.execute("SELECT * FROM courses WHERE id = 1").fetchone()
        join_code = course["join_code"]
        # Ensure student1 is not yet enrolled
        conn.execute("DELETE FROM course_enrollments WHERE course_id = 1 AND user_id = 3")
        conn.commit()
        conn.close()

        # Visit short link as guest
        self.client.get(f"/j/{join_code}", follow_redirects=True)

        # Now log in as student1
        res = self.client.post("/login", data={
            "identifier": "student1",
            "password": "student123"
        }, follow_redirects=True)

        self.assertEqual(res.status_code, 200)
        self.assertIn(b"You have been enrolled in", res.data)

        # Verify DB enrollment
        conn = app.get_db()
        enroll = conn.execute("SELECT * FROM course_enrollments WHERE course_id = 1 AND user_id = 3").fetchone()
        conn.close()
        self.assertIsNotNone(enroll)
        self.logout()

    def test_register_with_pending_join_code(self):
        """Verify registering a new account with pending_join_code auto-logs in, enrolls, and opens course."""
        conn = app.get_db()
        course = conn.execute("SELECT * FROM courses WHERE id = 1").fetchone()
        join_code = course["join_code"]
        conn.close()

        # Visit short link as guest
        self.client.get(f"/j/{join_code}", follow_redirects=True)

        # Register new student
        res = self.client.post("/register", data={
            "full_name": "Test ShortLink Student",
            "roll_number": "B26TEST999",
            "email": "testshortlink@iitbhilai.ac.in",
            "password": "password123",
            "confirm_password": "password123"
        }, follow_redirects=True)

        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Welcome to", res.data)

        # Verify DB: student created and enrolled
        conn = app.get_db()
        user = conn.execute("SELECT * FROM users WHERE roll_number = 'B26TEST999'").fetchone()
        self.assertIsNotNone(user)
        enroll = conn.execute("SELECT * FROM course_enrollments WHERE course_id = 1 AND user_id = ?", (user["id"],)).fetchone()
        self.assertIsNotNone(enroll)
        conn.close()
        self.logout()

    def test_short_jump_routes(self):
        """Verify short jump routes /c/<id>, /cw/<id>, and /p."""
        self.login("kishan", "password123")

        # Test /c/1
        res = self.client.get("/c/1", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("/courses/1", res.location)

        # Test /cw/1
        res = self.client.get("/cw/1", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("/courses/1/coursework/1", res.location)

        # Test /p
        res = self.client.get("/p", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("/dashboard", res.location)

        self.logout()

    def test_student_guide_routes(self):
        """Verify /guide and /student-guide render successfully."""
        # Unauthenticated access
        res = self.client.get("/guide")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Hoodle LMS Student Usage &amp; Examination Guide", res.data)
        self.assertIn(b"Timed Lab Exams &amp; Strict Lockdown Mode", res.data)
        self.assertIn(b"01_signin_register.png", res.data)

        res2 = self.client.get("/student-guide")
        self.assertEqual(res2.status_code, 200)
        self.assertIn(b"Official Student Handbook", res2.data)

        # Authenticated student access
        self.login("student1", "student123")
        res3 = self.client.get("/guide")
        self.assertEqual(res3.status_code, 200)
        self.logout()

    def test_past_attendance_import(self):
        """Test importing past Google Sheets attendance rows with auto-user-provisioning and auto-enrollment."""
        self.login("kishan", "password123")

        sample_sheet_data = """Date\tEmail\tSection\tSession Type\tStatus\tKey
9/9/2026\tb26cs021@iitbhilai.ac.in\tBatch 1\tLecture\tPRESENT\tb26cs021@iitbhilai.ac.in_LECTURE_2026-09-09
9/9/2026\tb26cs034@iitbhilai.ac.in\tBatch 1\tLecture\tPRESENT\tb26cs034@iitbhilai.ac.in_LECTURE_2026-09-09
9/9/2026\ts26ma021@iitbhilai.ac.in\tM.Sc Maths\tLecture\tPRESENT\ts26ma021@iitbhilai.ac.in_LECTURE_2026-09-09
"""
        res = self.client.post("/courses/1/attendance/import", data={
            "import_data": sample_sheet_data,
            "default_session_type": "Lecture",
            "default_date": "2026-09-09",
            "auto_create_users": "1",
            "auto_enroll": "1"
        }, follow_redirects=True)

        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Successfully processed", res.data)

        # Verify DB records
        conn = app.get_db()
        st1 = conn.execute("SELECT * FROM users WHERE LOWER(email) = 'b26cs021@iitbhilai.ac.in'").fetchone()
        self.assertIsNotNone(st1)
        self.assertEqual(st1["roll_number"], "B26CS021")

        enr = conn.execute("SELECT * FROM course_enrollments WHERE course_id = 1 AND user_id = ?", (st1["id"],)).fetchone()
        self.assertIsNotNone(enr)

        sess = conn.execute("SELECT * FROM attendance_sessions WHERE course_id = 1 AND session_date = '2026-09-09'").fetchone()
        self.assertIsNotNone(sess)

        log = conn.execute("SELECT * FROM attendance_logs WHERE course_id = 1 AND student_id = ?", (st1["id"],)).fetchone()
        self.assertIsNotNone(log)
        self.assertEqual(log["attendance_date"], "2026-09-09")
        self.assertEqual(log["status"], "PRESENT")
        self.assertEqual(log["method"], "GOOGLE_SHEET_IMPORT")

        # Test idempotency - reimporting same data should not duplicate
        res2 = self.client.post("/courses/1/attendance/import", data={
            "import_data": sample_sheet_data,
            "default_session_type": "Lecture",
            "default_date": "2026-09-09",
            "auto_create_users": "1",
            "auto_enroll": "1"
        }, follow_redirects=True)
        self.assertEqual(res2.status_code, 200)

        logs_count = conn.execute("SELECT COUNT(*) as cnt FROM attendance_logs WHERE course_id = 1 AND student_id = ?", (st1["id"],)).fetchone()["cnt"]
        self.assertEqual(logs_count, 1)

        conn.close()
        self.logout()

    def test_usage_guide_routes_and_navigation(self):
        """Verify /usage-guide route and course navigation elements."""
        res1 = self.client.get("/usage-guide")
        self.assertEqual(res1.status_code, 200)
        self.assertIn(b"Usage", res1.data)

        res2 = self.client.get("/guide")
        self.assertEqual(res2.status_code, 200)

        # Login student to check base.html profile modal and course navigation
        self.login("kishan", "password123")
        res_dash = self.client.get("/dashboard")
        self.assertEqual(res_dash.status_code, 200)
        self.assertIn(b"userProfileModal", res_dash.data)
        self.assertIn(b"Usage Guide", res_dash.data)
        self.assertIn(b"App (.apk)", res_dash.data)
        self.assertNotIn(b'href="/messages"', res_dash.data)
        self.logout()

    def test_custom_attendance_formula_50_percent_cutoff(self):
        """
        Verify attendance threshold formula:
        - <= 50% attendance yields 0 points
        - > 50% is scaled up to 100% over the remaining 50% range
        For a 5% weighted category:
          * 25% att -> 0.0 / 5.0 pts
          * 50% att -> 0.0 / 5.0 pts
          * 75% att -> 2.5 / 5.0 pts
          * 100% att -> 5.0 / 5.0 pts
        """
        conn = app.get_db()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Create Course with 50.0% attendance threshold
        conn.execute("""
            INSERT INTO courses (code, title, section, join_code, teacher_id, attendance_threshold, created_at)
            VALUES ('CS999', 'Formula Test Class', 'Sec A', 'FORMULA99', 2, 50.0, ?)
        """, (now_str,))
        course_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Configure categories: Attendance 5%, Assignments 95%
        conn.execute("DELETE FROM course_grading_categories WHERE course_id = ?", (course_id,))
        conn.execute("""
            INSERT INTO course_grading_categories (course_id, name, weight, is_attendance, created_at)
            VALUES (?, 'Attendance', 5.0, 1, ?), (?, 'Assignments', 95.0, 0, ?)
        """, (course_id, now_str, course_id, now_str))

        # Create 4 students
        student_ids = []
        for i in range(1, 5):
            pwd = app.hash_password("password123")
            conn.execute("""
                INSERT INTO users (username, roll_number, email, password_hash, display_name, role, created_at)
                VALUES (?, ?, ?, ?, ?, 'student', ?)
            """, (f"formula_st_{i}", f"FST00{i}", f"fst{i}@test.com", pwd, f"Student {i}", now_str))
            sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            student_ids.append(sid)
            conn.execute("INSERT INTO course_enrollments (course_id, user_id, role, enrolled_at) VALUES (?, ?, 'student', ?)", (course_id, sid, now_str))

        # Create 4 attendance sessions (e.g. 2026-09-01, 2026-09-02, 2026-09-03, 2026-09-04)
        for d in ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"):
            conn.execute("INSERT INTO attendance_sessions (course_id, title, session_date, session_type, created_by, created_at) VALUES (?, 'Lecture Class', ?, 'Lecture', 2, ?)", (course_id, d, now_str))

        def add_att(sid, roll, name, date_str):
            att_key = f"{roll}_Lecture_{date_str}"
            conn.execute("""
                INSERT INTO attendance_logs (course_id, student_id, roll_number, student_name, session_type, attendance_date, status, method, marked_at, attendance_key)
                VALUES (?, ?, ?, ?, 'Lecture', ?, 'PRESENT', 'TEST', ?, ?)
            """, (course_id, sid, roll, name, date_str, now_str, att_key))

        # Student 1: 1 / 4 sessions = 25% (<= 50%)
        add_att(student_ids[0], "FST001", "Student 1", "2026-09-01")

        # Student 2: 2 / 4 sessions = 50% (<= 50%)
        for d in ("2026-09-01", "2026-09-02"):
            add_att(student_ids[1], "FST002", "Student 2", d)

        # Student 3: 3 / 4 sessions = 75% (> 50%)
        for d in ("2026-09-01", "2026-09-02", "2026-09-03"):
            add_att(student_ids[2], "FST003", "Student 3", d)

        # Student 4: 4 / 4 sessions = 100% (> 50%)
        for d in ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"):
            add_att(student_ids[3], "FST004", "Student 4", d)

        conn.commit()

        # Run grade calculation
        grades = app.calculate_course_grades(course_id, conn=conn)
        conn.close()

        students_by_id = {s["id"]: s for s in grades["students"]}

        # Student 1: 25% attendance -> 0.0 pts
        s1 = students_by_id[student_ids[0]]
        self.assertEqual(s1["attendance"]["percentage"], 25.0)
        self.assertEqual(s1["attendance"]["weighted_points"], 0.0)

        # Student 2: 50% attendance -> 0.0 pts
        s2 = students_by_id[student_ids[1]]
        self.assertEqual(s2["attendance"]["percentage"], 50.0)
        self.assertEqual(s2["attendance"]["weighted_points"], 0.0)

        # Student 3: 75% attendance -> (75-50)/(100-50)*100 = 50% effective -> 2.50 pts
        s3 = students_by_id[student_ids[2]]
        self.assertEqual(s3["attendance"]["percentage"], 75.0)
        self.assertEqual(s3["attendance"]["effective_percentage"], 50.0)
        self.assertEqual(s3["attendance"]["weighted_points"], 2.5)

        # Student 4: 100% attendance -> 100% effective -> 5.00 pts
        s4 = students_by_id[student_ids[3]]
        self.assertEqual(s4["attendance"]["percentage"], 100.0)
        self.assertEqual(s4["attendance"]["effective_percentage"], 100.0)
        self.assertEqual(s4["attendance"]["weighted_points"], 5.0)

    def test_chat_messaging_permissions_and_student_block(self):
        """
        Verify chat rules:
        - Student CAN message instructor/TA.
        - Instructor CAN message student.
        - Student CANNOT message another student (strictly 403 Forbidden).
        """
        # Create 2 students and 1 teacher
        conn = app.get_db()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pwd = app.hash_password("password123")

        conn.execute("INSERT INTO users (username, roll_number, email, password_hash, display_name, role, created_at) VALUES ('chat_student_1', 'CST01', 'cst1@test.com', ?, 'Student Alpha', 'student', ?)", (pwd, now_str))
        s1_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute("INSERT INTO users (username, roll_number, email, password_hash, display_name, role, created_at) VALUES ('chat_student_2', 'CST02', 'cst2@test.com', ?, 'Student Beta', 'student', ?)", (pwd, now_str))
        s2_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Enroll student 1 in course 1 where 'kishan' teaches
        conn.execute("INSERT INTO course_enrollments (course_id, user_id, role, enrolled_at) VALUES (1, ?, 'student', ?)", (s1_id, now_str))
        conn.execute("INSERT INTO course_enrollments (course_id, user_id, role, enrolled_at) VALUES (1, ?, 'student', ?)", (s2_id, now_str))

        teacher_id = conn.execute("SELECT id FROM users WHERE username = 'kishan'").fetchone()[0]
        conn.commit()
        conn.close()

        # 1. Login as Student 1: Send message to teacher (should succeed)
        self.login("chat_student_1", "password123")
        res_ok = self.client.post("/api/messages/send", json={
            "recipient_id": teacher_id,
            "message": "Hello Professor, I have a question regarding homework 2."
        })
        self.assertEqual(res_ok.status_code, 200)
        self.assertTrue(res_ok.get_json()["success"])

        # 2. Student 1 attempts to message Student 2 (MUST BE 403 FORBIDDEN)
        res_blocked = self.client.post("/api/messages/send", json={
            "recipient_id": s2_id,
            "message": "Hey what is the answer to question 3?"
        })
        self.assertEqual(res_blocked.status_code, 403)
        self.assertIn("strictly prohibited", res_blocked.get_json()["error"].lower())

        # Also test GET api message feed for student-student is 403
        res_get_blocked = self.client.get(f"/api/messages/{s2_id}")
        self.assertEqual(res_get_blocked.status_code, 403)
        self.logout()

        # 3. Login as Teacher: Reply to Student 1 (should succeed)
        self.login("kishan", "password123")
        res_teacher = self.client.post("/api/messages/send", json={
            "recipient_id": s1_id,
            "message": "Office hours are tomorrow at 3 PM."
        })
        self.assertEqual(res_teacher.status_code, 200)

        # Check conversation history
        res_history = self.client.get(f"/api/messages/{s1_id}")
        self.assertEqual(res_history.status_code, 200)
        msgs = res_history.get_json()["messages"]
        self.assertEqual(len(msgs), 2)
        self.logout()

    def test_admin_bulk_user_deletion(self):
        """Verify admin can delete multiple users in bulk while self-deletion is prevented."""
        conn = app.get_db()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pwd = app.hash_password("password123")

        u_ids = []
        for i in range(1, 4):
            conn.execute("INSERT INTO users (username, roll_number, email, password_hash, display_name, role, created_at) VALUES (?, ?, ?, ?, ?, 'student', ?)",
                         (f"bulk_del_{i}", f"BDEL{i}", f"bdel{i}@test.com", pwd, f"Bulk User {i}", now_str))
            u_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
        conn.close()

        # Login as Admin
        self.login("admin", "admin@accl")
        admin_id = 1

        # Attempt bulk delete including admin_id
        res = self.client.post("/admin/users/delete-bulk", data={
            "user_ids": [str(u_ids[0]), str(u_ids[1]), str(admin_id)]
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Successfully deleted 2 user account", res.data)

        # Verify in DB: u_ids[0] and u_ids[1] are deleted, u_ids[2] and admin remain
        conn = app.get_db()
        self.assertIsNone(conn.execute("SELECT id FROM users WHERE id = ?", (u_ids[0],)).fetchone())
        self.assertIsNone(conn.execute("SELECT id FROM users WHERE id = ?", (u_ids[1],)).fetchone())
        self.assertIsNotNone(conn.execute("SELECT id FROM users WHERE id = ?", (u_ids[2],)).fetchone())
        self.assertIsNotNone(conn.execute("SELECT id FROM users WHERE id = ?", (admin_id,)).fetchone())
        conn.close()
        self.logout()

    def test_save_grading_categories_updates_attendance_threshold(self):
        """Verify teachers and TAs can update attendance cutoff threshold from the grading scheme."""
        self.login("kishan", "password123")
        conn = app.get_db()
        c = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()
        course_id = c["id"]
        cat = conn.execute("SELECT id FROM course_grading_categories WHERE course_id = ? AND is_attendance = 1", (course_id,)).fetchone()
        cat_id = cat["id"] if cat else 1
        conn.close()

        # Submit updated grading scheme with 40.0% attendance cutoff threshold
        res = self.client.post(f"/courses/{course_id}/grades/categories", data={
            "cat_id": [str(cat_id)],
            "name": ["Class Attendance"],
            "weight": ["5.0"],
            "attendance_threshold": "40.0"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        conn = app.get_db()
        updated_course = conn.execute("SELECT attendance_threshold FROM courses WHERE id = ?", (course_id,)).fetchone()
        self.assertEqual(float(updated_course["attendance_threshold"]), 40.0)
        conn.close()
        self.logout()

    def test_api_send_message_json_endpoint(self):
        """Verify the direct messaging API endpoint returns valid JSON with correct status."""
        # Student 1 logs in
        self.login("student1", "student123")
        conn = app.get_db()
        teacher = conn.execute("SELECT id FROM users WHERE role = 'teacher' LIMIT 1").fetchone()
        teacher_id = teacher["id"]
        course = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()
        course_id = course["id"]
        conn.close()

        # Send direct message via JSON POST
        res = self.client.post("/api/messages/send", json={
            "recipient_id": teacher_id,
            "course_id": course_id,
            "message": "Hello Professor, regarding tomorrow's lab session..."
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("recipient_id"), teacher_id)
        self.assertIn("lab session", data.get("message"))
        self.logout()

    def test_api_messages_poll_returns_messages_and_convos(self):
        """Verify the live chat polling endpoint returns new messages and conversation metadata."""
        self.login("student1", "student123")
        conn = app.get_db()
        st1 = conn.execute("SELECT id FROM users WHERE username = 'student1'").fetchone()
        teacher = conn.execute("SELECT id FROM users WHERE role = 'teacher' LIMIT 1").fetchone()
        st1_id = st1["id"]
        t_id = teacher["id"]
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Insert a new unread message from teacher to student1
        c = conn.cursor()
        c.execute("""
            INSERT INTO direct_messages (sender_id, recipient_id, message, is_read, created_at)
            VALUES (?, ?, ?, 0, ?)
        """, (t_id, st1_id, "Don't forget your lab manual today!", now_str))
        msg_id = c.lastrowid
        conn.commit()
        conn.close()

        # Poll active thread with after_id = msg_id - 1
        res = self.client.get(f"/api/messages/poll?active_user_id={t_id}&after_id={msg_id - 1}")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("success"))
        self.assertGreaterEqual(len(data.get("new_messages", [])), 1)
        self.assertEqual(data["new_messages"][0]["id"], msg_id)
        self.assertIn("lab manual", data["new_messages"][0]["message"])
        self.assertFalse(data["new_messages"][0]["is_mine"])

        # Check that it automatically marked the message as read
        conn = app.get_db()
        read_check = conn.execute("SELECT is_read FROM direct_messages WHERE id = ?", (msg_id,)).fetchone()
        self.assertEqual(read_check["is_read"], 1)
        conn.close()
        self.logout()

    def test_admin_email_settings_view_update_reset(self):
        """Verify Admin can view, update, and reset Gmail SMTP settings."""
        # 1. Check default configuration
        user, pwd, name = app.get_smtp_config()
        self.assertEqual(user, "hoodle.lms@gmail.com")
        self.assertEqual(pwd, "hvrnbggfrzmnvrsh")

        # 2. Login as Admin and view settings
        self.login("admin", "admin@accl")
        res = self.client.get("/admin/email")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Gmail &amp; Notification Configuration", res.data)
        self.assertIn(b"hoodle.lms@gmail.com", res.data)

        # 3. Update Gmail credentials
        res = self.client.post("/admin/email/update", data={
            "gmail_user": "custom_accl_admin@gmail.com",
            "gmail_password": "customapppass123",
            "from_name": "ACCL IIT Bhilai LMS",
            "portal_base_url": "http://10.10.14.104/lms"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"SMTP settings saved successfully", res.data)

        # Verify get_smtp_config reflects the DB update
        up_user, up_pwd, up_name = app.get_smtp_config()
        self.assertEqual(up_user, "custom_accl_admin@gmail.com")
        self.assertEqual(up_pwd, "customapppass123")
        self.assertEqual(up_name, "ACCL IIT Bhilai LMS")

        # 4. Reset to system defaults
        res = self.client.post("/admin/email/reset", follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"reset to system defaults", res.data)

        # Verify fallback to default hoodle.lms@gmail.com
        rst_user, rst_pwd, rst_name = app.get_smtp_config()
        self.assertEqual(rst_user, "hoodle.lms@gmail.com")
        self.assertEqual(rst_pwd, "hvrnbggfrzmnvrsh")
        self.logout()

    def test_resolve_portal_url_and_reply_link(self):
        """Verify resolve_portal_url resolves paths to full URLs and prevents duplicate /lms prefixes."""
        with app.app.test_request_context():
            url1 = app.resolve_portal_url("/messages?user_id=2")
            self.assertTrue(url1.endswith("/messages?user_id=2"))
            self.assertTrue(url1.startswith("http"))

            url2 = app.resolve_portal_url("/lms/messages?user_id=2")
            self.assertNotIn("/lms/lms", url2)
            self.assertTrue(url2.endswith("/lms/messages?user_id=2"))

            url3 = app.resolve_portal_url("http://example.com/custom")
            self.assertEqual(url3, "http://example.com/custom")

    def test_add_student_and_ta_triggers_email_notification(self):
        """Verify adding a student or TA triggers send_event_notification_email."""
        from unittest.mock import patch

        self.login("kishan", "password123")
        conn = app.get_db()
        conn.execute("UPDATE users SET email = 'student1@iitbhilai.ac.in' WHERE id = 3")
        conn.execute("DELETE FROM course_enrollments WHERE course_id = 1 AND user_id = 3")
        conn.commit()
        conn.close()

        with patch("app.send_event_notification_email") as mock_notify:
            res = self.client.post("/courses/1/people/add-coteacher", data={
                "user_id": "3",
                "role": "student"
            }, follow_redirects=True)
            self.assertEqual(res.status_code, 200)
            mock_notify.assert_called_once()
            args, kwargs = mock_notify.call_args
            self.assertIn("student1@iitbhilai.ac.in", kwargs["recipient_emails"])
            self.assertIn("enrolled as a Student", kwargs["heading"])

        with patch("app.send_event_notification_email") as mock_notify:
            res = self.client.post("/courses/1/people/add-coteacher", data={
                "user_id": "3",
                "role": "ta"
            }, follow_redirects=True)
            self.assertEqual(res.status_code, 200)
            mock_notify.assert_called_once()
            args, kwargs = mock_notify.call_args
            self.assertIn("student1@iitbhilai.ac.in", kwargs["recipient_emails"])
            self.assertIn("Co-Teacher / TA", kwargs["heading"])

        self.logout()

    def test_invite_students_with_ta_role_and_acceptance(self):
        """Verify inviting a user as TA records role='ta' and enrolls them as TA upon acceptance."""
        from unittest.mock import patch

        self.login("kishan", "password123")
        with patch("app.send_course_invitation_email") as mock_inv:
            res = self.client.post("/courses/1/invite", data={
                "students_input": "newta@iitbhilai.ac.in",
                "role": "ta"
            }, follow_redirects=True)
            self.assertEqual(res.status_code, 200)
            self.assertIn(b"Co-Teacher(s) / TA(s) invitation(s) saved", res.data)
            mock_inv.assert_called_once()
            args, kwargs = mock_inv.call_args
            self.assertEqual(kwargs.get("role"), "ta")

        # Verify invitation row in DB
        conn = app.get_db()
        inv = conn.execute("SELECT * FROM course_invitations WHERE course_id = 1 AND student_email = 'newta@iitbhilai.ac.in'").fetchone()
        conn.close()
        self.assertIsNotNone(inv)
        self.assertEqual(inv["role"], "ta")

        # Now accept invitation by token while logged in as student1
        self.logout()
        self.login("student1", "student123")
        res = self.client.get(f"/invitations/accept/{inv['token']}", follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Successfully joined", res.data)

        # Verify enrollment has role='ta'
        conn = app.get_db()
        enroll = conn.execute("SELECT * FROM course_enrollments WHERE course_id = 1 AND user_id = 3").fetchone()
        conn.close()
        self.assertIsNotNone(enroll)
        self.assertEqual(enroll["role"], "ta")
        self.logout()

    def test_attendance_20s_rotation_and_projector_giant_screen(self):
        """Verify attendance QR code auto-rotates every 20s, default mode is Giant Screen, and countdown counter is removed."""
        self.assertEqual(app.ATTENDANCE_ROTATION_SECONDS, 20)

        self.login("kishan", "password123")
        res = self.client.get("/courses/1/attendance/projector")
        self.assertEqual(res.status_code, 200)

        # 1. Verify default view mode is Giant screen (mode-auditorium)
        self.assertIn(b'class="mode-auditorium"', res.data)
        self.assertIn(b'id="btnModeAuditorium" onclick="setViewMode(\'auditorium\')"', res.data)
        self.assertIn(b'btnModeAuditorium" onclick="setViewMode(\'auditorium\')" title="Giant Auditorium Screen (High-Vis)"', res.data)

        # 2. Verify countdown counter is removed
        self.assertNotIn(b'timer-ring-wrap', res.data)
        self.assertNotIn(b'Code refreshes in:', res.data)

        # 3. Verify text reflects 20s rotation
        self.assertIn(b'Auto-rotates every 20s', res.data)
        self.logout()

    def test_course_wise_messaging_and_unread_badges(self):
        """Verify course-scoped messaging route, student TA/teacher restrictions, and unread count badges."""
        self.login("kishan", "password123")

        # 1. Course messaging shortcut route
        res = self.client.get("/courses/1/messages")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/messages?course_id=1", res.headers.get("Location"))

        # Follow redirect to course messages
        res_follow = self.client.get("/messages?course_id=1")
        self.assertEqual(res_follow.status_code, 200)
        self.assertIn(b"Course Channel", res_follow.data)
        self.assertIn(b"CSL100 Messages", res_follow.data)
        self.assertIn(b"chat-app-card", res_follow.data)

        # 2. Send message to student1 in course 1
        res_send = self.client.post("/api/messages/send", json={
            "recipient_id": 3,
            "message": "Hello student, this is for CSL100.",
            "course_id": 1
        })
        self.assertEqual(res_send.status_code, 200)
        self.logout()

        # 3. Log in as student1 and check unread count & course tab badge
        self.login("student1", "student123")

        # Check course stream template renders Messages tab with unread badge 1
        res_stream = self.client.get("/courses/1/stream")
        self.assertEqual(res_stream.status_code, 200)
        self.assertIn(b"Messages", res_stream.data)

        # Check course messages view as student
        res_stud_msg = self.client.get("/messages?course_id=1")
        self.assertEqual(res_stud_msg.status_code, 200)
        self.assertIn(b"Course Channel", res_stud_msg.data)
        # Student contacts should only be course teachers/TAs
        self.assertIn(b"Kishan", res_stud_msg.data)

        # Academic integrity: student cannot message student even with course_id
        res_block = self.client.post("/api/messages/send", json={
            "recipient_id": 4, # another student if any
            "message": "Hey friend",
            "course_id": 1
        })
        # If recipient 4 is student or not found:
        self.assertIn(res_block.status_code, (403, 404))

        self.logout()

    def test_student_guide_no_tailscale_and_mobile_header_footer(self):
        """Verify student guide does not contain Tailscale VPN and mobile header/footer elements exist."""
        # 1. User Guide - verify NO Tailscale / VPN references
        res_guide = self.client.get("/student-guide")
        self.assertEqual(res_guide.status_code, 200)
        self.assertNotIn(b"Tailscale", res_guide.data)
        self.assertNotIn(b"tailscale", res_guide.data)
        self.assertNotIn(b"Tailscale VPN", res_guide.data)
        self.assertIn(b"Network Access &amp; Portal Connectivity", res_guide.data)

        # 2. Header user dropdown and footer classes
        self.login("kishan", "password123")
        res_dash = self.client.get("/dashboard")
        self.assertEqual(res_dash.status_code, 200)
        self.assertIn(b'id="userMenuWrapper"', res_dash.data)
        self.assertIn(b'id="userChipBtn"', res_dash.data)
        self.assertIn(b'id="userDropdownMenu"', res_dash.data)
        self.assertIn(b'Change Password', res_dash.data)
        self.assertIn(b'Sign Out', res_dash.data)
        self.assertIn(b'class="lms-footer"', res_dash.data)
        self.assertIn(b'class="lms-footer-container"', res_dash.data)
        self.logout()

    def test_topbar_messaging_removal_and_apk_download(self):
        """Verify global topbar does not show messages pill and footer has APK download."""
        self.login("kishan", "password123")
        res_dash = self.client.get("/dashboard")
        self.assertEqual(res_dash.status_code, 200)

        # Global nav-strip must NOT contain Messages link
        self.assertNotIn(b'href="/messages"', res_dash.data)
        self.assertNotIn("💬 Messages".encode("utf-8"), res_dash.data)

        # APK download link must be in user dropdown menu and dashboard
        self.assertIn(b'/download/app.apk', res_dash.data)
        self.assertIn(b'Download App (.apk)', res_dash.data)

        # Test APK download route
        res_apk = self.client.get("/download/app.apk")
        self.assertEqual(res_apk.status_code, 200)
        self.assertIn("application/vnd.android.package-archive", res_apk.headers.get("Content-Type", ""))
        self.assertIn("Hoodle_LMS.apk", res_apk.headers.get("Content-Disposition", ""))

        res_apk2 = self.client.get("/download/hoodle.apk")
        self.assertEqual(res_apk2.status_code, 200)

        self.logout()

    def test_student_registration_required_for_messaging(self):
        """Verify student must be registered in a course to send messages to instructors."""
        # 1. Register a brand new student not enrolled in any course
        reg_res = self.client.post("/register", data={
            "roll_number": "CS26BTECH99999",
            "full_name": "Unregistered Student",
            "email": "unregistered@iitbhilai.ac.in",
            "password": "password123",
            "confirm_password": "password123"
        }, follow_redirects=True)
        self.assertIn(b"Registration successful", reg_res.data)

        # 2. Login as this unregistered student
        self.login("CS26BTECH99999", "password123")

        # 3. View /messages - should display course registration warning
        res_msgs = self.client.get("/messages")
        self.assertEqual(res_msgs.status_code, 200)
        self.assertIn(b"Course Registration Required", res_msgs.data)

        # 4. Attempt to send direct message to instructor (user_id 1) -> Must be blocked (403)
        res_block = self.client.post("/api/messages/send", json={
            "recipient_id": 1,
            "message": "Hello Professor"
        })
        self.assertEqual(res_block.status_code, 403)
        self.assertIn("registered", res_block.get_json()["error"].lower())

        # 5. Enroll the student into Course 1
        conn = app.get_db()
        new_user = conn.execute("SELECT id FROM users WHERE roll_number = 'CS26BTECH99999'").fetchone()
        student_id = new_user["id"]
        conn.execute("""
            INSERT INTO course_enrollments (course_id, user_id, role, enrolled_at)
            VALUES (1, ?, 'student', '2026-09-12 12:00:00')
        """, (student_id,))
        conn.commit()
        conn.close()

        # 6. Attempt to message instructor now that student is registered in Course 1 -> Must succeed
        res_allow = self.client.post("/api/messages/send", json={
            "recipient_id": 1,
            "course_id": 1,
            "message": "Hello Professor, I am now registered!"
        })
        self.assertEqual(res_allow.status_code, 200)
        self.assertTrue(res_allow.get_json()["success"])

    def test_topbar_scan_qr_and_courses_border(self):
        """Verify Courses and Scan QR buttons have uniform border class and bottom scanner-fab is removed."""
        self.login("student1", "student123")
        res = self.client.get("/dashboard")
        self.assertEqual(res.status_code, 200)

        # 1. Courses button has nav-pill-bordered and nav-pill-courses
        self.assertIn(b"nav-pill-bordered nav-pill-courses", res.data)

        # 2. Top bar Scan QR button has nav-pill-bordered, nav-pill-scan-qr, and 'ScanQR' label
        self.assertIn(b"nav-pill-bordered nav-pill-scan-qr", res.data)
        self.assertIn(b"ScanQR", res.data)

        # 3. Floating downside scanner button is completely removed
        self.assertNotIn(b'class="scanner-fab"', res.data)
        self.logout()

    def test_user_icon_menu_has_guide_and_apk(self):
        """Verify both userDropdownMenu and userProfileModal include Usage Guide and APK download links."""
        self.login("student1", "student123")
        res = self.client.get("/dashboard")
        self.assertEqual(res.status_code, 200)

        # 1. User dropdown menu has Usage Guide and APK download
        self.assertIn(b'id="userDropdownMenu"', res.data)
        self.assertIn(b'href="/student-guide"', res.data)
        self.assertIn(b'href="/download/app.apk"', res.data)

        # 2. User profile modal has Usage Guide and APK download
        self.assertIn(b'id="userProfileModal"', res.data)
        self.assertIn(b'Usage Guide', res.data)
        self.assertIn(b'Download App (.apk)', res.data)
        self.logout()

    def test_teacher_promoted_ta_messaging_allowed(self):
        """Verify that when a teacher promotes a student to TA, messaging to and from the TA is allowed without admin intervention."""
        from unittest.mock import patch

        # Enroll student1 and a second student in Course 1
        conn = app.get_db()
        pwd = app.hash_password("password123")
        conn.execute("INSERT OR IGNORE INTO users (id, username, password_hash, display_name, email, role, created_at) VALUES (4, 'student2', ?, 'Second Student', 'student2@iitbhilai.ac.in', 'student', '2026-09-12 12:00:00')", (pwd,))
        conn.execute("INSERT OR REPLACE INTO course_enrollments (course_id, user_id, role, enrolled_at) VALUES (1, 3, 'student', '2026-09-12 12:00:00')")
        conn.execute("INSERT OR REPLACE INTO course_enrollments (course_id, user_id, role, enrolled_at) VALUES (1, 4, 'student', '2026-09-12 12:00:00')")
        conn.commit()
        conn.close()

        # Teacher promotes student1 to TA
        self.login("kishan", "password123")
        res_promote = self.client.post("/courses/1/people/4/role", data={
            "role": "ta"
        }, follow_redirects=True)
        self.assertEqual(res_promote.status_code, 200)
        self.logout()

        with patch("app.send_event_notification_email"):
            # Student1 logs in and messages the newly promoted TA (user_id 4) -> must succeed!
            self.login("student1", "student123")
            res_msg = self.client.post("/api/messages/send", json={
                "recipient_id": 4,
                "course_id": 1,
                "message": "Hi TA, question regarding lab 1."
            })
            self.assertEqual(res_msg.status_code, 200)
            self.assertTrue(res_msg.get_json()["success"])
            self.logout()

            # Promoted TA logs in and replies to student1 -> must succeed!
            self.login("student2", "password123")
            res_reply = self.client.post("/api/messages/send", json={
                "recipient_id": 3,
                "course_id": 1,
                "message": "Sure, office hours are tomorrow at 4pm."
            })
            self.assertEqual(res_reply.status_code, 200)
            self.assertTrue(res_reply.get_json()["success"])
            self.logout()

    def test_student_message_sends_gmail_notification(self):
        """Verify student sending message to teacher/TA triggers email notification with course details."""
        from unittest.mock import patch

        self.login("student1", "student123")
        with patch("app.send_event_notification_email") as mock_mail:
            res = self.client.post("/api/messages/send", json={
                "recipient_id": 2, # Prof. Kishan
                "course_id": 1,
                "message": "Dear Professor, I have a doubt regarding homework 2."
            })
            self.assertEqual(res.status_code, 200)
            mock_mail.assert_called_once()
            args, kwargs = mock_mail.call_args
            self.assertIn("kishan@iitbhilai.ac.in", kwargs["recipient_emails"])
            self.assertIn("[CSL100]", kwargs["subject"])
            self.assertIn("Student", kwargs["subject"])
            self.assertIn("Aarav Sharma", kwargs["heading"])
            self.assertIn("Reply to Student on Hoodle", kwargs["action_text"])
        self.logout()

    def test_ta_included_in_announcement_and_coursework_notifications(self):
        """Verify all enrolled TAs receive email notifications alongside students for announcements and coursework."""
        from unittest.mock import patch

        conn = app.get_db()
        conn.execute("INSERT OR IGNORE INTO users (id, username, password_hash, display_name, email, role, created_at) VALUES (4, 'ta_user', 'hash', 'TA Person', 'ta_person@iitbhilai.ac.in', 'ta', '2026-09-12 12:00:00')")
        conn.execute("INSERT OR REPLACE INTO course_enrollments (course_id, user_id, role, enrolled_at) VALUES (1, 4, 'ta', '2026-09-12 12:00:00')")
        conn.commit()
        conn.close()

        self.login("kishan", "password123")

        # 1. Post announcement
        with patch("app.send_event_notification_email") as mock_ann:
            res = self.client.post("/courses/1/announcements", data={
                "content": "Important class announcement for all students and TAs."
            }, follow_redirects=True)
            self.assertEqual(res.status_code, 200)
            mock_ann.assert_called_once()
            args, kwargs = mock_ann.call_args
            self.assertIn("ta_person@iitbhilai.ac.in", kwargs["recipient_emails"])
            self.assertIn("b26ds001@iitbhilai.ac.in", kwargs["recipient_emails"])

        # 2. Create coursework
        with patch("app.send_event_notification_email") as mock_cw:
            res_cw = self.client.post("/courses/1/coursework/create", data={
                "title": "Lab Assignment 3",
                "type": "assignment",
                "points": "50",
                "due_date": "2026-09-20T23:59",
                "instructions": "Complete all exercises."
            }, follow_redirects=True)
            self.assertEqual(res_cw.status_code, 200)
            mock_cw.assert_called_once()
            args, kwargs = mock_cw.call_args
            self.assertIn("ta_person@iitbhilai.ac.in", kwargs["recipient_emails"])
            self.assertIn("b26ds001@iitbhilai.ac.in", kwargs["recipient_emails"])

        self.logout()

    def test_messages_no_email_alerts_active_box(self):
        """Verify the 'Email alerts active' box has been removed from messages page."""
        self.login("student1", "student123")
        res = self.client.get("/messages")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(b"Email alerts active", res.data)
        self.logout()

    def test_user_icon_network_buttons_intranet_and_internet(self):
        """Verify the user icon dropdown and modal include 'Intranet' (default) and 'Internet' buttons."""
        self.login("student1", "student123")
        res = self.client.get("/dashboard")
        self.assertEqual(res.status_code, 200)

        # 1. Top bar user chip has Intranet badge
        self.assertIn(b'id="topNetBadge"', res.data)
        self.assertIn(b'Intranet', res.data)

        # 2. User dropdown menu has Intranet and Internet toggle buttons
        self.assertIn(b'id="userDropdownMenu"', res.data)
        self.assertIn(b'id="btnNetIntranet"', res.data)
        self.assertIn(b'id="btnNetInternet"', res.data)
        self.assertIn(b'onclick="selectNetworkMode(\'intranet\', event)"', res.data)
        self.assertIn(b'onclick="selectNetworkMode(\'internet\', event)"', res.data)

        # 3. User profile modal has network toggle buttons
        self.assertIn(b'id="userProfileModal"', res.data)
        self.assertIn(b'id="modalBtnNetIntranet"', res.data)
        self.assertIn(b'id="modalBtnNetInternet"', res.data)
        self.logout()

    def test_admin_test_email_534_error_guidance(self):
        """Verify SMTP test verification handles 534 error with helpful guidance."""
        from unittest.mock import patch
        import smtplib

        self.login("admin", "admin@accl")
        # Mock create_smtp_connection to simulate Google 534 WebLoginRequired
        with patch("app.create_smtp_connection") as mock_conn:
            mock_conn.side_effect = smtplib.SMTPAuthenticationError(534, b"5.7.9 Please log in with your web browser and then try again. https://support.google.com/mail/?p=WebLoginRequired")
            res = self.client.post("/admin/email/test", data={
                "test_recipient": "test@domain.com"
            }, follow_redirects=True)
            self.assertEqual(res.status_code, 200)
            self.assertIn(b"Google Authentication Blocked", res.data)
            self.assertIn(b"Error 534: WebLoginRequired", res.data)
            self.assertIn(b"DisplayUnlockCaptcha", res.data)
        self.logout()

    def test_change_password_flow_and_validation(self):
        """Verify password change validations: invalid current password shows danger alert, and valid change succeeds."""
        self.login("student1", "student123")

        # 1. GET /change-password shows form with current_password, new_password, confirm_password
        res = self.client.get("/change-password")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Change Account Password", res.data)
        self.assertIn(b'name="current_password"', res.data)
        self.assertIn(b'name="new_password"', res.data)

        # 2. POST with invalid current password shows "Invalid current password." in alert-danger
        res_fail = self.client.post("/change-password", data={
            "current_password": "wrongpassword",
            "new_password": "brandnewpassword123",
            "confirm_password": "brandnewpassword123"
        })
        self.assertEqual(res_fail.status_code, 200)
        self.assertIn(b"Invalid current password.", res_fail.data)
        self.assertIn(b"alert-danger", res_fail.data)

        # 3. POST with correct current password succeeds and redirects to dashboard
        res_ok = self.client.post("/change-password", data={
            "current_password": "student123",
            "new_password": "brandnewpassword123",
            "confirm_password": "brandnewpassword123"
        }, follow_redirects=True)
        self.assertEqual(res_ok.status_code, 200)
        self.assertIn(b"Your password has been changed successfully.", res_ok.data)
        self.logout()

        # 4. Student can now log in with the new password
        res_login = self.login("student1", "brandnewpassword123")
        self.assertEqual(res_login.status_code, 200)

        # 5. Rapid duplicate submit handling (where hash matches new password)
        res_dup = self.client.post("/change-password", data={
            "current_password": "student123",
            "new_password": "brandnewpassword123",
            "confirm_password": "brandnewpassword123"
        }, follow_redirects=True)
        self.assertEqual(res_dup.status_code, 200)
        self.assertIn(b"Your password has been changed successfully.", res_dup.data)
    def test_attendance_session_matrix_export_and_google_sheet_backup(self):
        """Verify session-by-session attendance matrix CSV export, Google Sheets live feed, and automated webhook backup."""
        from unittest.mock import patch, MagicMock

        conn = app.get_db()
        # Seed test sessions in attendance_logs for course 1
        # Student 3 is student1 (roll B26DS001)
        conn.execute("DELETE FROM attendance_logs WHERE course_id = 1")
        conn.execute("""
            INSERT INTO attendance_logs (course_id, student_id, roll_number, student_name, attendance_date, session_type, status, method, marked_at, attendance_key)
            VALUES (1, 3, 'B26DS001', 'Aarav Sharma', '2026-09-01', 'Lecture', 'present', 'QR_SCAN', '2026-09-01 10:00:00', '3_Lecture_2026-09-01')
        """)
        conn.execute("""
            INSERT INTO attendance_logs (course_id, student_id, roll_number, student_name, attendance_date, session_type, status, method, marked_at, attendance_key)
            VALUES (1, 3, 'B26DS001', 'Aarav Sharma', '2026-09-03', 'Lab', 'present', 'QR_SCAN', '2026-09-03 14:00:00', '3_Lab_2026-09-03')
        """)
        # A third session where another student or nobody attended (simulating an absent session for student1)
        conn.execute("""
            INSERT INTO attendance_logs (course_id, student_id, roll_number, student_name, attendance_date, session_type, status, method, marked_at, attendance_key)
            VALUES (1, 1, 'ADMIN', 'LMS Administrator', '2026-09-05', 'Lecture', 'present', 'MANUAL', '2026-09-05 10:00:00', '1_Lecture_2026-09-05')
        """)
        conn.commit()
        conn.close()

        # 1. Test get_course_attendance_matrix backend function
        matrix_data = app.get_course_attendance_matrix(1)
        self.assertEqual(len(matrix_data["sessions"]), 3)
        headers = matrix_data["matrix"][0]
        self.assertIn("2026-09-01 (Lecture)", headers)
        self.assertIn("2026-09-03 (Lab)", headers)
        self.assertIn("2026-09-05 (Lecture)", headers)
        self.assertIn("Total Attended", headers)
        self.assertIn("Attendance Percentage", headers)

        # Check student1 row in matrix
        student1_row = [r for r in matrix_data["matrix"][1:] if r[0] == 'B26DS001' or 'Aarav' in r[1]]
        self.assertTrue(len(student1_row) > 0)
        s_row = student1_row[0]
        # Should have 'P' for 2026-09-01 and 2026-09-03, 'A' for 2026-09-05
        date1_idx = headers.index("2026-09-01 (Lecture)")
        date2_idx = headers.index("2026-09-03 (Lab)")
        date3_idx = headers.index("2026-09-05 (Lecture)")
        self.assertEqual(s_row[date1_idx], "P")
        self.assertEqual(s_row[date2_idx], "P")
        self.assertEqual(s_row[date3_idx], "A")

        # 2. Test CSV Export Route (session-by-session)
        self.login("kishan", "password123")
        res_csv = self.client.get("/courses/1/attendance/export-csv")
        self.assertEqual(res_csv.status_code, 200)
        self.assertIn("text/csv", res_csv.headers.get("Content-Type", ""))
        self.assertIn("attachment", res_csv.headers.get("Content-Disposition", ""))
        csv_text = res_csv.data.decode("utf-8")
        self.assertIn("2026-09-01 (Lecture)", csv_text)
        self.assertIn("2026-09-03 (Lab)", csv_text)
        self.assertIn("2026-09-05 (Lecture)", csv_text)
        self.assertIn("Total Attended", csv_text)

        # 3. Test Google Sheet Live Feed Endpoint
        conn = app.get_db()
        course = conn.execute("SELECT attendance_feed_token FROM courses WHERE id = 1").fetchone()
        conn.close()
        feed_token = course["attendance_feed_token"]
        self.assertTrue(bool(feed_token))

        # Unauthorized access without token
        res_feed_bad = self.client.get("/api/courses/1/attendance/sheet-feed")
        self.assertEqual(res_feed_bad.status_code, 403)

        # Unauthorized access with invalid token
        res_feed_invalid = self.client.get("/api/courses/1/attendance/sheet-feed?token=invalid_token")
        self.assertEqual(res_feed_invalid.status_code, 403)

        # Authorized access with valid token
        res_feed_ok = self.client.get(f"/api/courses/1/attendance/sheet-feed?token={feed_token}")
        self.assertEqual(res_feed_ok.status_code, 200)
        self.assertIn("text/csv", res_feed_ok.headers.get("Content-Type", ""))
        feed_csv = res_feed_ok.data.decode("utf-8")
        self.assertIn("2026-09-01 (Lecture)", feed_csv)
        self.assertIn("2026-09-03 (Lab)", feed_csv)

        # 4. Test Google Sheet Webhook Config POST
        res_config = self.client.post("/courses/1/attendance/google-sheet-config", data={
            "webhook_url": "https://script.google.com/macros/s/test_token/exec",
            "daily_sync": "1"
        }, follow_redirects=True)
        self.assertEqual(res_config.status_code, 200)
        self.assertIn(b"Google Sheet backup settings updated successfully.", res_config.data)

        # Verify DB updated
        conn = app.get_db()
        updated_course = conn.execute("SELECT google_sheet_webhook_url, google_sheet_sync_enabled FROM courses WHERE id = 1").fetchone()
        conn.close()
        self.assertEqual(updated_course["google_sheet_webhook_url"], "https://script.google.com/macros/s/test_token/exec")
        self.assertEqual(updated_course["google_sheet_sync_enabled"], 1)

        # 5. Test Manual Google Sheet Sync POST with mocked urllib
        with patch("urllib.request.urlopen") as mock_url:
            mock_resp = MagicMock()
            mock_resp.__enter__.return_value = mock_resp
            mock_resp.status = 200
            mock_url.return_value = mock_resp

            res_sync = self.client.post("/courses/1/attendance/sync-google-sheet", follow_redirects=True)
            self.assertEqual(res_sync.status_code, 200)
            self.assertIn(b"Google Sheet Sync: Success", res_sync.data)
            mock_url.assert_called_once()

        # 6. Verify Attendance View UI has the new buttons & modal
        res_view = self.client.get("/courses/1/attendance")
        self.assertEqual(res_view.status_code, 200)
        self.assertIn(b"Google Sheet Backup", res_view.data)
        self.assertIn(b"Export Detailed CSV (Session-by-Session)", res_view.data)
        self.assertIn(b'id="googleSheetModal"', res_view.data)
        self.assertIn(b'=IMPORTDATA(', res_view.data)
        self.assertIn(b'function doPost(e)', res_view.data)
        self.logout()


if __name__ == "__main__":
    unittest.main()




