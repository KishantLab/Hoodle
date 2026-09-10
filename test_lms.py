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


if __name__ == "__main__":
    unittest.main()
