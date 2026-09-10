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

class TestLabExamPortalV2(unittest.TestCase):
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

    def test_teacher_login_and_create_exam(self):
        # Test login
        res = self.login_teacher()
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Course Exams", res.data)

        # Test creating exam
        res = self.client.post("/admin/exams/create", data={
            "course_code": "CSL100",
            "course_name": "Computer Programming Lab",
            "exam_title": "Lab Exam 1",
            "teacher_name": "Prof. Kishan",
            "instructions": "Submit zip file only",
            "allowed_types": "zip",
            "allow_multiple": "1"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        # Check course folder created on disk
        course_folder = Path("/tmp/test_submissions/CSL100_Lab_Exam_1")
        self.assertTrue(course_folder.exists())

    def test_student_submission_single_file_storage(self):
        # Create exam first
        self.login_teacher()
        self.client.post("/admin/exams/create", data={
            "course_code": "CSL100",
            "course_name": "Computer Programming Lab",
            "exam_title": "Lab Exam 1",
            "teacher_name": "Prof. Kishan",
            "instructions": "Submit zip file only",
            "allowed_types": "zip",
            "allow_multiple": "1"
        })

        # Student submits without login
        zip_v1 = create_sample_zip(b"Version 1 code")
        res = self.client.post("/exam/csl100-lab-exam-1/submit", data={
            "roll_number": "b26ds003",
            "exam_file": (zip_v1, "my_exam.zip")
        })
        self.assertEqual(res.status_code, 200)
        res_json = res.get_json()
        self.assertTrue(res_json["success"])
        self.assertEqual(res_json["details"]["roll_number"], "B26DS003")
        self.assertEqual(res_json["details"]["version"], 1)

        # Check only 1 file in the course folder: B26DS003.zip
        course_folder = Path("/tmp/test_submissions/CSL100_Lab_Exam_1")
        files = list(course_folder.iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "B26DS003.zip")

        # Second submission by student (multiple submissions allowed)
        zip_v2 = create_sample_zip(b"Version 2 updated code")
        res2 = self.client.post("/exam/csl100-lab-exam-1/submit", data={
            "roll_number": "B26DS003",
            "exam_file": (zip_v2, "my_exam.zip")
        })
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.get_json()["details"]["version"], 2)

        # Check STILL only 1 file in the course folder, replaced with v2
        files = list(course_folder.iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "B26DS003.zip")
        with zipfile.ZipFile(files[0], "r") as zf:
            self.assertEqual(zf.read("test.c"), b"Version 2 updated code")

    def test_single_submission_policy_rejected(self):
        # Create exam with allow_multiple = 0
        self.login_teacher()
        self.client.post("/admin/exams/create", data={
            "course_code": "CSL200",
            "course_name": "Operating Systems",
            "exam_title": "Final Exam",
            "teacher_name": "Prof. Kishan",
            "allowed_types": "zip",
            "allow_multiple": "0"
        })

        # First submission succeeds
        res1 = self.client.post("/exam/csl200-final-exam/submit", data={
            "roll_number": "B26DS004",
            "exam_file": (create_sample_zip(b"v1"), "ans.zip")
        })
        self.assertEqual(res1.status_code, 200)

        # Second submission rejected
        res2 = self.client.post("/exam/csl200-final-exam/submit", data={
            "roll_number": "B26DS004",
            "exam_file": (create_sample_zip(b"v2"), "ans.zip")
        })
        self.assertEqual(res2.status_code, 400)
        self.assertIn("Multiple submissions are not allowed", res2.get_json()["error"])

    def test_bulk_and_single_downloads(self):
        self.login_teacher()
        self.client.post("/admin/exams/create", data={
            "course_code": "CSL100",
            "course_name": "Computer Programming Lab",
            "exam_title": "Lab Exam 1",
            "teacher_name": "Prof. Kishan",
            "allowed_types": "zip",
            "allow_multiple": "1"
        })

        # Submit for student 1 & student 2
        self.client.post("/exam/csl100-lab-exam-1/submit", data={
            "roll_number": "B26DS001",
            "exam_file": (create_sample_zip(b"student 1"), "s1.zip")
        })
        self.client.post("/exam/csl100-lab-exam-1/submit", data={
            "roll_number": "B26DS002",
            "exam_file": (create_sample_zip(b"student 2"), "s2.zip")
        })

        # Test single download
        single_res = self.client.get("/admin/download-file/1")
        self.assertEqual(single_res.status_code, 200)
        self.assertEqual(single_res.headers["Content-Disposition"], "attachment; filename=B26DS001.zip")

        # Test bulk download
        bulk_res = self.client.get("/admin/exam/1/download-all")
        self.assertEqual(bulk_res.status_code, 200)
        self.assertEqual(bulk_res.headers["Content-Type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(bulk_res.data)) as zf:
            namelist = zf.namelist()
            self.assertIn("B26DS001.zip", namelist)
            self.assertIn("B26DS002.zip", namelist)
            self.assertEqual(len(namelist), 2)

if __name__ == "__main__":
    unittest.main()
