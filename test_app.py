import os
import io
import zipfile
import unittest
import shutil
from pathlib import Path

os.environ["SUBMISSION_DIR"] = "/tmp/test_submissions"
os.environ["DB_PATH"] = "/tmp/test_submissions.db"
os.environ["PORT"] = "8099"

from app import app, init_db, sanitize_roll_number

def create_sample_zip(content=b"sample code"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("test.c", content)
    buf.seek(0)
    return buf

class TestLabExamPortalV4(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        if os.path.exists("/tmp/test_submissions.db"):
            os.remove("/tmp/test_submissions.db")
        if os.path.exists("/tmp/test_submissions"):
            shutil.rmtree("/tmp/test_submissions")
        init_db()
        self.client = app.test_client()

    def tearDown(self):
        if os.path.exists("/tmp/test_submissions.db"):
            os.remove("/tmp/test_submissions.db")
        if os.path.exists("/tmp/test_submissions"):
            shutil.rmtree("/tmp/test_submissions")

    def login(self, username, password):
        return self.client.post("/login", data={
            "username": username,
            "password": password
        }, follow_redirects=True)

    def test_admin_rbac_and_teacher_isolation(self):
        # Admin logs in
        self.login("admin", "admin@accl")

        # Admin adds teacher: gupta
        res = self.client.post("/admin/teachers/add", data={
            "username": "gupta",
            "display_name": "Dr. Gupta",
            "password": "guptapassword"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        # Admin creates exam CSL100
        self.client.post("/admin/exams/create", data={
            "course_code": "CSL100",
            "short_code": "csl100",
            "course_name": "Prog Lab",
            "exam_title": "Exam 1",
            "teacher_name": "Prof. Kishan",
            "allowed_types": "zip",
            "allow_multiple": "1",
            "labs": "Lab 1, Lab 2"
        })

        # Logout admin
        self.client.get("/logout")

        # Teacher gupta logs in
        self.login("gupta", "guptapassword")

        # Teacher cannot add other teachers
        res_add = self.client.post("/admin/teachers/add", data={
            "username": "hacker",
            "display_name": "Hacker",
            "password": "password"
        }, follow_redirects=True)
        self.assertIn(b"Permission denied", res_add.data)

        # Teacher dashboard should NOT show CSL100 (Prog Lab)
        res_dash = self.client.get("/admin")
        self.assertNotIn(b"Prog Lab", res_dash.data)

        # Teacher gupta creates their own exam: EEL201
        self.client.post("/admin/exams/create", data={
            "course_code": "EEL201",
            "short_code": "eel201",
            "course_name": "Circuits Lab",
            "exam_title": "Circuits Exam",
            "teacher_name": "Dr. Gupta",
            "allowed_types": "zip",
            "allow_multiple": "1",
            "labs": "Hardware Lab, VLSI Lab"
        })

        # Teacher gupta sees EEL201, not CSL100
        res_dash2 = self.client.get("/admin")
        self.assertIn(b"Circuits Lab", res_dash2.data)
        self.assertNotIn(b"Prog Lab", res_dash2.data)

    def test_multi_lab_student_submission(self):
        self.login("admin", "admin@accl")
        self.client.post("/admin/exams/create", data={
            "course_code": "CSL100",
            "short_code": "csl100",
            "course_name": "Prog Lab",
            "exam_title": "Exam 1",
            "teacher_name": "Prof. Kishan",
            "allowed_types": "zip",
            "allow_multiple": "1",
            "labs": "Lab 1, Lab 2, Lab 3"
        })

        # Student submits from Lab 2
        res = self.client.post("/csl100/submit", data={
            "roll_number": "B26DS010",
            "lab_name": "Lab 2",
            "exam_file": (create_sample_zip(b"code"), "my.zip")
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["details"]["lab_name"], "Lab 2")

        # Check course detail view shows Lab 2
        res_admin = self.client.get("/admin?exam=csl100")
        self.assertIn(b"Lab 2", res_admin.data)
        self.assertIn(b"B26DS010", res_admin.data)

    def test_course_deletion(self):
        self.login("admin", "admin@accl")
        self.client.post("/admin/exams/create", data={
            "course_code": "TEMP101",
            "short_code": "temp101",
            "course_name": "Temp Lab",
            "exam_title": "Temp Exam",
            "teacher_name": "Prof. Kishan",
            "allowed_types": "zip",
            "allow_multiple": "1"
        })

        # Submit a file
        self.client.post("/temp101/submit", data={
            "roll_number": "B26DS999",
            "exam_file": (create_sample_zip(b"temp"), "t.zip")
        })

        course_folder = Path("/tmp/test_submissions/TEMP101_Temp_Exam")
        self.assertTrue(course_folder.exists())

        # Delete course
        res_del = self.client.post("/admin/exams/1/delete", follow_redirects=True)
        self.assertEqual(res_del.status_code, 200)
        self.assertIn(b"permanently deleted", res_del.data)

        # Check directory deleted from server disk
        self.assertFalse(course_folder.exists())

    def test_admin_password_reset_and_delete_user(self):
        # 1. Login as default admin
        res_login = self.client.post("/login", data={
            "username": "admin",
            "password": "admin@accl"
        }, follow_redirects=True)
        self.assertEqual(res_login.status_code, 200)

        # 2. Add a teacher
        self.client.post("/admin/teachers/add", data={
            "username": "t_test",
            "display_name": "Test Teacher",
            "password": "initialpassword"
        })

        import sqlite3
        conn = sqlite3.connect("/tmp/test_submissions.db")
        u_id = conn.execute("SELECT id FROM users WHERE username='t_test'").fetchone()[0]
        conn.close()

        # 3. Admin resets password for t_test with require_reset=1
        res_reset = self.client.post(f"/admin/users/{u_id}/password", data={
            "new_password": "temppassword123",
            "require_reset": "1"
        }, follow_redirects=True)
        self.assertEqual(res_reset.status_code, 200)

        # 4. Logout admin
        self.client.get("/logout")

        # 5. Teacher t_test logs in with temppassword123 -> redirected to /set-password
        res_t_login = self.client.post("/login", data={
            "username": "t_test",
            "password": "temppassword123"
        })
        self.assertEqual(res_t_login.status_code, 302)
        self.assertIn("/set-password", res_t_login.headers["Location"])

        # 6. Teacher submits new password
        res_set = self.client.post("/set-password", data={
            "new_password": "myfinalpassword",
            "confirm_password": "myfinalpassword"
        }, follow_redirects=True)
        self.assertEqual(res_set.status_code, 200)
        self.assertIn(b"Test Teacher", res_set.data)

        # 7. Admin logs back in and deletes t_test
        self.client.get("/logout")
        self.client.post("/login", data={"username": "admin", "password": "admin@accl"})
        res_del = self.client.post(f"/admin/users/{u_id}/delete", follow_redirects=True)
        self.assertEqual(res_del.status_code, 200)

        # Verify user is gone from db
        conn = sqlite3.connect("/tmp/test_submissions.db")
        row = conn.execute("SELECT id FROM users WHERE id=?", (u_id,)).fetchone()
        conn.close()
        self.assertIsNone(row)

    def test_timing_and_late_submission_policies(self):
        from datetime import datetime, timedelta
        self.login("admin", "admin@accl")

        now = datetime.now()
        future_start = (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
        future_end = (now + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M")

        # 1. Exam not started yet
        self.client.post("/admin/exams/create", data={
            "course_code": "CSL200",
            "short_code": "csl200",
            "course_name": "Data Structures",
            "exam_title": "Midterm Exam",
            "teacher_name": "Prof. Kishan",
            "start_time": future_start,
            "end_time": future_end,
            "late_policy": "strict"
        })

        # Check student page indicates not started
        res_page = self.client.get("/csl200")
        self.assertIn(b"Exam Has Not Started Yet", res_page.data)

        # Attempt student submission before start -> rejected 403
        res_sub = self.client.post("/csl200/submit", data={
            "roll_number": "B26DS001",
            "exam_file": (create_sample_zip(b"code"), "my.zip")
        })
        self.assertEqual(res_sub.status_code, 403)
        self.assertIn("not started yet", res_sub.get_json()["error"].lower())

        # 2. Strict End Time Policy (past deadline)
        past_start = (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
        past_end = (now - timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M")

        # Edit schedule of exam 1 to be past deadline with strict policy
        res_edit = self.client.post("/admin/exams/1/edit-schedule", data={
            "start_time": past_start,
            "end_time": past_end,
            "late_policy": "strict",
            "labs": "Lab 1, Lab 2"
        }, follow_redirects=True)
        self.assertEqual(res_edit.status_code, 200)

        # Check student page indicates submissions closed
        res_page2 = self.client.get("/csl200")
        self.assertIn(b"Submissions Closed", res_page2.data)

        # Attempt submission -> rejected 403
        res_sub2 = self.client.post("/csl200/submit", data={
            "roll_number": "B26DS001",
            "exam_file": (create_sample_zip(b"code"), "my.zip")
        })
        self.assertEqual(res_sub2.status_code, 403)
        self.assertIn("strictly closed", res_sub2.get_json()["error"].lower())

        # 3. Allow Late Policy (past deadline)
        res_edit2 = self.client.post("/admin/exams/1/edit-schedule", data={
            "start_time": past_start,
            "end_time": past_end,
            "late_policy": "allow_late",
            "labs": "Lab 1, Lab 2"
        }, follow_redirects=True)
        self.assertEqual(res_edit2.status_code, 200)

        # Check student page indicates late submission window active
        res_page3 = self.client.get("/csl200")
        self.assertIn(b"Late Submission Window Active", res_page3.data)

        # Attempt submission -> accepted, flagged as late with minutes
        res_sub3 = self.client.post("/csl200/submit", data={
            "roll_number": "B26DS001",
            "lab_name": "Lab 1",
            "exam_file": (create_sample_zip(b"late code"), "my.zip")
        })
        self.assertEqual(res_sub3.status_code, 200)
        data = res_sub3.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["details"]["is_late"])
        self.assertIn("late", data["details"]["late_desc"])

        # 4. Polling API
        res_poll = self.client.get("/api/exam/1/submissions-poll")
        self.assertEqual(res_poll.status_code, 200)
        poll_data = res_poll.get_json()
        self.assertTrue(poll_data["success"])
        self.assertEqual(poll_data["count"], 1)
        self.assertEqual(poll_data["lab_counts"].get("Lab 1"), 1)
        self.assertTrue(poll_data["submissions"][0]["is_late"])
        self.assertEqual(poll_data["submissions"][0]["roll_number"], "B26DS001")


if __name__ == "__main__":
    unittest.main()

