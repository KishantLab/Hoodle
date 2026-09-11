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


if __name__ == "__main__":
    unittest.main()



