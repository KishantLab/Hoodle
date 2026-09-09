import os
import io
import zipfile
import unittest
import shutil
from pathlib import Path

# Setup test environment variables
os.environ["SUBMISSION_DIR"] = "/tmp/test_submissions"
os.environ["DB_PATH"] = "/tmp/test_submissions.db"
os.environ["PORT"] = "8099"

from app import app, sanitize_roll_number, init_db

def create_sample_zip(content=b"sample exam code"):
    zip_bytes = io.BytesIO()
    with zipfile.ZipFile(zip_bytes, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("solution.c", content)
    zip_bytes.seek(0)
    return zip_bytes

class TestLabExamPortal(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
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

    def test_roll_sanitization(self):
        self.assertEqual(sanitize_roll_number("cs21b001"), "CS21B001")
        self.assertEqual(sanitize_roll_number("  ee22m015  "), "EE22M015")
        self.assertEqual(sanitize_roll_number("cs-2023_01"), "CS-2023_01")
        self.assertEqual(sanitize_roll_number("cs21!@#$b001"), "CS21B001")

    def test_first_submission(self):
        zip_file = create_sample_zip(b"int main() { return 0; }")
        data = {
            'roll_number': 'cs21b001',
            'exam_file': (zip_file, 'my_solution.zip')
        }
        response = self.client.post('/submit', data=data, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200)
        res_json = response.get_json()
        self.assertTrue(res_json['success'])
        self.assertEqual(res_json['message'], 'Submitted')
        self.assertEqual(res_json['details']['roll_number'], 'CS21B001')
        self.assertEqual(res_json['details']['version'], 1)

        # Check file exists on disk
        student_dir = Path("/tmp/test_submissions/CS21B001")
        self.assertTrue(student_dir.exists())
        latest_file = student_dir / "CS21B001_latest.zip"
        self.assertTrue(latest_file.exists())
        self.assertEqual(latest_file.read_bytes(), (student_dir / res_json['details']['stored_filename']).read_bytes())

    def test_multiple_submissions_latest_considered(self):
        # First submission
        zip_v1 = create_sample_zip(b"Version 1 code")
        res1 = self.client.post('/submit', data={'roll_number': 'cs21b002', 'exam_file': (zip_v1, 'exam.zip')})
        self.assertEqual(res1.get_json()['details']['version'], 1)

        # Second submission
        zip_v2 = create_sample_zip(b"Version 2 updated code")
        res2 = self.client.post('/submit', data={'roll_number': 'CS21B002', 'exam_file': (zip_v2, 'exam.zip')})
        self.assertEqual(res2.status_code, 200)
        json2 = res2.get_json()
        self.assertEqual(json2['details']['version'], 2)
        self.assertTrue(json2['details']['is_update'])

        # Check status endpoint
        status_res = self.client.get('/api/status/cs21b002')
        status_json = status_res.get_json()
        self.assertTrue(status_json['has_submitted'])
        self.assertEqual(status_json['version'], 2)

        # Verify latest file has version 2 content
        student_dir = Path("/tmp/test_submissions/CS21B002")
        latest_file = student_dir / "CS21B002_latest.zip"
        with zipfile.ZipFile(latest_file, "r") as zf:
            self.assertEqual(zf.read("solution.c"), b"Version 2 updated code")

    def test_empty_file_rejected(self):
        empty_file = (io.BytesIO(b""), "empty.zip")
        res = self.client.post('/submit', data={'roll_number': 'cs21b003', 'exam_file': empty_file})
        self.assertEqual(res.status_code, 400)
        self.assertIn("empty", res.get_json()['error'].lower())

    def test_admin_and_download_latest(self):
        # Submit for student A
        self.client.post('/submit', data={'roll_number': 'A01', 'exam_file': (create_sample_zip(b"Student A"), 'a.zip')})
        # Submit for student B v1 and v2
        self.client.post('/submit', data={'roll_number': 'B02', 'exam_file': (create_sample_zip(b"Student B v1"), 'b.zip')})
        self.client.post('/submit', data={'roll_number': 'B02', 'exam_file': (create_sample_zip(b"Student B v2"), 'b.zip')})

        # Test admin download
        download_res = self.client.get('/admin/download-latest')
        self.assertEqual(download_res.status_code, 200)
        self.assertEqual(download_res.headers['Content-Type'], 'application/zip')

        # Verify zip contains only latest files for A01 and B02
        with zipfile.ZipFile(io.BytesIO(download_res.data)) as zf:
            namelist = zf.namelist()
            self.assertIn("A01_latest.zip", namelist)
            self.assertIn("B02_latest.zip", namelist)
            self.assertEqual(len(namelist), 2)
            # Check B02 content is v2
            b_zip_bytes = io.BytesIO(zf.read("B02_latest.zip"))
            with zipfile.ZipFile(b_zip_bytes) as b_zf:
                self.assertEqual(b_zf.read("solution.c"), b"Student B v2")

if __name__ == "__main__":
    unittest.main()
