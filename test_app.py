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

class TestLabExamPortalV3(unittest.TestCase):
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

    def login_teacher(self):
        return self.client.post("/login", data={
            "username": "kishan",
            "password": "password123"
        }, follow_redirects=True)

    def test_short_url_and_submission(self):
        self.login_teacher()
        # Create exam with short_code=csl100
        res = self.client.post("/admin/exams/create", data={
            "course_code": "CSL100",
            "short_code": "csl100",
            "course_name": "Computer Programming Lab",
            "exam_title": "Lab Exam 1",
            "teacher_name": "Prof. Kishan",
            "allowed_types": "zip",
            "allow_multiple": "1"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        # Student opens short link /csl100 directly without login
        res_page = self.client.get("/csl100")
        self.assertEqual(res_page.status_code, 200)
        self.assertIn(b"CSL100", res_page.data)

        # Student submits to short link /csl100/submit
        res_sub = self.client.post("/csl100/submit", data={
            "roll_number": "b26ds003",
            "exam_file": (create_sample_zip(b"test"), "exam.zip")
        })
        self.assertEqual(res_sub.status_code, 200)
        self.assertTrue(res_sub.get_json()["success"])
        self.assertEqual(res_sub.get_json()["details"]["roll_number"], "B26DS003")

    def test_add_teacher(self):
        self.login_teacher()
        # Add new teacher
        res = self.client.post("/admin/teachers/add", data={
            "username": "sharma",
            "display_name": "Dr. Sharma",
            "password": "teacherpass123"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        # Log out
        self.client.get("/logout")

        # Log in as the new teacher
        res_login = self.client.post("/login", data={
            "username": "sharma",
            "password": "teacherpass123"
        }, follow_redirects=True)
        self.assertEqual(res_login.status_code, 200)
        self.assertIn(b"Dr. Sharma", res_login.data)

if __name__ == "__main__":
    unittest.main()
