import os
import sys
import unittest
import json
import sqlite3

# Set testing configuration
os.environ["TESTING"] = "True"

from app import app, init_db, get_course_grades_matrix

class TestLMSFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        cls.client = app.test_client()
        with app.app_context():
            init_db()

    def login(self, username, password="Password123!"):
        return self.client.post("/login", data={
            "identifier": username,
            "password": password
        }, follow_redirects=True)

    def logout(self):
        return self.client.get("/logout", follow_redirects=True)

    def test_01_api_app_version(self):
        """Test /api/app/version endpoint returns correct structure and values."""
        res = self.client.get("/api/app/version")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data.decode("utf-8"))
        self.assertEqual(data["latest_version"], "1.2.0")
        self.assertEqual(data["version_code"], 3)
        self.assertIn("download_url", data)
        self.assertIn("changelog", data)
        self.assertTrue(len(data["changelog"]) > 0)
        print("✅ /api/app/version passed:", data["latest_version"])

    def test_02_student_navbar_messaging_hidden(self):
        """Test Messages button is not in top navbar for student, but visible for teacher."""
        # Check student dashboard
        with self.client.session_transaction() as sess:
            sess["user_id"] = 27
            sess["username"] = "student_test"
            sess["role"] = "student"
            sess["display_name"] = "Student Test"

        res = self.client.get("/dashboard")
        html = res.data.decode("utf-8")
        # Ensure '💬 Messages' nav-pill is NOT present for student
        self.assertNotIn('<span>Messages</span>', html)

        # Check teacher dashboard
        with self.client.session_transaction() as sess:
            sess["user_id"] = 10
            sess["username"] = "teacher_test"
            sess["role"] = "teacher"
            sess["display_name"] = "Teacher Test"

        res = self.client.get("/dashboard")
        html = res.data.decode("utf-8")
        self.assertIn('<span>Messages</span>', html)
        print("✅ Student navbar messaging restriction passed.")

    def test_03_student_messaging_course_scoping(self):
        """Test student messaging restriction to course-registered teachers & TAs."""
        # Create test users and course
        from app import get_db, hash_password
        conn = get_db()
        c = conn.cursor()
        now = "2026-09-13 12:00:00"
        
        # Insert student A, student B, teacher X, teacher Y
        c.execute("INSERT OR IGNORE INTO users (username, email, password_hash, display_name, role, created_at) VALUES ('test_stu_a', 'stua@test.edu', ?, 'Student A', 'student', ?)", (hash_password("Pass123!"), now))
        c.execute("INSERT OR IGNORE INTO users (username, email, password_hash, display_name, role, created_at) VALUES ('test_stu_b', 'stub@test.edu', ?, 'Student B', 'student', ?)", (hash_password("Pass123!"), now))
        c.execute("INSERT OR IGNORE INTO users (username, email, password_hash, display_name, role, created_at) VALUES ('test_tch_x', 'tchx@test.edu', ?, 'Teacher X', 'teacher', ?)", (hash_password("Pass123!"), now))
        c.execute("INSERT OR IGNORE INTO users (username, email, password_hash, display_name, role, created_at) VALUES ('test_tch_y', 'tchy@test.edu', ?, 'Teacher Y', 'teacher', ?)", (hash_password("Pass123!"), now))
        
        stu_a = conn.execute("SELECT id FROM users WHERE username = 'test_stu_a'").fetchone()["id"]
        stu_b = conn.execute("SELECT id FROM users WHERE username = 'test_stu_b'").fetchone()["id"]
        tch_x = conn.execute("SELECT id FROM users WHERE username = 'test_tch_x'").fetchone()["id"]
        tch_y = conn.execute("SELECT id FROM users WHERE username = 'test_tch_y'").fetchone()["id"]

        # Course 1001 taught by tch_x, enrolled stu_a
        c.execute("INSERT OR IGNORE INTO courses (id, code, title, section, join_code, teacher_id, created_at) VALUES (1001, 'TEST101', 'Test Course 101', 'Sec 1', 'J1001', ?, ?)", (tch_x, now))
        c.execute("INSERT OR IGNORE INTO course_enrollments (course_id, user_id, role, enrolled_at) VALUES (1001, ?, 'student', ?)", (stu_a, now))

        # Course 1002 taught by tch_y, enrolled stu_b
        c.execute("INSERT OR IGNORE INTO courses (id, code, title, section, join_code, teacher_id, created_at) VALUES (1002, 'TEST102', 'Test Course 102', 'Sec 2', 'J1002', ?, ?)", (tch_y, now))
        c.execute("INSERT OR IGNORE INTO course_enrollments (course_id, user_id, role, enrolled_at) VALUES (1002, ?, 'student', ?)", (stu_b, now))
        conn.commit()
        conn.close()

        # Session as stu_a
        with self.client.session_transaction() as sess:
            sess["user_id"] = stu_a
            sess["username"] = "test_stu_a"
            sess["role"] = "student"
            sess["display_name"] = "Student A"

        # 1. Student A tries to message Student B -> MUST BE 403
        res = self.client.post("/api/messages/send", json={
            "recipient_id": stu_b,
            "message": "Hey Student B!",
            "course_id": 1001
        })
        self.assertEqual(res.status_code, 403)
        print("✅ Student A -> Student B blocked (403).")

        # 2. Student A tries to message Teacher Y (not registered in Course 1001) -> MUST BE 403
        res = self.client.post("/api/messages/send", json={
            "recipient_id": tch_y,
            "message": "Hey Teacher Y!",
            "course_id": 1001
        })
        self.assertEqual(res.status_code, 403)
        print("✅ Student A -> Unregistered Teacher Y blocked (403).")

        # 3. Student A messages Teacher X (registered instructor in Course 1001) -> MUST BE 200
        res = self.client.post("/api/messages/send", json={
            "recipient_id": tch_x,
            "message": "Hello Teacher X, I have a doubt.",
            "course_id": 1001
        })
        self.assertEqual(res.status_code, 200)
        print("✅ Student A -> Course Teacher X allowed (200).")

        # 4. Student A views messages with Teacher Y -> MUST BE 403
        res = self.client.get(f"/api/messages/{tch_y}")
        self.assertEqual(res.status_code, 403)
        print("✅ Student A -> View messages with Teacher Y blocked (403).")

        # 5. Student A views messages with Teacher X -> MUST BE 200
        res = self.client.get(f"/api/messages/{tch_x}")
        self.assertEqual(res.status_code, 200)
        print("✅ Student A -> View messages with Teacher X allowed (200).")

        self.logout()

    def test_04_gradebook_matrix_and_google_sheet_feed(self):
        """Test Gradebook matrix calculation and CSV sheet feed endpoint."""
        from app import get_db
        conn = get_db()
        course = conn.execute("SELECT id, gradebook_feed_token FROM courses LIMIT 1").fetchone()
        course_id = course["id"]
        token = course["gradebook_feed_token"]
        conn.close()

        # Check get_course_grades_matrix
        matrix_data = get_course_grades_matrix(course_id)
        self.assertIn("matrix", matrix_data)
        headers = matrix_data["matrix"][0]
        self.assertIn("Roll Number", headers)
        self.assertIn("Student Name", headers)
        self.assertIn("Email", headers)
        self.assertIn("Section", headers)
        self.assertIn("Final Weighted Score (100)", headers)
        self.assertIn("Letter Grade", headers)
        print(f"✅ Gradebook matrix generated for course {course_id}. Headers: {headers[:6]}...")

        # Check /api/courses/<id>/grades/sheet-feed?token=...
        # 1. Without token -> 403
        res = self.client.get(f"/api/courses/{course_id}/grades/sheet-feed")
        self.assertEqual(res.status_code, 403)
        print("✅ Sheet feed without token blocked (403).")

        # 2. With invalid token -> 403
        res = self.client.get(f"/api/courses/{course_id}/grades/sheet-feed?token=wrong_token")
        self.assertEqual(res.status_code, 403)
        print("✅ Sheet feed with invalid token blocked (403).")

        # 3. With valid token -> 200 and CSV content
        res = self.client.get(f"/api/courses/{course_id}/grades/sheet-feed?token={token}")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/csv", res.headers.get("Content-Type", ""))
        csv_text = res.data.decode("utf-8")
        self.assertIn("Roll Number", csv_text)
        self.assertIn("Section", csv_text)
        print("✅ Sheet feed with valid token returned CSV (200).")

    def test_05_update_app_button_in_ui(self):
        """Verify Update App (.apk) button and badge in base.html."""
        self.login("student1", "student123")
        res = self.client.get("/dashboard")
        html = res.data.decode("utf-8")
        self.assertIn("Update App (.apk)", html)
        self.assertIn("Latest v1.2", html)
        self.assertIn("v1.2.0 (Build 3)", html)
        self.logout()
        print("✅ Update App (.apk) dropdown button and modal card verified in UI.")

if __name__ == "__main__":
    unittest.main()
