#!/usr/bin/env python3
"""
ACCLLMS - Learning Management System & Classroom Portal
Accelerated Computing Research Lab (ACCL) • Indian Institute of Technology Bhilai (IIT Bhilai)
Author: Kishan Tamboli (PhD)
"""

import os
import re
import sys
import time
import shutil
import sqlite3
import hashlib
import zipfile
import io
import json
import csv
import xml.etree.ElementTree as ET
import secrets
from datetime import datetime
from pathlib import Path
from functools import wraps

from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    send_from_directory,
    send_file,
    redirect,
    url_for,
    session,
    flash,
    abort,
    Response
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

def hash_password(password):
    return generate_password_hash(password, method="pbkdf2:sha256")


# --- Directory & Environment Configuration ---
BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = Path(os.environ.get("STORAGE_DIR", BASE_DIR / "storage"))
LOCKERS_DIR = STORAGE_DIR / "lockers"
SUBMISSIONS_DIR = STORAGE_DIR / "submissions"
ATTACHMENTS_DIR = STORAGE_DIR / "attachments"
STATIC_DIR = BASE_DIR / "static"
UPLOADS_DIR = STATIC_DIR / "uploads"
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "accl_lms.db"))

PORT = int(os.environ.get("PORT", 8095))
HOST = os.environ.get("HOST", "0.0.0.0")
MAX_CONTENT_LENGTH = 300 * 1024 * 1024  # 300 MB max upload limit
SECRET_KEY = os.environ.get("SECRET_KEY", "accl_lms_classroom_secret_2026_super_secure")
DEFAULT_LOCKER_QUOTA = 500 * 1024 * 1024  # 500 MB default private storage quota

# Create directories if they do not exist
for d in (STORAGE_DIR, LOCKERS_DIR, SUBMISSIONS_DIR, ATTACHMENTS_DIR, STATIC_DIR, UPLOADS_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.config["STORAGE_DIR"] = STORAGE_DIR
app.config["LOCKERS_DIR"] = LOCKERS_DIR
app.config["SUBMISSIONS_DIR"] = SUBMISSIONS_DIR
app.config["ATTACHMENTS_DIR"] = ATTACHMENTS_DIR
app.config["DB_PATH"] = DB_PATH

# Enable proper reverse-proxy handling (Nginx /lms/)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


# --- Template Filters & Utilities ---

def parse_iso_datetime(dt_str):
    if not dt_str:
        return None
    try:
        clean = dt_str.replace("T", " ").strip()
        if len(clean) == 16:
            return datetime.strptime(clean, "%Y-%m-%d %H:%M")
        return datetime.strptime(clean[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def format_datetime_display(dt_str):
    dt = parse_iso_datetime(dt_str)
    if not dt:
        return ""
    return dt.strftime("%d %b %Y, %I:%M %p")


def format_file_size(num_bytes):
    if num_bytes is None:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


def calc_sha256(filepath):
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


@app.template_filter("format_dt")
def filter_format_dt(s):
    return format_datetime_display(s)


@app.template_filter("filesize")
def filter_filesize(num):
    return format_file_size(num)


@app.template_filter("nl2br")
def filter_nl2br(s):
    if not s:
        return ""
    from markupsafe import Markup, escape
    return Markup("<br>".join(escape(s).splitlines()))


# --- Database Connection & Schema Setup ---

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    # 1. Users Table
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            roll_number TEXT UNIQUE,
            email TEXT,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT DEFAULT 'student',
            storage_quota_bytes INTEGER DEFAULT 524288000,
            must_change_password INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    # 2. Courses Table
    c.execute("""
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            title TEXT NOT NULL,
            section TEXT DEFAULT 'Lab 1',
            description TEXT,
            join_code TEXT UNIQUE NOT NULL,
            teacher_id INTEGER NOT NULL,
            theme_color TEXT DEFAULT 'indigo',
            is_archived INTEGER DEFAULT 0,
            active_exam_id INTEGER DEFAULT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (teacher_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # 3. Course Enrollments Table
    c.execute("""
        CREATE TABLE IF NOT EXISTS course_enrollments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT DEFAULT 'student',
            enrolled_at TEXT NOT NULL,
            UNIQUE(course_id, user_id),
            FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # 4. Topics Table (like Google Classroom topics)
    c.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            display_order INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE
        )
    """)

    # 5. Coursework Table (Assignments, Exams, Materials)
    c.execute("""
        CREATE TABLE IF NOT EXISTS coursework (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            topic_id INTEGER,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            points INTEGER DEFAULT 100,
            due_date TEXT,
            start_time TEXT,
            end_time TEXT,
            allowed_types TEXT DEFAULT 'all',
            allow_multiple INTEGER DEFAULT 1,
            allow_late INTEGER DEFAULT 1,
            is_exam_mode INTEGER DEFAULT 0,
            labs TEXT DEFAULT 'Lab 1, Lab 2, Lab 3, CC-101',
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE,
            FOREIGN KEY (topic_id) REFERENCES topics(id) ON DELETE SET NULL,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # 6. Coursework Attachments Table (Lectures, templates, problem statements)
    c.execute("""
        CREATE TABLE IF NOT EXISTS coursework_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coursework_id INTEGER NOT NULL,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            uploaded_at TEXT NOT NULL,
            FOREIGN KEY (coursework_id) REFERENCES coursework(id) ON DELETE CASCADE
        )
    """)

    # 7. Submissions Table (Assignments & Exams)
    c.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coursework_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            roll_number TEXT NOT NULL,
            student_name TEXT NOT NULL,
            lab_name TEXT DEFAULT 'Lab 1',
            status TEXT DEFAULT 'turned_in',
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            ip_address TEXT,
            submitted_at TEXT NOT NULL,
            version INTEGER DEFAULT 1,
            is_late INTEGER DEFAULT 0,
            late_minutes INTEGER DEFAULT 0,
            grade REAL DEFAULT NULL,
            feedback TEXT,
            graded_by INTEGER DEFAULT NULL,
            graded_at TEXT,
            receipt_token TEXT UNIQUE NOT NULL,
            FOREIGN KEY (coursework_id) REFERENCES coursework(id) ON DELETE CASCADE,
            FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(coursework_id, student_id)
        )
    """)

    # 8. Student Private Locker Files
    c.execute("""
        CREATE TABLE IF NOT EXISTS student_locker_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            mime_type TEXT,
            uploaded_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # 9. Announcements Feed
    c.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            attachment_name TEXT,
            attachment_path TEXT,
            attachment_size INTEGER,
            is_pinned INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # 10. Comments Table (Public on stream, or Private on assignments)
    c.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            context_type TEXT NOT NULL,
            context_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # 11. Attendance Sessions Table
    c.execute("""
        CREATE TABLE IF NOT EXISTS attendance_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            session_type TEXT NOT NULL,
            session_date TEXT NOT NULL,
            start_time TEXT,
            end_time TEXT,
            is_active INTEGER DEFAULT 1,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # 12. Attendance Logs Table (Anti-Proxy Atomic Logging)
    c.execute("""
        CREATE TABLE IF NOT EXISTS attendance_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            session_id INTEGER,
            student_id INTEGER NOT NULL,
            roll_number TEXT NOT NULL,
            student_name TEXT NOT NULL,
            section TEXT DEFAULT 'Section A',
            session_type TEXT NOT NULL,
            attendance_date TEXT NOT NULL,
            status TEXT DEFAULT 'PRESENT',
            method TEXT DEFAULT 'QR_SCAN',
            ip_address TEXT,
            marked_at TEXT NOT NULL,
            attendance_key TEXT UNIQUE NOT NULL,
            FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES attendance_sessions(id) ON DELETE SET NULL,
            FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_att_course_date ON attendance_logs (course_id, attendance_date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_att_student ON attendance_logs (student_id)")

    # 13. Course Grading Categories & Weights (Canvas LMS Style)
    c.execute("""
        CREATE TABLE IF NOT EXISTS course_grading_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 0.0,
            is_attendance INTEGER DEFAULT 0,
            drop_lowest INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_cat_course ON course_grading_categories (course_id)")

    # Migration for courses table
    c.execute("PRAGMA table_info(courses)")
    course_cols = [row["name"] for row in c.fetchall()]
    if "grading_formula" not in course_cols:
        c.execute("ALTER TABLE courses ADD COLUMN grading_formula TEXT DEFAULT NULL")
    if "grade_calculation_mode" not in course_cols:
        c.execute("ALTER TABLE courses ADD COLUMN grade_calculation_mode TEXT DEFAULT 'weighted_categories'")

    # Migration for coursework table
    c.execute("PRAGMA table_info(coursework)")
    cw_cols = [row["name"] for row in c.fetchall()]
    if "category_id" not in cw_cols:
        c.execute("ALTER TABLE coursework ADD COLUMN category_id INTEGER DEFAULT NULL")

    conn.commit()

    # Pre-seed initial default accounts if not existing
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Superadmin
    c.execute("SELECT id FROM users WHERE LOWER(username) = 'admin'")
    if not c.fetchone():
        pwd = hash_password("admin@accl")
        c.execute("""
            INSERT INTO users (username, roll_number, email, password_hash, display_name, role, created_at)
            VALUES ('admin', 'ADMIN', 'admin@iitbhilai.ac.in', ?, 'LMS Administrator', 'admin', ?)
        """, (pwd, now_str))

    # Teacher Kishan
    c.execute("SELECT id FROM users WHERE LOWER(username) = 'kishan'")
    t_user = c.fetchone()
    if not t_user:
        pwd = hash_password("password123")
        c.execute("""
            INSERT INTO users (username, roll_number, email, password_hash, display_name, role, created_at)
            VALUES ('kishan', 'FAC001', 'kishan@iitbhilai.ac.in', ?, 'Prof. Kishan Tamboli', 'teacher', ?)
        """, (pwd, now_str))
        teacher_id = c.lastrowid
    else:
        teacher_id = t_user["id"]

    # Student Aarav
    c.execute("SELECT id FROM users WHERE LOWER(username) = 'student1'")
    s_user = c.fetchone()
    if not s_user:
        pwd = hash_password("student123")
        c.execute("""
            INSERT INTO users (username, roll_number, email, password_hash, display_name, role, created_at)
            VALUES ('student1', 'B26DS001', 'b26ds001@iitbhilai.ac.in', ?, 'Aarav Sharma', 'student', ?)
        """, (pwd, now_str))
        student_id = c.lastrowid
    else:
        student_id = s_user["id"]

    # Pre-seed a demonstration Course if empty
    c.execute("SELECT COUNT(*) as count FROM courses")
    if c.fetchone()["count"] == 0:
        c.execute("""
            INSERT INTO courses (code, title, section, description, join_code, teacher_id, theme_color, created_at)
            VALUES ('CSL100', 'Introduction to Computing & Parallel Architectures', 'Lab Section A',
                    'Foundational computer systems course covering C programming, memory hierarchy, multi-core concepts, and GPU acceleration.',
                    'ACCL26', ?, 'indigo', ?)
        """, (teacher_id, now_str))
        course_id = c.lastrowid

        # Enroll student in the demo course
        c.execute("""
            INSERT OR IGNORE INTO course_enrollments (course_id, user_id, role, enrolled_at)
            VALUES (?, ?, 'student', ?)
        """, (course_id, student_id, now_str))

        # Seed topics
        c.execute("INSERT INTO topics (course_id, name, display_order, created_at) VALUES (?, 'Week 1: Foundations of C & Linux', 1, ?)", (course_id, now_str))
        t1_id = c.lastrowid
        c.execute("INSERT INTO topics (course_id, name, display_order, created_at) VALUES (?, 'Lab Exams & Timed Assessments', 2, ?)", (course_id, now_str))
        t2_id = c.lastrowid

        # Seed welcome announcement
        c.execute("""
            INSERT INTO announcements (course_id, user_id, content, is_pinned, created_at)
            VALUES (?, ?, 'Welcome to CSL100! All lab assignments, lecture materials, and timed laboratory examinations will be administered through ACCLLMS. Be sure to check your Private Locker for personal file storage.', 1, ?)
        """, (course_id, teacher_id, now_str))

        # Seed sample assignment
        c.execute("""
            INSERT INTO coursework (course_id, topic_id, type, title, description, points, due_date, allowed_types, allow_multiple, allow_late, is_exam_mode, created_by, created_at)
            VALUES (?, ?, 'assignment', 'Lab Assignment 1: Dynamic Memory Allocation in C',
                    'Implement a custom memory allocator utilizing malloc/free wrappers with buffer overflow guard bands. Submit your code archive (.c or .zip) or attach directly from your Private Locker.',
                    100, '2026-09-30 23:59:00', 'all', 1, 1, 0, ?, ?)
        """, (course_id, t1_id, teacher_id, now_str))

        # Seed sample exam with Exam Mode available
        c.execute("""
            INSERT INTO coursework (course_id, topic_id, type, title, description, points, due_date, start_time, end_time, allowed_types, allow_multiple, allow_late, is_exam_mode, labs, created_by, created_at)
            VALUES (?, ?, 'exam', 'Mid-Term Lab Examination: Pointer Arithmetic & File I/O',
                    'Laboratory Practical Examination. Strict Exam Mode: When active, students cannot browse materials or private lockers and must submit only a single .zip archive before the timer expires.',
                    100, '2026-09-20 18:00:00', '2026-09-20 15:00:00', '2026-09-20 18:00:00', 'zip', 1, 1, 0, 'Lab 1, Lab 2, Lab 3, CC-101', ?, ?)
        """, (course_id, t2_id, teacher_id, now_str))

    # Upgrade any scrypt hashes for cross-version compatibility
    c.execute("SELECT id, username, password_hash FROM users")
    for u in c.fetchall():
        if u["password_hash"].startswith("scrypt"):
            if u["username"] == "admin":
                c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password("admin@accl"), u["id"]))
            elif u["username"] == "kishan":
                c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password("password123"), u["id"]))
            elif u["username"] == "student1":
                c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password("student123"), u["id"]))

    conn.commit()
    conn.close()


# --- Authentication & Authorization Helpers ---

def get_current_user():
    if "user_id" not in session:
        return None
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()
    return user


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please sign in to access this page.", "warning")
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated_function


def teacher_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            flash("Please sign in to proceed.", "warning")
            return redirect(url_for("login", next=request.path))
            
        role = session.get("role")
        user_id = session.get("user_id")
        
        # 1. Global teacher or admin role
        if role in ("teacher", "admin"):
            return f(*args, **kwargs)
            
        # 2. Course-specific instructor or co-teacher check
        course_id = kwargs.get("course_id")
        if course_id:
            conn = get_db()
            is_instr = conn.execute("SELECT 1 FROM courses WHERE id = ? AND teacher_id = ?", (course_id, user_id)).fetchone()
            if is_instr:
                conn.close()
                return f(*args, **kwargs)
                
            co_teacher = conn.execute("""
                SELECT 1 FROM course_enrollments
                WHERE course_id = ? AND user_id = ? AND role IN ('teacher', 'ta', 'co-teacher')
            """, (course_id, user_id)).fetchone()
            conn.close()
            if co_teacher:
                return f(*args, **kwargs)

        if request.path.startswith("/api/"):
            return jsonify({"error": "Access restricted to faculty and instructors."}), 403
        flash("Access restricted to faculty, instructors, and co-teachers.", "danger")
        return redirect(url_for("dashboard"))
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please sign in to proceed.", "warning")
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            flash("Administrator privileges required.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated_function


# --- Strict Exam Mode Lockdown System ---

def get_active_exam_lockdown_for_student(user_id):
    """
    Checks if the student is currently enrolled in any course where an Exam
    is actively undergoing 'Strict Exam Mode'.
    When active, students cannot access any other LMS pages, courses, streams,
    materials, or private lockers—they are locked exclusively to the exam submission page.
    """
    conn = get_db()
    row = conn.execute("""
        SELECT cw.id as coursework_id, cw.title, cw.course_id, c.code as course_code,
               cw.start_time, cw.end_time, cw.is_exam_mode
        FROM coursework cw
        JOIN courses c ON cw.course_id = c.id
        JOIN course_enrollments ce ON ce.course_id = c.id
        WHERE ce.user_id = ? AND ce.role = 'student'
          AND (cw.is_exam_mode = 1 OR c.active_exam_id = cw.id)
        LIMIT 1
    """, (user_id,)).fetchone()

    if not row:
        conn.close()
        return None

    now = datetime.now()
    st = parse_iso_datetime(row["start_time"])
    et = parse_iso_datetime(row["end_time"])

    # If exam end time is specified and has passed, automatically disable exam lockdown
    if et and now > et:
        # Auto-disable in database
        conn.execute("UPDATE coursework SET is_exam_mode = 0 WHERE id = ?", (row["coursework_id"],))
        conn.execute("UPDATE courses SET active_exam_id = NULL WHERE active_exam_id = ?", (row["coursework_id"],))
        conn.commit()
        conn.close()
        return None

    # If is_exam_mode is 1 (explicitly toggled on by teacher), lockdown is active!
    if row["is_exam_mode"] == 1:
        res = dict(row)
        conn.close()
        return res

    # If scheduled start and end window is active
    if st and et and st <= now <= et:
        res = dict(row)
        conn.close()
        return res

    conn.close()
    return None


@app.before_request
def enforce_exam_lockdown():
    """
    Global request hook:
    If a logged-in student has an active Exam Mode lockdown in place:
    Block access to stream, classwork materials, locker, other courses, etc.,
    and redirect them directly to the exam interface!
    """
    if "user_id" not in session or session.get("role") != "student":
        return

    # Whitelisted endpoints during exam mode
    endpoint = request.endpoint or ""
    allowed_endpoints = {
        "exam_view", "exam_submit", "exam_receipt", "logout", "static", "login",
        "attend_scan_landing", "attend_submit", "course_attendance"
    }

    if endpoint in allowed_endpoints:
        return

    active_exam = get_active_exam_lockdown_for_student(session["user_id"])
    if active_exam:
        flash(
            f"🔒 STRICT EXAM LOCKDOWN ACTIVE for {active_exam['course_code']}: {active_exam['title']}. "
            "Access to lecture materials, stream discussions, and your private locker is restricted until the exam ends.",
            "danger"
        )
        return redirect(url_for(
            "exam_view",
            course_id=active_exam["course_id"],
            coursework_id=active_exam["coursework_id"]
        ))


@app.context_processor
def inject_global_variables():
    user = get_current_user()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    active_exam = None
    if user and user["role"] == "student":
        active_exam = get_active_exam_lockdown_for_student(user["id"])
    return {
        "current_user": user,
        "now_iso": now_str,
        "active_exam_lockdown": active_exam
    }


# --- Authentication Routes ---

@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")

        if not identifier or not password:
            flash("Please provide your username/roll number and password.", "danger")
            return render_template("login.html")

        conn = get_db()
        user = conn.execute("""
            SELECT * FROM users
            WHERE LOWER(username) = LOWER(?)
               OR LOWER(roll_number) = LOWER(?)
               OR LOWER(email) = LOWER(?)
        """, (identifier, identifier, identifier)).fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["display_name"] = user["display_name"]
            session["role"] = user["role"]
            session["roll_number"] = user["roll_number"]

            flash(f"Welcome back, {user['display_name']}!", "success")
            next_url = request.args.get("next")
            if next_url and next_url.startswith("/"):
                return redirect(next_url)
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid credentials. Please check your username/roll number and password.", "danger")

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        roll_number = request.form.get("roll_number", "").strip().upper()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        role = request.form.get("role", "student")

        if role not in ("student", "teacher"):
            role = "student"

        if not full_name or not roll_number or not password:
            flash("Full Name, Roll Number, and Password are required.", "danger")
            return render_template("login.html", register_active=True)

        if password != confirm_password:
            flash("Passwords do not match. Please verify.", "danger")
            return render_template("login.html", register_active=True)

        if len(password) < 6:
            flash("Password must contain at least 6 characters.", "danger")
            return render_template("login.html", register_active=True)

        username = roll_number.lower()

        conn = get_db()
        exists = conn.execute("""
            SELECT id FROM users
            WHERE LOWER(username) = ? OR LOWER(roll_number) = ? OR (email != '' AND LOWER(email) = ?)
        """, (username, roll_number.lower(), email)).fetchone()

        if exists:
            conn.close()
            flash("An account with this Roll Number or Email already exists. Please log in.", "warning")
            return render_template("login.html", register_active=False)

        pwd_hash = hash_password(password)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn.execute("""
            INSERT INTO users (username, roll_number, email, password_hash, display_name, role, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (username, roll_number, email, pwd_hash, full_name, role, now_str))
        conn.commit()
        conn.close()

        flash("Registration successful! You may now sign in.", "success")
        return redirect(url_for("login"))

    return render_template("login.html", register_active=True)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("login"))


# --- Main Dashboard & Course Hub ---

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = get_current_user()
    conn = get_db()

    courses = []
    upcoming_deadlines = []
    locker_stats = {"used_bytes": 0, "quota_bytes": user["storage_quota_bytes"], "percent": 0}

    if user["role"] == "student":
        # Courses enrolled by student
        courses = conn.execute("""
            SELECT c.*, u.display_name as teacher_name,
                   (SELECT COUNT(*) FROM coursework cw WHERE cw.course_id = c.id) as total_work
            FROM courses c
            JOIN course_enrollments ce ON c.id = ce.course_id
            JOIN users u ON c.teacher_id = u.id
            WHERE ce.user_id = ? AND c.is_archived = 0
            ORDER BY c.created_at DESC
        """, (user["id"],)).fetchall()

        # Upcoming assignments with due dates
        upcoming_deadlines = conn.execute("""
            SELECT cw.*, c.code as course_code, c.title as course_title,
                   s.status as submission_status, s.grade
            FROM coursework cw
            JOIN courses c ON cw.course_id = c.id
            JOIN course_enrollments ce ON ce.course_id = c.id
            LEFT JOIN submissions s ON s.coursework_id = cw.id AND s.student_id = ?
            WHERE ce.user_id = ? AND cw.due_date IS NOT NULL AND cw.type != 'material'
            ORDER BY cw.due_date ASC
            LIMIT 5
        """, (user["id"], user["id"])).fetchall()

        # Locker storage usage
        usage_row = conn.execute("""
            SELECT COALESCE(SUM(file_size), 0) as total_used, COUNT(*) as file_count
            FROM student_locker_files WHERE user_id = ?
        """, (user["id"],)).fetchone()
        used = usage_row["total_used"] or 0
        quota = user["storage_quota_bytes"] or DEFAULT_LOCKER_QUOTA
        locker_stats = {
            "used_bytes": used,
            "quota_bytes": quota,
            "used_formatted": format_file_size(used),
            "quota_formatted": format_file_size(quota),
            "percent": min(100, round((used / quota) * 100, 1)) if quota > 0 else 0,
            "file_count": usage_row["file_count"]
        }

    else:
        # Teacher or Admin
        if user["role"] == "admin":
            courses = conn.execute("""
                SELECT c.*, u.display_name as teacher_name,
                       (SELECT COUNT(*) FROM course_enrollments ce WHERE ce.course_id = c.id AND ce.role = 'student') as student_count,
                       (SELECT COUNT(*) FROM coursework cw WHERE cw.course_id = c.id) as total_work
                FROM courses c
                JOIN users u ON c.teacher_id = u.id
                ORDER BY c.is_archived ASC, c.created_at DESC
            """).fetchall()
        else:
            courses = conn.execute("""
                SELECT c.*, u.display_name as teacher_name,
                       (SELECT COUNT(*) FROM course_enrollments ce WHERE ce.course_id = c.id AND ce.role = 'student') as student_count,
                       (SELECT COUNT(*) FROM coursework cw WHERE cw.course_id = c.id) as total_work
                FROM courses c
                JOIN users u ON c.teacher_id = u.id
                WHERE c.teacher_id = ?
                ORDER BY c.is_archived ASC, c.created_at DESC
            """, (user["id"],)).fetchall()

    conn.close()
    return render_template(
        "dashboard.html",
        courses=courses,
        upcoming_deadlines=upcoming_deadlines,
        locker_stats=locker_stats
    )


# --- Course Management & Enrollment ---

@app.route("/courses/create", methods=["POST"])
@teacher_required
def create_course():
    code = request.form.get("code", "").strip().upper()
    title = request.form.get("title", "").strip()
    section = request.form.get("section", "Section A").strip()
    description = request.form.get("description", "").strip()
    theme_color = request.form.get("theme_color", "indigo").strip()

    if not code or not title:
        flash("Course Code and Title are required.", "danger")
        return redirect(url_for("dashboard"))

    join_code = secrets.token_hex(3).upper()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO courses (code, title, section, description, join_code, teacher_id, theme_color, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (code, title, section, description, join_code, session["user_id"], theme_color, now_str))
    course_id = c.lastrowid

    c.execute("""
        INSERT INTO course_enrollments (course_id, user_id, role, enrolled_at)
        VALUES (?, ?, 'teacher', ?)
    """, (course_id, session["user_id"], now_str))

    c.execute("""
        INSERT INTO topics (course_id, name, display_order, created_at)
        VALUES (?, 'General Course Information', 1, ?)
    """, (course_id, now_str))

    conn.commit()
    conn.close()

    flash(f"Course '{code}: {title}' created successfully! Class Code: {join_code}", "success")
    return redirect(url_for("course_stream", course_id=course_id))


@app.route("/courses/join", methods=["POST"])
@login_required
def join_course():
    join_code = request.form.get("join_code", "").strip().upper()
    if not join_code:
        flash("Please enter a valid Class Code.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_db()
    course = conn.execute("SELECT * FROM courses WHERE UPPER(join_code) = ? AND is_archived = 0", (join_code,)).fetchone()

    if not course:
        conn.close()
        flash("No active course found with that Class Code. Please verify with your instructor.", "danger")
        return redirect(url_for("dashboard"))

    user = get_current_user()
    user_id = user["id"]

    existing = conn.execute("""
        SELECT * FROM course_enrollments WHERE course_id = ? AND user_id = ?
    """, (course["id"], user_id)).fetchone()

    if existing:
        conn.close()
        flash(f"You are already enrolled in {course['code']} - {course['title']}.", "info")
        return redirect(url_for("course_stream", course_id=course["id"]))

    requested_role = request.form.get("enrollment_role", "student").strip().lower()
    # Default is students; only users with teacher or admin global roles can choose to join as a co-teacher
    enrollment_role = "student"
    if user["role"] in ("teacher", "admin") and requested_role in ("ta", "teacher", "co-teacher"):
        enrollment_role = "ta"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO course_enrollments (course_id, user_id, role, enrolled_at)
        VALUES (?, ?, ?, ?)
    """, (course["id"], user_id, enrollment_role, now_str))
    conn.commit()
    conn.close()

    role_label = "Co-Teacher" if enrollment_role == "ta" else "Student"
    flash(f"Successfully joined {course['code']}: {course['title']} as {role_label}!", "success")
    return redirect(url_for("course_stream", course_id=course["id"]))


@app.route("/courses/<int:course_id>/reset-join-code", methods=["POST"])
@teacher_required
def reset_join_code(course_id):
    new_code = secrets.token_hex(3).upper()
    conn = get_db()
    conn.execute("UPDATE courses SET join_code = ? WHERE id = ?", (new_code, course_id))
    conn.commit()
    conn.close()
    flash(f"Class code reset successfully! New Code: {new_code}", "success")
    return redirect(url_for("course_people", course_id=course_id))


# --- Google Classroom Tabs: Stream, Classwork, People, Grades ---

def get_course_or_404(course_id):
    conn = get_db()
    course = conn.execute("""
        SELECT c.*, u.display_name as teacher_name, u.email as teacher_email
        FROM courses c
        JOIN users u ON c.teacher_id = u.id
        WHERE c.id = ?
    """, (course_id,)).fetchone()
    conn.close()
    if not course:
        abort(404, "Course not found")
    return course


@app.route("/courses/<int:course_id>")
@login_required
def course_home(course_id):
    return redirect(url_for("course_stream", course_id=course_id))


# --- Tab 1: Stream ---

@app.route("/courses/<int:course_id>/stream")
@login_required
def course_stream(course_id):
    course = get_course_or_404(course_id)
    conn = get_db()

    user_id = session["user_id"]
    role = session.get("role")
    if role not in ("teacher", "admin"):
        enrollment = conn.execute("""
            SELECT * FROM course_enrollments WHERE course_id = ? AND user_id = ?
        """, (course_id, user_id)).fetchone()
        if not enrollment:
            conn.close()
            flash("You are not enrolled in this course.", "danger")
            return redirect(url_for("dashboard"))

    announcements_raw = conn.execute("""
        SELECT a.*, u.display_name as author_name, u.role as author_role
        FROM announcements a
        JOIN users u ON a.user_id = u.id
        WHERE a.course_id = ?
        ORDER BY a.is_pinned DESC, a.created_at DESC
    """, (course_id,)).fetchall()

    announcements = []
    for row in announcements_raw:
        item = dict(row)
        comments = conn.execute("""
            SELECT cm.*, u.display_name, u.role, u.roll_number
            FROM comments cm
            JOIN users u ON cm.user_id = u.id
            WHERE cm.context_type = 'announcement' AND cm.context_id = ?
            ORDER BY cm.created_at ASC
        """, (item["id"],)).fetchall()
        item["comments"] = comments
        announcements.append(item)

    conn.close()
    return render_template("course_stream.html", course=course, announcements=announcements, active_tab="stream")


@app.route("/courses/<int:course_id>/announcements", methods=["POST"])
@login_required
def post_announcement(course_id):
    content = request.form.get("content", "").strip()
    if not content:
        flash("Announcement content cannot be empty.", "warning")
        return redirect(url_for("course_stream", course_id=course_id))

    file = request.files.get("attachment")
    att_name = None
    att_path = None
    att_size = None

    if file and file.filename:
        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stored_name = f"ann_{course_id}_{timestamp}_{filename}"
        dest_path = ATTACHMENTS_DIR / stored_name
        file.save(dest_path)
        att_name = filename
        att_path = str(dest_path)
        att_size = dest_path.stat().st_size

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    conn.execute("""
        INSERT INTO announcements (course_id, user_id, content, attachment_name, attachment_path, attachment_size, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (course_id, session["user_id"], content, att_name, att_path, att_size, now_str))
    conn.commit()
    conn.close()

    flash("Announcement published to course stream.", "success")
    return redirect(url_for("course_stream", course_id=course_id))


@app.route("/announcements/<int:announcement_id>/pin", methods=["POST"])
@teacher_required
def toggle_pin_announcement(announcement_id):
    conn = get_db()
    row = conn.execute("SELECT course_id, is_pinned FROM announcements WHERE id = ?", (announcement_id,)).fetchone()
    if row:
        new_pinned = 0 if row["is_pinned"] == 1 else 1
        conn.execute("UPDATE announcements SET is_pinned = ? WHERE id = ?", (new_pinned, announcement_id))
        conn.commit()
        course_id = row["course_id"]
        conn.close()
        flash("Announcement pin status updated.", "info")
        return redirect(url_for("course_stream", course_id=course_id))
    conn.close()
    return redirect(url_for("dashboard"))


@app.route("/announcements/<int:announcement_id>/delete", methods=["POST"])
@login_required
def delete_announcement(announcement_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM announcements WHERE id = ?", (announcement_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)

    if row["user_id"] != session["user_id"] and session.get("role") not in ("teacher", "admin"):
        conn.close()
        abort(403)

    if row["attachment_path"] and os.path.exists(row["attachment_path"]):
        try:
            os.remove(row["attachment_path"])
        except Exception:
            pass

    conn.execute("DELETE FROM comments WHERE context_type = 'announcement' AND context_id = ?", (announcement_id,))
    conn.execute("DELETE FROM announcements WHERE id = ?", (announcement_id,))
    conn.commit()
    course_id = row["course_id"]
    conn.close()

    flash("Announcement deleted.", "info")
    return redirect(url_for("course_stream", course_id=course_id))


@app.route("/announcements/<int:announcement_id>/comments", methods=["POST"])
@login_required
def add_announcement_comment(announcement_id):
    content = request.form.get("content", "").strip()
    if not content:
        flash("Comment cannot be empty.", "warning")
        return redirect(request.referrer or url_for("dashboard"))

    conn = get_db()
    ann = conn.execute("SELECT course_id FROM announcements WHERE id = ?", (announcement_id,)).fetchone()
    if not ann:
        conn.close()
        abort(404)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO comments (context_type, context_id, user_id, content, created_at)
        VALUES ('announcement', ?, ?, ?, ?)
    """, (announcement_id, session["user_id"], content, now_str))
    conn.commit()
    conn.close()

    return redirect(url_for("course_stream", course_id=ann["course_id"]))


# --- Tab 2: Classwork ---

@app.route("/courses/<int:course_id>/classwork")
@login_required
def course_classwork(course_id):
    course = get_course_or_404(course_id)
    conn = get_db()
    user_id = session["user_id"]

    topics = conn.execute("""
        SELECT * FROM topics WHERE course_id = ? ORDER BY display_order ASC, created_at ASC
    """, (course_id,)).fetchall()

    coursework_raw = conn.execute("""
        SELECT cw.*, t.name as topic_name,
               (SELECT COUNT(*) FROM coursework_attachments cwa WHERE cwa.coursework_id = cw.id) as attachment_count,
               s.status as my_status, s.grade as my_grade, s.is_late as my_is_late, s.receipt_token as my_receipt
        FROM coursework cw
        LEFT JOIN topics t ON cw.topic_id = t.id
        LEFT JOIN submissions s ON s.coursework_id = cw.id AND s.student_id = ?
        WHERE cw.course_id = ?
        ORDER BY cw.created_at DESC
    """, (user_id, course_id)).fetchall()

    topic_map = {}
    for t in topics:
        topic_map[t["id"]] = {"topic": t, "cw_items": []}
    topic_map[None] = {"topic": {"id": None, "name": "General Coursework"}, "cw_items": []}

    for item in coursework_raw:
        t_id = item["topic_id"]
        if t_id in topic_map:
            topic_map[t_id]["cw_items"].append(item)
        else:
            topic_map[None]["cw_items"].append(item)

    conn.close()
    return render_template(
        "course_classwork.html",
        course=course,
        topics=topics,
        topic_map=topic_map,
        active_tab="classwork"
    )


@app.route("/courses/<int:course_id>/topics", methods=["POST"])
@teacher_required
def create_topic(course_id):
    name = request.form.get("name", "").strip()
    if not name:
        flash("Topic name cannot be empty.", "warning")
        return redirect(url_for("course_classwork", course_id=course_id))

    conn = get_db()
    max_order = conn.execute("SELECT MAX(display_order) as mo FROM topics WHERE course_id = ?", (course_id,)).fetchone()["mo"] or 0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn.execute("""
        INSERT INTO topics (course_id, name, display_order, created_at)
        VALUES (?, ?, ?, ?)
    """, (course_id, name, max_order + 1, now_str))
    conn.commit()
    conn.close()

    flash(f"Topic '{name}' created successfully.", "success")
    return redirect(url_for("course_classwork", course_id=course_id))


@app.route("/courses/<int:course_id>/topics/<int:topic_id>/delete", methods=["POST"])
@teacher_required
def delete_topic(course_id, topic_id):
    conn = get_db()
    conn.execute("UPDATE coursework SET topic_id = NULL WHERE course_id = ? AND topic_id = ?", (course_id, topic_id))
    conn.execute("DELETE FROM topics WHERE id = ? AND course_id = ?", (topic_id, course_id))
    conn.commit()
    conn.close()
    flash("Topic removed. Items under this topic have been moved to General Coursework.", "info")
    return redirect(url_for("course_classwork", course_id=course_id))


@app.route("/courses/<int:course_id>/coursework/create", methods=["POST"])
@teacher_required
def create_coursework(course_id):
    cw_type = request.form.get("type", "assignment")
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    topic_id_raw = request.form.get("topic_id", "")
    topic_id = int(topic_id_raw) if topic_id_raw and topic_id_raw.isdigit() else None
    points = int(request.form.get("points", 100)) if request.form.get("points") else 100
    due_date = request.form.get("due_date", "").strip() or None
    start_time = request.form.get("start_time", "").strip() or None
    end_time = request.form.get("end_time", "").strip() or None
    allowed_types = request.form.get("allowed_types", "all").strip().lower()
    allow_multiple = 1 if request.form.get("allow_multiple") else 0
    allow_late = 1 if request.form.get("allow_late") else 0
    is_exam_mode = 1 if request.form.get("is_exam_mode") else 0
    labs = request.form.get("labs", "Lab 1, Lab 2, Lab 3, CC-101").strip()

    if cw_type == "exam" or is_exam_mode == 1:
        allowed_types = "zip"

    if not title:
        flash("Title is required for coursework.", "danger")
        return redirect(url_for("course_classwork", course_id=course_id))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO coursework (
            course_id, topic_id, type, title, description, points, due_date,
            start_time, end_time, allowed_types, allow_multiple, allow_late,
            is_exam_mode, labs, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        course_id, topic_id, cw_type, title, description, points, due_date,
        start_time, end_time, allowed_types, allow_multiple, allow_late,
        is_exam_mode, labs, session["user_id"], now_str
    ))
    coursework_id = c.lastrowid

    if is_exam_mode == 1:
        c.execute("UPDATE courses SET active_exam_id = ? WHERE id = ?", (coursework_id, course_id))

    uploaded_files = request.files.getlist("attachments")
    for f in uploaded_files:
        if f and f.filename:
            fname = secure_filename(f.filename)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            stored = f"att_{course_id}_{coursework_id}_{timestamp}_{fname}"
            dest = ATTACHMENTS_DIR / stored
            f.save(dest)
            c.execute("""
                INSERT INTO coursework_attachments (coursework_id, original_filename, stored_filename, file_path, file_size, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (coursework_id, fname, stored, str(dest), dest.stat().st_size, now_str))

    conn.commit()
    conn.close()

    flash(f"Coursework '{title}' published successfully.", "success")
    return redirect(url_for("course_classwork", course_id=course_id))


@app.route("/courses/<int:course_id>/coursework/<int:coursework_id>/toggle-exam-mode", methods=["POST"])
@teacher_required
def toggle_exam_mode(course_id, coursework_id):
    conn = get_db()
    row = conn.execute("SELECT is_exam_mode, title FROM coursework WHERE id = ? AND course_id = ?", (coursework_id, course_id)).fetchone()
    if not row:
        conn.close()
        abort(404)

    new_state = 0 if row["is_exam_mode"] == 1 else 1
    conn.execute("UPDATE coursework SET is_exam_mode = ?, allowed_types = 'zip' WHERE id = ?", (new_state, coursework_id))

    if new_state == 1:
        conn.execute("UPDATE courses SET active_exam_id = ? WHERE id = ?", (coursework_id, course_id))
        flash(f"🔒 STRICT EXAM LOCKDOWN ENABLED for '{row['title']}'. Students are now locked into the single exam submission link and can submit ONLY .zip files.", "warning")
    else:
        conn.execute("UPDATE courses SET active_exam_id = NULL WHERE id = ?", (course_id,))
        flash(f"Exam Mode deactivated for '{row['title']}'. Regular classroom access restored.", "info")

    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("course_classwork", course_id=course_id))


@app.route("/courses/<int:course_id>/coursework/<int:coursework_id>/delete", methods=["POST"])
@teacher_required
def delete_coursework(course_id, coursework_id):
    conn = get_db()
    conn.execute("UPDATE courses SET active_exam_id = NULL WHERE id = ? AND active_exam_id = ?", (course_id, coursework_id))
    conn.execute("DELETE FROM coursework WHERE id = ? AND course_id = ?", (coursework_id, course_id))
    conn.commit()
    conn.close()
    flash("Coursework item deleted.", "info")
    return redirect(url_for("course_classwork", course_id=course_id))


# --- Coursework Detail & Submissions ---

@app.route("/courses/<int:course_id>/coursework/<int:coursework_id>")
@login_required
def coursework_detail(course_id, coursework_id):
    course = get_course_or_404(course_id)
    conn = get_db()
    user_id = session["user_id"]
    role = session.get("role")

    cw = conn.execute("""
        SELECT cw.*, t.name as topic_name, u.display_name as creator_name
        FROM coursework cw
        LEFT JOIN topics t ON cw.topic_id = t.id
        JOIN users u ON cw.created_by = u.id
        WHERE cw.id = ? AND cw.course_id = ?
    """, (coursework_id, course_id)).fetchone()

    if not cw:
        conn.close()
        abort(404, "Coursework not found")

    if role == "student" and cw["is_exam_mode"] == 1:
        conn.close()
        return redirect(url_for("exam_view", course_id=course_id, coursework_id=coursework_id))

    attachments = conn.execute("""
        SELECT * FROM coursework_attachments WHERE coursework_id = ? ORDER BY uploaded_at ASC
    """, (coursework_id,)).fetchall()

    my_submission = None
    all_submissions = []
    stats = {"turned_in": 0, "graded": 0, "assigned": 0}

    if role == "student":
        my_submission = conn.execute("""
            SELECT * FROM submissions WHERE coursework_id = ? AND student_id = ?
        """, (coursework_id, user_id)).fetchone()

        private_comments = conn.execute("""
            SELECT cm.*, u.display_name, u.role
            FROM comments cm
            JOIN users u ON cm.user_id = u.id
            WHERE cm.context_type = 'coursework_private' AND cm.context_id = ?
            ORDER BY cm.created_at ASC
        """, (my_submission["id"] if my_submission else 0,)).fetchall()

        locker_files = conn.execute("""
            SELECT * FROM student_locker_files WHERE user_id = ? ORDER BY uploaded_at DESC
        """, (user_id,)).fetchall()

    else:
        all_submissions = conn.execute("""
            SELECT u.id as student_id, u.display_name, u.roll_number, u.email,
                   s.id as submission_id, s.status, s.original_filename, s.file_size,
                   s.submitted_at, s.is_late, s.late_minutes, s.grade, s.feedback,
                   s.sha256, s.receipt_token, s.lab_name
            FROM course_enrollments ce
            JOIN users u ON ce.user_id = u.id
            LEFT JOIN submissions s ON s.coursework_id = ? AND s.student_id = u.id
            WHERE ce.course_id = ? AND ce.role = 'student'
            ORDER BY u.roll_number ASC
        """, (coursework_id, course_id)).fetchall()

        on_time = 0
        late = 0
        for s in all_submissions:
            if s["submission_id"]:
                if s["status"] == "graded":
                    stats["graded"] += 1
                else:
                    stats["turned_in"] += 1
                if s["is_late"]:
                    late += 1
                else:
                    on_time += 1
            else:
                stats["assigned"] += 1

        stats["total_submissions"] = stats["turned_in"] + stats["graded"]
        stats["on_time"] = on_time
        stats["late"] = late
        stats["total_enrolled"] = len(all_submissions)

        private_comments = []
        locker_files = []

    conn.close()
    return render_template(
        "coursework_detail.html",
        course=course,
        cw=cw,
        attachments=attachments,
        my_submission=my_submission,
        all_submissions=all_submissions,
        private_comments=private_comments,
        locker_files=locker_files,
        stats=stats,
        active_tab="classwork"
    )


# --- Live Exam Submission Telemetry API ---

@app.route("/api/courses/<int:course_id>/coursework/<int:coursework_id>/live-submissions")
@login_required
def api_live_submissions(course_id, coursework_id):
    conn = get_db()
    user_id = session["user_id"]
    role = session.get("role")

    is_teacher = role in ("teacher", "admin")
    if not is_teacher:
        enr = conn.execute(
            "SELECT role FROM course_enrollments WHERE course_id = ? AND user_id = ? AND role = 'ta'",
            (course_id, user_id)
        ).fetchone()
        if enr:
            is_teacher = True

    if not is_teacher:
        conn.close()
        return jsonify({"success": False, "error": "Access denied: Instructor role required"}), 403

    cw = conn.execute(
        "SELECT * FROM coursework WHERE id = ? AND course_id = ?",
        (coursework_id, course_id)
    ).fetchone()
    if not cw:
        conn.close()
        return jsonify({"success": False, "error": "Coursework not found"}), 404

    subs = conn.execute("""
        SELECT u.id as student_id, u.display_name, u.roll_number, u.email,
               s.id as submission_id, s.status, s.original_filename, s.file_size,
               s.submitted_at, s.is_late, s.late_minutes, s.grade, s.feedback,
               s.sha256, s.receipt_token, s.lab_name
        FROM course_enrollments ce
        JOIN users u ON ce.user_id = u.id
        LEFT JOIN submissions s ON s.coursework_id = ? AND s.student_id = u.id
        WHERE ce.course_id = ? AND ce.role = 'student'
        ORDER BY u.roll_number ASC
    """, (coursework_id, course_id)).fetchall()
    conn.close()

    total_enrolled = len(subs)
    turned_in = 0
    graded = 0
    assigned = 0
    on_time = 0
    late = 0

    items = []
    for s in subs:
        sub_id = s["submission_id"]
        if sub_id:
            if s["status"] == "graded":
                graded += 1
            else:
                turned_in += 1
            if s["is_late"]:
                late += 1
            else:
                on_time += 1
        else:
            assigned += 1

        items.append({
            "student_id": s["student_id"],
            "roll_number": s["roll_number"] or "N/A",
            "display_name": s["display_name"],
            "email": s["email"],
            "submission_id": sub_id,
            "status": s["status"] if sub_id else "assigned",
            "submitted_at": s["submitted_at"],
            "is_late": bool(s["is_late"]) if sub_id else False,
            "late_minutes": s["late_minutes"] if sub_id else 0,
            "original_filename": s["original_filename"] if sub_id else None,
            "file_size": s["file_size"] if sub_id else 0,
            "receipt_token": s["receipt_token"] if sub_id else None,
            "grade": s["grade"] if sub_id else None,
            "feedback": s["feedback"] if sub_id else None,
            "is_pdf": (s["original_filename"].lower().endswith(".pdf")) if (sub_id and s["original_filename"]) else False
        })

    total_subs = turned_in + graded

    # Timer calculation
    now = datetime.now()
    end_dt = parse_iso_datetime(cw["end_time"]) if cw["end_time"] else None
    time_remaining_sec = max(0, int((end_dt - now).total_seconds())) if end_dt else None

    return jsonify({
        "success": True,
        "is_exam_mode": bool(cw["is_exam_mode"]),
        "total_enrolled": total_enrolled,
        "total_submissions": total_subs,
        "on_time_count": on_time,
        "late_count": late,
        "pending_count": assigned,
        "turned_in_count": turned_in,
        "graded_count": graded,
        "time_remaining_sec": time_remaining_sec,
        "submissions": items
    })


# --- Assignment Submission ---

@app.route("/courses/<int:course_id>/coursework/<int:coursework_id>/submit", methods=["POST"])
@login_required
def submit_coursework(course_id, coursework_id):
    conn = get_db()
    cw = conn.execute("SELECT * FROM coursework WHERE id = ? AND course_id = ?", (coursework_id, course_id)).fetchone()
    if not cw:
        conn.close()
        abort(404)

    user = get_current_user()
    user_id = user["id"]
    roll_number = user["roll_number"] or user["username"].upper()

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    is_late = 0
    late_minutes = 0

    if cw["due_date"]:
        due_dt = parse_iso_datetime(cw["due_date"])
        if due_dt and now > due_dt:
            if cw["allow_late"] == 0:
                conn.close()
                flash("Submission deadline has passed and late submissions are not accepted.", "danger")
                return redirect(url_for("coursework_detail", course_id=course_id, coursework_id=coursework_id))
            is_late = 1
            late_minutes = int((now - due_dt).total_seconds() / 60)

    existing = conn.execute("SELECT * FROM submissions WHERE coursework_id = ? AND student_id = ?", (coursework_id, user_id)).fetchone()
    if existing and cw["allow_multiple"] == 0:
        conn.close()
        flash("Multiple submissions are not permitted for this coursework.", "warning")
        return redirect(url_for("coursework_detail", course_id=course_id, coursework_id=coursework_id))

    version = (existing["version"] + 1) if existing else 1
    coursework_dir = SUBMISSIONS_DIR / str(course_id) / str(coursework_id)
    coursework_dir.mkdir(parents=True, exist_ok=True)

    source_type = request.form.get("source_type", "local")
    lab_name = request.form.get("lab_name", "Lab 1").strip()

    target_filepath = None
    orig_filename = ""

    if source_type == "locker":
        locker_file_id = request.form.get("locker_file_id")
        locker_row = conn.execute("SELECT * FROM student_locker_files WHERE id = ? AND user_id = ?", (locker_file_id, user_id)).fetchone()
        if not locker_row:
            conn.close()
            flash("Selected file not found in your locker.", "danger")
            return redirect(url_for("coursework_detail", course_id=course_id, coursework_id=coursework_id))

        orig_filename = locker_row["original_filename"]
        clean_ext = Path(orig_filename).suffix.lower()

        if cw["allowed_types"] == "zip" and clean_ext != ".zip":
            conn.close()
            flash("This coursework strictly accepts only .zip archives.", "danger")
            return redirect(url_for("coursework_detail", course_id=course_id, coursework_id=coursework_id))

        stored_filename = f"{roll_number}_v{version}_{secure_filename(orig_filename)}"
        target_filepath = coursework_dir / stored_filename
        shutil.copy2(locker_row["file_path"], target_filepath)

    else:
        file = request.files.get("submission_file")
        if not file or not file.filename:
            conn.close()
            flash("Please choose a file to submit.", "warning")
            return redirect(url_for("coursework_detail", course_id=course_id, coursework_id=coursework_id))

        orig_filename = file.filename
        clean_ext = Path(orig_filename).suffix.lower()

        if cw["allowed_types"] == "zip" and clean_ext != ".zip":
            conn.close()
            flash("This coursework strictly accepts only .zip archives.", "danger")
            return redirect(url_for("coursework_detail", course_id=course_id, coursework_id=coursework_id))

        stored_filename = f"{roll_number}_v{version}_{secure_filename(orig_filename)}"
        target_filepath = coursework_dir / stored_filename
        file.save(target_filepath)

    file_size = target_filepath.stat().st_size
    sha256_hash = calc_sha256(target_filepath)
    receipt_token = secrets.token_hex(16)
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)

    if existing:
        conn.execute("""
            UPDATE submissions SET
                original_filename = ?, stored_filename = ?, file_path = ?, file_size = ?,
                sha256 = ?, ip_address = ?, submitted_at = ?, version = ?,
                is_late = ?, late_minutes = ?, lab_name = ?, status = 'turned_in', receipt_token = ?
            WHERE id = ?
        """, (
            orig_filename, stored_filename, str(target_filepath), file_size,
            sha256_hash, client_ip, now_str, version,
            is_late, late_minutes, lab_name, receipt_token, existing["id"]
        ))
    else:
        conn.execute("""
            INSERT INTO submissions (
                coursework_id, student_id, roll_number, student_name, lab_name,
                status, original_filename, stored_filename, file_path, file_size,
                sha256, ip_address, submitted_at, version, is_late, late_minutes, receipt_token
            ) VALUES (?, ?, ?, ?, ?, 'turned_in', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            coursework_id, user_id, roll_number, user["display_name"], lab_name,
            orig_filename, stored_filename, str(target_filepath), file_size,
            sha256_hash, client_ip, now_str, version, is_late, late_minutes, receipt_token
        ))

    conn.commit()
    conn.close()

    flash("Work submitted successfully! Digital receipt generated with SHA-256 checksum.", "success")
    return redirect(url_for("coursework_detail", course_id=course_id, coursework_id=coursework_id))


@app.route("/courses/<int:course_id>/coursework/<int:coursework_id>/unsubmit", methods=["POST"])
@login_required
def unsubmit_coursework(course_id, coursework_id):
    conn = get_db()
    sub = conn.execute("SELECT * FROM submissions WHERE coursework_id = ? AND student_id = ?", (coursework_id, session["user_id"])).fetchone()
    if sub:
        if sub["status"] == "graded":
            conn.close()
            flash("Cannot unsubmit work that has already been evaluated and graded.", "danger")
            return redirect(url_for("coursework_detail", course_id=course_id, coursework_id=coursework_id))

        conn.execute("UPDATE submissions SET status = 'assigned' WHERE id = ?", (sub["id"],))
        conn.commit()
        flash("Submission unsubmitted. You may make changes and turn it in again.", "info")
    conn.close()
    return redirect(url_for("coursework_detail", course_id=course_id, coursework_id=coursework_id))


# --- Strict Exam Mode Lockdown Interface & Submission ---

@app.route("/courses/<int:course_id>/exam/<int:coursework_id>")
@login_required
def exam_view(course_id, coursework_id):
    course = get_course_or_404(course_id)
    conn = get_db()
    cw = conn.execute("SELECT * FROM coursework WHERE id = ? AND course_id = ?", (coursework_id, course_id)).fetchone()
    if not cw:
        conn.close()
        abort(404, "Exam not found")

    user = get_current_user()
    my_sub = conn.execute("SELECT * FROM submissions WHERE coursework_id = ? AND student_id = ?", (coursework_id, user["id"])).fetchone()

    raw_labs = cw["labs"] or "Lab 1, Lab 2, Lab 3"
    labs = [l.strip() for l in raw_labs.split(",") if l.strip()]

    now = datetime.now()
    st = parse_iso_datetime(cw["start_time"])
    et = parse_iso_datetime(cw["end_time"])

    is_started = True if not st or now >= st else False
    is_ended = True if et and now > et else False

    conn.close()
    return render_template(
        "exam_view.html",
        course=course,
        exam=cw,
        my_sub=my_sub,
        labs=labs,
        is_started=is_started,
        is_ended=is_ended,
        start_iso=cw["start_time"],
        end_iso=cw["end_time"]
    )


@app.route("/courses/<int:course_id>/exam/<int:coursework_id>/submit", methods=["POST"])
@login_required
def exam_submit(course_id, coursework_id):
    conn = get_db()
    cw = conn.execute("SELECT * FROM coursework WHERE id = ? AND course_id = ?", (coursework_id, course_id)).fetchone()
    if not cw:
        conn.close()
        abort(404)

    user = get_current_user()
    user_id = user["id"]
    roll_number = user["roll_number"] or user["username"].upper()

    file = request.files.get("exam_file")
    if not file or not file.filename:
        conn.close()
        flash("No file was uploaded. Please choose your exam ZIP file.", "danger")
        return redirect(url_for("exam_view", course_id=course_id, coursework_id=coursework_id))

    filename = file.filename
    clean_ext = Path(filename).suffix.lower()

    # Strict ZIP validation requirement
    if clean_ext != ".zip":
        conn.close()
        flash("STRICT REJECTION: Only .zip files are allowed for exam submissions.", "danger")
        return redirect(url_for("exam_view", course_id=course_id, coursework_id=coursework_id))

    header = file.read(4)
    file.seek(0)
    if header != b"PK" and header != b"PK" and header != b"PK":
        conn.close()
        flash("Invalid archive format: The file uploaded is not a valid ZIP archive.", "danger")
        return redirect(url_for("exam_view", course_id=course_id, coursework_id=coursework_id))

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    is_late = 0
    late_minutes = 0

    existing = conn.execute("SELECT * FROM submissions WHERE coursework_id = ? AND student_id = ?", (coursework_id, user_id)).fetchone()
    if existing:
        # If student already submitted once and exam end time has passed, strictly lock submission
        if cw["end_time"]:
            et = parse_iso_datetime(cw["end_time"])
            if et and now > et:
                conn.close()
                flash("The exam deadline has passed. Modifying or re-submitting after the exam has ended is strictly locked.", "danger")
                return redirect(url_for("exam_view", course_id=course_id, coursework_id=coursework_id))
        if cw["allow_multiple"] == 0:
            conn.close()
            flash("Single submission policy: You have already submitted your exam.", "warning")
            return redirect(url_for("exam_view", course_id=course_id, coursework_id=coursework_id))

    if cw["end_time"]:
        et = parse_iso_datetime(cw["end_time"])
        if et and now > et:
            if cw["allow_late"] == 0:
                conn.close()
                flash("Exam submission cutoff has expired. Submissions are closed.", "danger")
                return redirect(url_for("exam_view", course_id=course_id, coursework_id=coursework_id))
            is_late = 1
            late_minutes = int((now - et).total_seconds() / 60)

    version = (existing["version"] + 1) if existing else 1
    lab_name = request.form.get("lab_name", "Lab 1").strip()

    exam_dir = SUBMISSIONS_DIR / str(course_id) / str(coursework_id)
    exam_dir.mkdir(parents=True, exist_ok=True)

    stored_filename = f"{roll_number}.zip"
    dest_path = exam_dir / stored_filename
    file.save(dest_path)

    file_size = dest_path.stat().st_size
    sha256_hash = calc_sha256(dest_path)
    receipt_token = secrets.token_hex(16)
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)

    if existing:
        conn.execute("""
            UPDATE submissions SET
                original_filename = ?, stored_filename = ?, file_path = ?, file_size = ?,
                sha256 = ?, ip_address = ?, submitted_at = ?, version = ?,
                is_late = ?, late_minutes = ?, lab_name = ?, status = 'turned_in', receipt_token = ?
            WHERE id = ?
        """, (
            filename, stored_filename, str(dest_path), file_size,
            sha256_hash, client_ip, now_str, version,
            is_late, late_minutes, lab_name, receipt_token, existing["id"]
        ))
    else:
        conn.execute("""
            INSERT INTO submissions (
                coursework_id, student_id, roll_number, student_name, lab_name,
                status, original_filename, stored_filename, file_path, file_size,
                sha256, ip_address, submitted_at, version, is_late, late_minutes, receipt_token
            ) VALUES (?, ?, ?, ?, ?, 'turned_in', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            coursework_id, user_id, roll_number, user["display_name"], lab_name,
            filename, stored_filename, str(dest_path), file_size,
            sha256_hash, client_ip, now_str, version, is_late, late_minutes, receipt_token
        ))

    conn.commit()
    conn.close()

    flash(f"Exam successfully submitted! Verification Receipt: {receipt_token[:8]}... (SHA-256 verified)", "success")
    return redirect(url_for("exam_receipt", receipt_token=receipt_token))


@app.route("/receipt/<receipt_token>")
@login_required
def exam_receipt(receipt_token):
    conn = get_db()
    sub = conn.execute("""
        SELECT s.*, cw.title as exam_title, cw.type as cw_type, c.code as course_code, c.title as course_title
        FROM submissions s
        JOIN coursework cw ON s.coursework_id = cw.id
        JOIN courses c ON cw.course_id = c.id
        WHERE s.receipt_token = ?
    """, (receipt_token,)).fetchone()
    conn.close()

    if not sub:
        abort(404, "Receipt not found")

    return render_template("receipt_view.html", sub=sub)


# --- Tab 3: People ---

@app.route("/courses/<int:course_id>/people")
@login_required
def course_people(course_id):
    course = get_course_or_404(course_id)
    conn = get_db()
    curr_user = get_current_user()

    teachers = conn.execute("""
        SELECT u.id, u.display_name, u.email, u.roll_number, u.role as system_role, ce.role as enrollment_role
        FROM course_enrollments ce
        JOIN users u ON ce.user_id = u.id
        WHERE ce.course_id = ? AND ce.role IN ('teacher', 'ta', 'co-teacher')
        ORDER BY (CASE WHEN u.id = ? THEN 0 ELSE 1 END), u.display_name ASC
    """, (course_id, course["teacher_id"])).fetchall()

    students = conn.execute("""
        SELECT u.id, u.display_name, u.roll_number, u.email, ce.enrolled_at, ce.role as enrollment_role,
               (SELECT COUNT(*) FROM submissions s
                JOIN coursework cw ON s.coursework_id = cw.id
                WHERE cw.course_id = ? AND s.student_id = u.id) as submissions_count
        FROM course_enrollments ce
        JOIN users u ON ce.user_id = u.id
        WHERE ce.course_id = ? AND ce.role = 'student'
        ORDER BY u.roll_number ASC, u.display_name ASC
    """, (course_id, course_id)).fetchall()

    available_users = conn.execute("""
        SELECT u.id, u.display_name, u.roll_number, u.email, u.role,
               (SELECT role FROM course_enrollments ce WHERE ce.course_id = ? AND ce.user_id = u.id) as course_role
        FROM users u
        WHERE u.id != ?
        ORDER BY u.role DESC, u.roll_number ASC, u.display_name ASC
    """, (course_id, course["teacher_id"])).fetchall()

    is_teacher_or_admin = (
        curr_user["role"] in ("teacher", "admin") or
        course["teacher_id"] == curr_user["id"] or
        any(t["id"] == curr_user["id"] for t in teachers)
    )

    conn.close()
    return render_template(
        "course_people.html",
        course=course,
        teachers=teachers,
        students=students,
        available_users=available_users,
        is_teacher_or_admin=is_teacher_or_admin,
        active_tab="people"
    )


@app.route("/courses/<int:course_id>/people/add-coteacher", methods=["POST"])
@teacher_required
def add_co_teacher(course_id):
    course = get_course_or_404(course_id)
    target_id = request.form.get("user_id")
    identifier = request.form.get("identifier", "").strip()
    assigned_role = request.form.get("role", "ta").strip().lower()
    if assigned_role not in ("student", "ta", "teacher"):
        assigned_role = "ta"

    conn = get_db()
    user = None
    if target_id and target_id.isdigit():
        user = conn.execute("SELECT * FROM users WHERE id = ?", (int(target_id),)).fetchone()
    elif identifier:
        user = conn.execute("""
            SELECT * FROM users
            WHERE LOWER(username) = ? OR LOWER(roll_number) = ? OR (email != '' AND LOWER(email) = ?)
        """, (identifier.lower(), identifier.lower(), identifier.lower())).fetchone()

    if not user:
        conn.close()
        flash("User not found. Please verify the roll number, username, or email.", "danger")
        return redirect(url_for("course_people", course_id=course_id))

    # If assigning teacher role, elevate user system role to teacher
    if assigned_role == "teacher":
        conn.execute("UPDATE users SET role = 'teacher' WHERE id = ?", (user["id"],))
        enroll_role = "ta"
        role_label = "Faculty / Teacher"
    elif assigned_role == "student":
        enroll_role = "student"
        role_label = "Student"
    else:
        enroll_role = "ta"
        role_label = "Co-Teacher"

    existing = conn.execute("SELECT * FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course_id, user["id"])).fetchone()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if existing:
        conn.execute("UPDATE course_enrollments SET role = ? WHERE course_id = ? AND user_id = ?", (enroll_role, course_id, user["id"]))
    else:
        conn.execute("INSERT INTO course_enrollments (course_id, user_id, role, enrolled_at) VALUES (?, ?, ?, ?)", (course_id, user["id"], enroll_role, now_str))

    conn.commit()
    conn.close()
    flash(f"'{user['display_name']}' is now configured as {role_label} for {course['code']}.", "success")
    return redirect(url_for("course_people", course_id=course_id))


@app.route("/courses/<int:course_id>/people/<int:target_user_id>/role", methods=["POST"])
@teacher_required
def change_course_person_role(course_id, target_user_id):
    course = get_course_or_404(course_id)
    new_role = request.form.get("role", "student").strip().lower()
    if new_role not in ("student", "ta", "teacher"):
        flash("Invalid role selected.", "danger")
        return redirect(url_for("course_people", course_id=course_id))

    if target_user_id == course["teacher_id"] and new_role == "student":
        flash("The primary course instructor cannot be demoted to student.", "warning")
        return redirect(url_for("course_people", course_id=course_id))

    conn = get_db()
    target_user = conn.execute("SELECT * FROM users WHERE id = ?", (target_user_id,)).fetchone()
    if not target_user:
        conn.close()
        abort(404)

    if new_role == "teacher":
        conn.execute("UPDATE users SET role = 'teacher' WHERE id = ?", (target_user_id,))
        conn.execute("UPDATE course_enrollments SET role = 'ta' WHERE course_id = ? AND user_id = ?", (course_id, target_user_id))
        label = "Teacher / Faculty"
    elif new_role == "ta":
        conn.execute("UPDATE course_enrollments SET role = 'ta' WHERE course_id = ? AND user_id = ?", (course_id, target_user_id))
        label = "Co-Teacher"
    else:  # student
        conn.execute("UPDATE course_enrollments SET role = 'student' WHERE course_id = ? AND user_id = ?", (course_id, target_user_id))
        # If demoted to student, also demote system role if they are not lead teacher of any course and not admin
        other_lead = conn.execute("SELECT COUNT(*) FROM courses WHERE teacher_id = ?", (target_user_id,)).fetchone()[0]
        if other_lead == 0 and target_user["role"] != "admin":
            conn.execute("UPDATE users SET role = 'student' WHERE id = ?", (target_user_id,))
        label = "Student"

    conn.commit()
    conn.close()

    flash(f"Updated {target_user['display_name']}'s role in {course['code']} to {label}.", "success")
    return redirect(url_for("course_people", course_id=course_id))


@app.route("/courses/<int:course_id>/people/remove/<int:target_user_id>", methods=["POST"])
@teacher_required
def remove_student(course_id, target_user_id):
    course = get_course_or_404(course_id)
    if target_user_id == course["teacher_id"]:
        flash("Cannot remove the primary course instructor.", "danger")
        return redirect(url_for("course_people", course_id=course_id))

    conn = get_db()
    conn.execute("DELETE FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course_id, target_user_id))
    conn.commit()
    conn.close()
    flash("Person removed from course roster.", "info")
    return redirect(url_for("course_people", course_id=course_id))


# --- Tab 4: Grades & Canvas-Inspired Weighted Assessment Engine ---

def ensure_course_grading_categories(course_id, conn=None):
    """
    Ensures that default Canvas-style grading categories exist for a course.
    Default weighting scheme totaling 100%:
      - End-Semester Exam: 20%
      - Mid-Semester Exam: 20%
      - Lab Exams: 10%
      - Assignments & Quizzes: 45%
      - Attendance: 5% (is_attendance=1)
    """
    close_at_end = False
    if conn is None:
        conn = get_db()
        close_at_end = True

    cats = conn.execute("""
        SELECT * FROM course_grading_categories
        WHERE course_id = ?
        ORDER BY is_attendance ASC, id ASC
    """, (course_id,)).fetchall()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not cats:
        default_cats = [
            ("Assignments & Quizzes", 45.0, 0),
            ("Lab Exams", 10.0, 0),
            ("Mid-Semester Exam", 20.0, 0),
            ("End-Semester Exam", 20.0, 0),
            ("Attendance", 5.0, 1),
        ]
        for name, weight, is_att in default_cats:
            conn.execute("""
                INSERT INTO course_grading_categories (course_id, name, weight, is_attendance, drop_lowest, created_at)
                VALUES (?, ?, ?, ?, 0, ?)
            """, (course_id, name, weight, is_att, now_str))
        conn.commit()
        cats = conn.execute("""
            SELECT * FROM course_grading_categories
            WHERE course_id = ?
            ORDER BY is_attendance ASC, id ASC
        """, (course_id,)).fetchall()

    # Automatically map unassigned coursework to the best matching category
    unassigned = conn.execute("""
        SELECT id, title, type FROM coursework
        WHERE course_id = ? AND category_id IS NULL AND type != 'material'
    """, (course_id,)).fetchall()

    if unassigned:
        cat_map = {c["name"].lower(): c["id"] for c in cats}
        assign_cat = next((c["id"] for c in cats if "assign" in c["name"].lower() or "quiz" in c["name"].lower()), None)
        lab_cat = next((c["id"] for c in cats if "lab" in c["name"].lower()), None)
        mid_cat = next((c["id"] for c in cats if "mid" in c["name"].lower()), None)
        end_cat = next((c["id"] for c in cats if "end" in c["name"].lower()), None)
        first_regular = next((c["id"] for c in cats if not c["is_attendance"]), cats[0]["id"] if cats else None)

        for cw in unassigned:
            title_l = cw["title"].lower()
            target_id = first_regular
            if "mid" in title_l and mid_cat:
                target_id = mid_cat
            elif "end" in title_l and end_cat:
                target_id = end_cat
            elif ("lab" in title_l or cw["type"] == "exam") and lab_cat:
                target_id = lab_cat
            elif assign_cat:
                target_id = assign_cat

            if target_id:
                conn.execute("UPDATE coursework SET category_id = ? WHERE id = ?", (target_id, cw["id"]))
        conn.commit()

    if close_at_end:
        conn.close()
    return cats


def score_to_letter_grade(score):
    if score is None:
        return "N/A"
    if score >= 90.0:
        return "A"
    elif score >= 85.0:
        return "A-"
    elif score >= 80.0:
        return "B+"
    elif score >= 75.0:
        return "B"
    elif score >= 70.0:
        return "B-"
    elif score >= 65.0:
        return "C+"
    elif score >= 60.0:
        return "C"
    elif score >= 50.0:
        return "D"
    else:
        return "F"


def parse_spreadsheet_file(file_storage):
    """
    Robustly parses uploaded spreadsheets:
      - .csv, .tsv, .txt
      - Excel .xlsx (via openpyxl if installed, or native zipfile + ElementTree fallback)
    Returns: list of list of string values.
    """
    filename = (file_storage.filename or "").lower()
    raw_bytes = file_storage.read()

    # 1. Attempt XLSX
    if filename.endswith(".xlsx") or raw_bytes[:4] == b"PK\x03\x04":
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True)
            ws = wb.active
            rows = []
            for row in ws.iter_rows(values_only=True):
                if any(v is not None and str(v).strip() != "" for v in row):
                    rows.append([str(v).strip() if v is not None else "" for v in row])
            if rows:
                return rows
        except Exception:
            pass

        # Native zipfile + XML fallback for .xlsx (no external libraries needed)
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
            shared_strings = []
            if "xl/sharedStrings.xml" in zf.namelist():
                tree = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
                for si in tree.findall(f"{ns}si"):
                    parts = [t.text for t in si.iter(f"{ns}t") if t.text]
                    shared_strings.append("".join(parts))

            sheet_name = next((n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")), None)
            if sheet_name:
                tree = ET.fromstring(zf.read(sheet_name))
                ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
                rows = []
                for row_el in tree.iter(f"{ns}row"):
                    row_cells = []
                    for c_el in row_el.iter(f"{ns}c"):
                        t_attr = c_el.get("t")
                        v_el = c_el.find(f"{ns}v")
                        val = ""
                        if v_el is not None and v_el.text:
                            raw = v_el.text
                            if t_attr == "s" and raw.isdigit() and int(raw) < len(shared_strings):
                                val = shared_strings[int(raw)]
                            elif t_attr == "b":
                                val = "1" if raw == "1" else "0"
                            else:
                                val = raw
                        elif t_attr == "inlineStr":
                            t_el = c_el.find(f"{ns}is/{ns}t")
                            if t_el is not None and t_el.text:
                                val = t_el.text
                        row_cells.append(val.strip())
                    if any(row_cells):
                        rows.append(row_cells)
                if rows:
                    return rows
        except Exception:
            pass

    # 2. Text / CSV / TSV
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            text = raw_bytes.decode(enc)
            first_line = text.split("\n")[0] if "\n" in text else text
            delimiter = "\t" if ("\t" in first_line and "," not in first_line) else (";" if (";" in first_line and "," not in first_line) else ",")
            reader = csv.reader(io.StringIO(text), delimiter=delimiter)
            rows = []
            for r in reader:
                if any(c.strip() for c in r):
                    rows.append([c.strip() for c in r])
            if rows:
                return rows
        except UnicodeDecodeError:
            continue

    return []


def calculate_course_grades(course_id, student_id=None, conn=None):
    """
    Computes Canvas-style weighted grading out of 100 for all enrolled students
    or a specific student in a course.
    """
    close_at_end = False
    if conn is None:
        conn = get_db()
        close_at_end = True

    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    categories = ensure_course_grading_categories(course_id, conn)

    coursework_list = conn.execute("""
        SELECT cw.*, cgc.name as category_name
        FROM coursework cw
        LEFT JOIN course_grading_categories cgc ON cw.category_id = cgc.id
        WHERE cw.course_id = ? AND cw.type != 'material'
        ORDER BY cw.created_at ASC
    """, (course_id,)).fetchall()

    if student_id:
        students = conn.execute("""
            SELECT u.id, u.display_name, u.roll_number, u.email
            FROM course_enrollments ce
            JOIN users u ON ce.user_id = u.id
            WHERE ce.course_id = ? AND ce.role = 'student' AND u.id = ?
        """, (course_id, student_id)).fetchall()
    else:
        students = conn.execute("""
            SELECT u.id, u.display_name, u.roll_number, u.email
            FROM course_enrollments ce
            JOIN users u ON ce.user_id = u.id
            WHERE ce.course_id = ? AND ce.role = 'student'
            ORDER BY u.roll_number ASC
        """, (course_id,)).fetchall()

    # Submissions
    sub_query = """
        SELECT s.* FROM submissions s
        JOIN coursework cw ON s.coursework_id = cw.id
        WHERE cw.course_id = ?
    """
    if student_id:
        sub_query += " AND s.student_id = ?"
        submissions_raw = conn.execute(sub_query, (course_id, student_id)).fetchall()
    else:
        submissions_raw = conn.execute(sub_query, (course_id,)).fetchall()

    sub_map = {(r["student_id"], r["coursework_id"]): r for r in submissions_raw}

    # Attendance stats for course
    logged_dates = conn.execute("""
        SELECT COUNT(DISTINCT attendance_date) FROM attendance_logs WHERE course_id = ?
    """, (course_id,)).fetchone()[0] or 0
    created_sessions = conn.execute("""
        SELECT COUNT(*) FROM attendance_sessions WHERE course_id = ?
    """, (course_id,)).fetchone()[0] or 0
    total_attendance_sessions = max(logged_dates, created_sessions)

    # Calculate for each student
    students_summary = []
    total_scheme_weight = sum(float(c["weight"]) for c in categories)
    if total_scheme_weight <= 0:
        total_scheme_weight = 100.0

    for s in students:
        s_id = s["id"]
        # Student attendance
        present_count = conn.execute("""
            SELECT COUNT(DISTINCT attendance_date) FROM attendance_logs
            WHERE course_id = ? AND student_id = ? AND status = 'PRESENT'
        """, (course_id, s_id)).fetchone()[0] or 0

        if total_attendance_sessions > 0:
            att_pct = round(min(100.0, (present_count / total_attendance_sessions) * 100.0), 1)
        else:
            att_pct = 100.0

        cat_scores = {}
        cw_scores = {}
        weighted_points_earned = 0.0
        active_weights_sum = 0.0

        for cat in categories:
            cat_id = cat["id"]
            weight = float(cat["weight"])
            is_att = bool(cat["is_attendance"])

            if is_att:
                cat_earned_pct = att_pct
                cat_weighted_pts = round(cat_earned_pct * (weight / 100.0), 2)
                cat_scores[cat_id] = {
                    "id": cat_id,
                    "name": cat["name"],
                    "weight": weight,
                    "earned_points": present_count,
                    "max_points": total_attendance_sessions,
                    "percentage": cat_earned_pct,
                    "weighted_points": cat_weighted_pts,
                    "is_attendance": True
                }
                weighted_points_earned += cat_weighted_pts
                active_weights_sum += weight
            else:
                cws = [cw for cw in coursework_list if cw["category_id"] == cat_id]
                cat_earned = 0.0
                cat_max = 0.0
                has_graded = False

                for cw in cws:
                    cw_id = cw["id"]
                    sub = sub_map.get((s_id, cw_id))
                    grade = sub["grade"] if (sub and sub["grade"] is not None) else None
                    if grade is not None:
                        cat_earned += float(grade)
                        cat_max += float(cw["points"] or 100)
                        has_graded = True
                    cw_scores[cw_id] = {
                        "submission": sub,
                        "grade": grade,
                        "max_points": cw["points"] or 100,
                        "percentage": round((float(grade) / (cw["points"] or 100)) * 100.0, 1) if grade is not None else None,
                        "status": sub["status"] if sub else "missing"
                    }

                if cat_max > 0:
                    cat_pct = round((cat_earned / cat_max) * 100.0, 1)
                    cat_weighted_pts = round(cat_pct * (weight / 100.0), 2)
                    cat_scores[cat_id] = {
                        "id": cat_id,
                        "name": cat["name"],
                        "weight": weight,
                        "earned_points": round(cat_earned, 2),
                        "max_points": round(cat_max, 2),
                        "percentage": cat_pct,
                        "weighted_points": cat_weighted_pts,
                        "is_attendance": False
                    }
                    weighted_points_earned += cat_weighted_pts
                    active_weights_sum += weight
                else:
                    cat_scores[cat_id] = {
                        "id": cat_id,
                        "name": cat["name"],
                        "weight": weight,
                        "earned_points": 0.0,
                        "max_points": 0.0,
                        "percentage": None,
                        "weighted_points": 0.0,
                        "is_attendance": False
                    }

        running_grade_100 = round((weighted_points_earned / active_weights_sum) * 100.0, 2) if active_weights_sum > 0 else 0.0
        final_grade_100 = round(weighted_points_earned, 2)

        # Custom formula evaluation if specified
        if course and course["grading_formula"]:
            try:
                formula_str = course["grading_formula"].strip()
                var_dict = {}
                for cat in categories:
                    v_name = re.sub(r'[^a-zA-Z0-9]', '', cat["name"])
                    if cat["is_attendance"]:
                        var_dict["Attendance"] = att_pct
                    if v_name:
                        var_dict[v_name] = cat_scores[cat["id"]]["percentage"] or 0.0
                safe_dict = {"__builtins__": {}}
                safe_dict.update(var_dict)
                formula_val = eval(formula_str, safe_dict)
                final_grade_100 = round(float(formula_val), 2)
            except Exception:
                pass

        letter_grade = score_to_letter_grade(final_grade_100)

        students_summary.append({
            "id": s["id"],
            "display_name": s["display_name"],
            "roll_number": s["roll_number"],
            "email": s["email"],
            "attendance": {
                "present_count": present_count,
                "total_sessions": total_attendance_sessions,
                "percentage": att_pct,
                "weighted_points": cat_scores.get(next((c["id"] for c in categories if c["is_attendance"]), None), {}).get("weighted_points", 0.0)
            },
            "cat_scores": cat_scores,
            "cw_scores": cw_scores,
            "final_grade": final_grade_100,
            "running_grade": running_grade_100,
            "letter_grade": letter_grade
        })

    if close_at_end:
        conn.close()

    return {
        "categories": categories,
        "coursework_list": coursework_list,
        "students": students_summary,
        "sub_map": sub_map,
        "total_attendance_sessions": total_attendance_sessions,
        "total_scheme_weight": total_scheme_weight,
        "course": course
    }


@app.route("/courses/<int:course_id>/grades")
@login_required
def course_grades(course_id):
    course = get_course_or_404(course_id)
    user = get_current_user()
    conn = get_db()

    # Check authorization
    is_teacher_or_admin = (user["role"] in ("teacher", "admin"))
    if not is_teacher_or_admin:
        co_teacher = conn.execute("""
            SELECT 1 FROM course_enrollments
            WHERE course_id = ? AND user_id = ? AND role IN ('teacher', 'ta', 'co-teacher')
        """, (course_id, user["id"])).fetchone()
        if co_teacher:
            is_teacher_or_admin = True

    if not is_teacher_or_admin:
        enr = conn.execute("SELECT 1 FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course_id, user["id"])).fetchone()
        if not enr:
            conn.close()
            flash("You must be enrolled in this course to view grades.", "warning")
            return redirect(url_for("dashboard"))

    conn.close()

    if not is_teacher_or_admin:
        # Student View: Canvas-style personal scorecard
        grade_data = calculate_course_grades(course_id, student_id=user["id"])
        student_record = grade_data["students"][0] if grade_data["students"] else None
        return render_template(
            "course_grades.html",
            course=course,
            is_student=True,
            student=student_record,
            categories=grade_data["categories"],
            coursework_list=grade_data["coursework_list"],
            total_attendance_sessions=grade_data["total_attendance_sessions"],
            active_tab="grades",
            is_teacher_or_admin=False
        )

    # Teacher / Admin View: Canvas-style Gradebook Matrix
    grade_data = calculate_course_grades(course_id)
    return render_template(
        "course_grades.html",
        course=course,
        is_student=False,
        students=grade_data["students"],
        categories=grade_data["categories"],
        coursework_list=grade_data["coursework_list"],
        sub_map=grade_data["sub_map"],
        total_attendance_sessions=grade_data["total_attendance_sessions"],
        total_scheme_weight=grade_data["total_scheme_weight"],
        active_tab="grades",
        is_teacher_or_admin=True
    )


@app.route("/courses/<int:course_id>/grades/categories", methods=["POST"])
@teacher_required
def save_grading_categories(course_id):
    course = get_course_or_404(course_id)
    conn = get_db()

    cat_ids = request.form.getlist("cat_id")
    weights = request.form.getlist("weight")
    names = request.form.getlist("name")

    for cid, w, n in zip(cat_ids, weights, names):
        if cid and cid.isdigit():
            try:
                w_val = max(0.0, float(w))
            except ValueError:
                w_val = 0.0
            conn.execute("""
                UPDATE course_grading_categories
                SET name = ?, weight = ?
                WHERE id = ? AND course_id = ?
            """, (n.strip(), w_val, int(cid), course_id))

    new_name = request.form.get("new_category_name", "").strip()
    new_weight = request.form.get("new_category_weight", "").strip()
    new_is_att = 1 if request.form.get("new_is_attendance") == "1" else 0

    if new_name:
        try:
            nw_val = max(0.0, float(new_weight)) if new_weight else 10.0
        except ValueError:
            nw_val = 10.0
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            INSERT INTO course_grading_categories (course_id, name, weight, is_attendance, drop_lowest, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
        """, (course_id, new_name, nw_val, new_is_att, now_str))

    conn.commit()
    conn.close()
    flash("Course grading scheme and category weights saved successfully.", "success")
    return redirect(url_for("course_grades", course_id=course_id))


@app.route("/courses/<int:course_id>/grades/categories/delete/<int:category_id>", methods=["POST"])
@teacher_required
def delete_grading_category(course_id, category_id):
    course = get_course_or_404(course_id)
    conn = get_db()
    conn.execute("UPDATE coursework SET category_id = NULL WHERE category_id = ? AND course_id = ?", (category_id, course_id))
    conn.execute("DELETE FROM course_grading_categories WHERE id = ? AND course_id = ?", (category_id, course_id))
    conn.commit()
    conn.close()
    flash("Grading category deleted.", "info")
    return redirect(url_for("course_grades", course_id=course_id))


@app.route("/courses/<int:course_id>/coursework/<int:coursework_id>/category", methods=["POST"])
@teacher_required
def set_coursework_category(course_id, coursework_id):
    category_id = request.form.get("category_id")
    cat_val = int(category_id) if category_id and category_id.isdigit() else None
    conn = get_db()
    conn.execute("UPDATE coursework SET category_id = ? WHERE id = ? AND course_id = ?", (cat_val, coursework_id, course_id))
    conn.commit()
    conn.close()
    flash("Coursework category updated.", "success")
    return redirect(request.referrer or url_for("course_grades", course_id=course_id))


@app.route("/courses/<int:course_id>/grades/formula", methods=["POST"])
@teacher_required
def save_grading_formula(course_id):
    formula = request.form.get("grading_formula", "").strip()
    conn = get_db()
    conn.execute("UPDATE courses SET grading_formula = ? WHERE id = ?", (formula if formula else None, course_id))
    conn.commit()
    conn.close()
    if formula:
        flash("Custom grading calculation formula saved.", "success")
    else:
        flash("Reverted to standard weighted category calculation.", "info")
    return redirect(url_for("course_grades", course_id=course_id))


@app.route("/courses/<int:course_id>/grades/template")
@teacher_required
def export_grades_template(course_id):
    course = get_course_or_404(course_id)
    conn = get_db()

    cw_list = conn.execute("""
        SELECT id, title, points FROM coursework WHERE course_id = ? AND type != 'material' ORDER BY created_at ASC
    """, (course_id,)).fetchall()

    students = conn.execute("""
        SELECT u.roll_number, u.display_name, u.email
        FROM course_enrollments ce
        JOIN users u ON ce.user_id = u.id
        WHERE ce.course_id = ? AND ce.role = 'student'
        ORDER BY u.roll_number ASC
    """, (course_id,)).fetchall()
    conn.close()

    output = io.StringIO()
    output.write("\ufeff")  # UTF-8 BOM for Microsoft Excel compatibility
    header = ["Roll Number", "Student Name", "Email"]
    for cw in cw_list:
        header.append(f"{cw['title']} (Max {cw['points']})")
    output.write(",".join([f'"{h}"' for h in header]) + "\n")

    for s in students:
        row = [s["roll_number"] or "", s["display_name"], s["email"] or ""]
        for cw in cw_list:
            row.append("")  # Empty cell for instructor grade input
        output.write(",".join([f'"{c}"' for c in row]) + "\n")

    clean_code = re.sub(r'[^a-zA-Z0-9_-]', '_', course["code"])
    filename = f"{clean_code}_grades_template_{datetime.now().strftime('%Y%m%d')}.csv"

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/courses/<int:course_id>/grades/import", methods=["POST"])
@teacher_required
def import_grades(course_id):
    course = get_course_or_404(course_id)
    file = request.files.get("grades_file")
    if not file or not file.filename:
        flash("Please choose a CSV, TSV, or Excel (.xlsx) file to upload.", "danger")
        return redirect(url_for("course_grades", course_id=course_id))

    rows = parse_spreadsheet_file(file)
    if not rows or len(rows) < 2:
        flash("The uploaded file contains no data rows or could not be parsed.", "danger")
        return redirect(url_for("course_grades", course_id=course_id))

    conn = get_db()
    cw_list = conn.execute("""
        SELECT id, title, points FROM coursework WHERE course_id = ? AND type != 'material'
    """, (course_id,)).fetchall()

    if not cw_list:
        conn.close()
        flash("No active coursework exists in this course to grade.", "warning")
        return redirect(url_for("course_grades", course_id=course_id))

    headers = [h.strip() for h in rows[0]]
    id_col_idx = -1
    cw_col_map = {}  # col_idx -> coursework dict

    for idx, h in enumerate(headers):
        h_clean = h.lower()
        if any(key in h_clean for key in ("roll", "roll number", "student id", "id", "username", "email")):
            if id_col_idx == -1:
                id_col_idx = idx

        # Normalize header: strip "(max ...)", "(points ...)"
        norm_h = re.sub(r'\(max[^)]*\)', '', h, flags=re.IGNORECASE).strip().lower()
        for cw in cw_list:
            cw_norm = cw["title"].strip().lower()
            if cw_norm in norm_h or norm_h in cw_norm:
                cw_col_map[idx] = cw
                break

    if id_col_idx == -1:
        id_col_idx = 0

    if not cw_col_map:
        conn.close()
        flash("Could not match any column headers to coursework in this course. Please verify column titles or use the downloadable template.", "danger")
        return redirect(url_for("course_grades", course_id=course_id))

    updated_grades_count = 0
    matched_students = set()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for r_idx in range(1, len(rows)):
        row = rows[r_idx]
        if id_col_idx >= len(row):
            continue
        ident = row[id_col_idx].strip()
        if not ident:
            continue

        st = conn.execute("""
            SELECT u.id, u.roll_number, u.display_name
            FROM course_enrollments ce
            JOIN users u ON ce.user_id = u.id
            WHERE ce.course_id = ? AND ce.role = 'student'
              AND (UPPER(u.roll_number) = ? OR LOWER(u.username) = ? OR (u.email != '' AND LOWER(u.email) = ?))
        """, (course_id, ident.upper(), ident.lower(), ident.lower())).fetchone()

        if not st:
            continue

        matched_students.add(st["id"])

        for col_idx, cw in cw_col_map.items():
            if col_idx < len(row):
                val_raw = row[col_idx].strip()
                if not val_raw or val_raw.upper() in ("MISSING", "N/A", "-"):
                    continue
                val_clean = re.sub(r'[^0-9.]', '', val_raw)
                try:
                    score = float(val_clean)
                except ValueError:
                    continue

                existing = conn.execute("""
                    SELECT id FROM submissions WHERE coursework_id = ? AND student_id = ?
                """, (cw["id"], st["id"])).fetchone()

                if existing:
                    conn.execute("""
                        UPDATE submissions SET
                            grade = ?, status = 'graded', graded_by = ?, graded_at = ?
                        WHERE id = ?
                    """, (score, session.get("user_id"), now_str, existing["id"]))
                else:
                    receipt_tok = f"IMP-{secrets.token_hex(12).upper()}"
                    conn.execute("""
                        INSERT INTO submissions (
                            coursework_id, student_id, roll_number, student_name,
                            original_filename, stored_filename, file_path, file_size,
                            sha256, submitted_at, grade, status, graded_by, graded_at, receipt_token
                        ) VALUES (?, ?, ?, ?, ?, ?, '', 0, ?, ?, ?, 'graded', ?, ?, ?)
                    """, (
                        cw["id"], st["id"], st["roll_number"] or ident, st["display_name"],
                        "grade_import.csv", "grade_import.csv",
                        hashlib.sha256(f"IMPORT:{st['id']}:{cw['id']}:{score}".encode()).hexdigest(),
                        now_str, score, session.get("user_id"), now_str, receipt_tok
                    ))
                updated_grades_count += 1

    conn.commit()
    conn.close()

    flash(f"Bulk Grade Import Successful: Updated {updated_grades_count} grade(s) across {len(cw_col_map)} assessment(s) for {len(matched_students)} student(s).", "success")
    return redirect(url_for("course_grades", course_id=course_id))


@app.route("/courses/<int:course_id>/grades/quick-update", methods=["POST"])
@teacher_required
def quick_grade_update(course_id):
    student_id = request.form.get("student_id")
    coursework_id = request.form.get("coursework_id")
    grade_val = request.form.get("grade", "").strip()
    feedback = request.form.get("feedback", "").strip()

    if not student_id or not coursework_id:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "error": "Missing student or coursework ID"}), 400
        flash("Missing parameters.", "danger")
        return redirect(url_for("course_grades", course_id=course_id))

    try:
        grade = float(grade_val) if grade_val else None
    except ValueError:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "error": "Invalid grade number"}), 400
        flash("Invalid grade number.", "danger")
        return redirect(url_for("course_grades", course_id=course_id))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()

    existing = conn.execute("""
        SELECT id FROM submissions WHERE coursework_id = ? AND student_id = ?
    """, (coursework_id, student_id)).fetchone()

    if existing:
        conn.execute("""
            UPDATE submissions SET
                grade = ?, feedback = ?, graded_by = ?, graded_at = ?, status = 'graded'
            WHERE id = ?
        """, (grade, feedback, session.get("user_id"), now_str, existing["id"]))
    else:
        st = conn.execute("SELECT roll_number, display_name FROM users WHERE id = ?", (student_id,)).fetchone()
        receipt_tok = f"QUICK-{secrets.token_hex(12).upper()}"
        conn.execute("""
            INSERT INTO submissions (
                coursework_id, student_id, roll_number, student_name,
                original_filename, stored_filename, file_path, file_size,
                sha256, submitted_at, grade, feedback, status, graded_by, graded_at, receipt_token
            ) VALUES (?, ?, ?, ?, 'manual_grade.txt', 'manual_grade.txt', '', 0, ?, ?, ?, ?, 'graded', ?, ?, ?)
        """, (
            coursework_id, student_id, st["roll_number"] or "", st["display_name"],
            hashlib.sha256(f"MANUAL:{student_id}:{coursework_id}:{grade}".encode()).hexdigest(),
            now_str, grade, feedback, session.get("user_id"), now_str, receipt_tok
        ))

    conn.commit()
    conn.close()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
        return jsonify({"success": True, "grade": grade, "feedback": feedback})

    flash("Grade saved successfully.", "success")
    return redirect(url_for("course_grades", course_id=course_id))


@app.route("/courses/<int:course_id>/coursework/<int:coursework_id>/grade/<int:submission_id>", methods=["POST"])
@teacher_required
def grade_submission(course_id, coursework_id, submission_id):
    grade_val = request.form.get("grade", "").strip()
    feedback = request.form.get("feedback", "").strip()

    try:
        grade = float(grade_val) if grade_val else None
    except ValueError:
        flash("Invalid grade number.", "danger")
        return redirect(request.referrer or url_for("course_grades", course_id=course_id))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    conn.execute("""
        UPDATE submissions SET
            grade = ?, feedback = ?, graded_by = ?, graded_at = ?, status = 'graded'
        WHERE id = ? AND coursework_id = ?
    """, (grade, feedback, session["user_id"], now_str, submission_id, coursework_id))
    conn.commit()
    conn.close()

    flash("Grade and feedback saved.", "success")
    return redirect(request.referrer or url_for("course_grades", course_id=course_id))


@app.route("/courses/<int:course_id>/grades/export-csv")
@teacher_required
def export_grades_csv(course_id):
    course = get_course_or_404(course_id)
    grade_data = calculate_course_grades(course_id)

    output = io.StringIO()
    output.write("\ufeff")  # UTF-8 BOM
    header = ["Roll Number", "Student Name", "Email"]

    for cw in grade_data["coursework_list"]:
        header.append(f"{cw['title']} (Max {cw['points']})")

    # Category subtotals
    for cat in grade_data["categories"]:
        header.append(f"{cat['name']} ({cat['weight']}%)")

    header.extend(["Final Weighted Score (100)", "Letter Grade"])
    output.write(",".join([f'"{h}"' for h in header]) + "\n")

    for s in grade_data["students"]:
        row = [s["roll_number"] or "", s["display_name"], s["email"] or ""]
        # Coursework scores
        for cw in grade_data["coursework_list"]:
            sc = s["cw_scores"].get(cw["id"])
            if sc and sc["grade"] is not None:
                row.append(str(sc["grade"]))
            elif sc and sc["status"] == "turned_in":
                row.append("Turned In")
            else:
                row.append("Missing")

        # Category scores
        for cat in grade_data["categories"]:
            cat_info = s["cat_scores"].get(cat["id"])
            if cat_info and cat_info["percentage"] is not None:
                row.append(f"{cat_info['percentage']}% ({cat_info['weighted_points']} pts)")
            else:
                row.append("-")

        row.append(f"{s['final_grade']} / 100")
        row.append(s["letter_grade"])
        output.write(",".join([f'"{c}"' for c in row]) + "\n")

    output.seek(0)
    clean_code = re.sub(r'[^a-zA-Z0-9_-]', '_', course["code"])
    filename = f"{clean_code}_gradebook_export_{datetime.now().strftime('%Y%m%d')}.csv"

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/courses/<int:course_id>/coursework/<int:coursework_id>/download-all-zip")
@teacher_required
def download_all_submissions_zip(course_id, coursework_id):
    conn = get_db()
    cw = conn.execute("SELECT * FROM coursework WHERE id = ? AND course_id = ?", (coursework_id, course_id)).fetchone()
    subs = conn.execute("""
        SELECT s.*, u.roll_number, u.display_name
        FROM submissions s
        JOIN users u ON s.student_id = u.id
        WHERE s.coursework_id = ?
    """, (coursework_id,)).fetchall()
    conn.close()

    if not cw:
        abort(404)

    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for sub in subs:
            fpath = Path(sub["file_path"])
            if fpath.exists():
                roll = sub["roll_number"] or f"ID_{sub['student_id']}"
                arcname = f"{roll}/{fpath.name}"
                zf.write(fpath, arcname=arcname)

    mem_zip.seek(0)
    clean_title = re.sub(r'[^a-zA-Z0-9_-]', '_', cw["title"])
    filename = f"{clean_title}_submissions_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"

    return send_file(
        mem_zip,
        mimetype="application/zip",
        as_attachment=True,
        download_name=filename
    )


# --- Student Private Space: My Locker / Cloud Drive ---

@app.route("/locker")
@login_required
def locker():
    user = get_current_user()
    user_id = user["id"]
    conn = get_db()

    files = conn.execute("""
        SELECT * FROM student_locker_files WHERE user_id = ? ORDER BY uploaded_at DESC
    """, (user_id,)).fetchall()

    usage_row = conn.execute("""
        SELECT COALESCE(SUM(file_size), 0) as total_used, COUNT(*) as file_count
        FROM student_locker_files WHERE user_id = ?
    """, (user_id,)).fetchone()
    conn.close()

    used = usage_row["total_used"] or 0
    quota = user["storage_quota_bytes"] or DEFAULT_LOCKER_QUOTA
    stats = {
        "used_bytes": used,
        "quota_bytes": quota,
        "used_formatted": format_file_size(used),
        "quota_formatted": format_file_size(quota),
        "percent": min(100, round((used / quota) * 100, 1)) if quota > 0 else 0,
        "file_count": usage_row["file_count"]
    }

    return render_template("locker.html", files=files, stats=stats)


@app.route("/locker/upload", methods=["POST"])
@login_required
def locker_upload():
    user = get_current_user()
    user_id = user["id"]

    uploaded_files = request.files.getlist("files")
    if not uploaded_files or all(not f.filename for f in uploaded_files):
        flash("Please select at least one file to upload.", "warning")
        return redirect(url_for("locker"))

    user_locker_dir = LOCKERS_DIR / str(user_id)
    user_locker_dir.mkdir(parents=True, exist_ok=True)

    conn = get_db()
    current_used = conn.execute("""
        SELECT COALESCE(SUM(file_size), 0) as total_used FROM student_locker_files WHERE user_id = ?
    """, (user_id,)).fetchone()["total_used"] or 0

    quota = user["storage_quota_bytes"] or DEFAULT_LOCKER_QUOTA
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    uploaded_count = 0
    for file in uploaded_files:
        if file and file.filename:
            orig_name = secure_filename(file.filename)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            stored_name = f"{timestamp}_{orig_name}"
            dest = user_locker_dir / stored_name
            file.save(dest)
            f_size = dest.stat().st_size

            if current_used + f_size > quota:
                if dest.exists():
                    dest.unlink()
                flash(f"Storage quota exceeded! Could not upload '{orig_name}'. Reclaim space and try again.", "danger")
                break

            current_used += f_size
            conn.execute("""
                INSERT INTO student_locker_files (user_id, original_filename, stored_filename, file_path, file_size, mime_type, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, orig_name, stored_name, str(dest), f_size, file.mimetype, now_str))
            uploaded_count += 1

    conn.commit()
    conn.close()

    if uploaded_count > 0:
        flash(f"{uploaded_count} file(s) saved to your Private Locker.", "success")
    return redirect(url_for("locker"))


@app.route("/locker/download/<int:file_id>")
@login_required
def locker_download(file_id):
    conn = get_db()
    f = conn.execute("SELECT * FROM student_locker_files WHERE id = ? AND user_id = ?", (file_id, session["user_id"])).fetchone()
    conn.close()

    if not f or not os.path.exists(f["file_path"]):
        abort(404, "File not found")

    return send_file(f["file_path"], as_attachment=True, download_name=f["original_filename"])


@app.route("/locker/preview/<int:file_id>")
@login_required
def locker_preview(file_id):
    conn = get_db()
    f = conn.execute("SELECT * FROM student_locker_files WHERE id = ? AND user_id = ?", (file_id, session["user_id"])).fetchone()
    conn.close()

    if not f or not os.path.exists(f["file_path"]):
        return jsonify({"error": "File not found"}), 404

    fpath = Path(f["file_path"])
    ext = fpath.suffix.lower()

    if ext == ".pdf":
        return jsonify({
            "type": "pdf",
            "filename": f["original_filename"],
            "size": format_file_size(f["file_size"]),
            "url": url_for("locker_view", file_id=file_id),
            "download_url": url_for("locker_download", file_id=file_id)
        })

    text_extensions = {".c", ".cpp", ".h", ".hpp", ".cu", ".cuh", ".py", ".sh", ".txt", ".md", ".json", ".sql", ".html", ".css", ".js"}
    if ext in text_extensions or f["file_size"] < 100 * 1024:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as content_file:
                content = content_file.read(50000)
            return jsonify({
                "type": "text",
                "filename": f["original_filename"],
                "size": format_file_size(f["file_size"]),
                "content": content
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({
        "type": "binary",
        "filename": f["original_filename"],
        "size": format_file_size(f["file_size"]),
        "message": "Binary preview not available for this file type. Please download to view."
    })


@app.route("/locker/view/<int:file_id>")
@login_required
def locker_view(file_id):
    conn = get_db()
    f = conn.execute("SELECT * FROM student_locker_files WHERE id = ? AND user_id = ?", (file_id, session["user_id"])).fetchone()
    conn.close()
    if not f or not os.path.exists(f["file_path"]):
        abort(404, "File not found")

    ext = Path(f["original_filename"]).suffix.lower()
    mimetype = "application/pdf" if ext == ".pdf" else None
    return send_file(f["file_path"], mimetype=mimetype, as_attachment=False, download_name=f["original_filename"])


@app.route("/locker/delete/<int:file_id>", methods=["POST"])
@login_required
def locker_delete(file_id):
    conn = get_db()
    f = conn.execute("SELECT * FROM student_locker_files WHERE id = ? AND user_id = ?", (file_id, session["user_id"])).fetchone()
    if f:
        try:
            if os.path.exists(f["file_path"]):
                os.remove(f["file_path"])
        except Exception:
            pass
        conn.execute("DELETE FROM student_locker_files WHERE id = ?", (file_id,))
        conn.commit()
        flash(f"'{f['original_filename']}' deleted from your locker.", "info")
    conn.close()
    return redirect(url_for("locker"))


@app.route("/locker/download-all")
@login_required
def locker_download_all():
    user = get_current_user()
    user_id = user["id"]
    conn = get_db()
    files = conn.execute("SELECT * FROM student_locker_files WHERE user_id = ?", (user_id,)).fetchall()
    conn.close()

    if not files:
        flash("Your private locker is empty.", "warning")
        return redirect(url_for("locker"))

    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            p = Path(f["file_path"])
            if p.exists():
                zf.write(p, arcname=f["original_filename"])

    mem_zip.seek(0)
    roll = user["roll_number"] or user["username"]
    filename = f"{roll}_Locker_Backup_{datetime.now().strftime('%Y%m%d')}.zip"
    return send_file(mem_zip, mimetype="application/zip", as_attachment=True, download_name=filename)


# --- File Downloads & In-App Viewers ---

@app.route("/view/attachment/<int:att_id>")
@app.route("/download/attachment/<int:att_id>")
@login_required
def download_attachment(att_id):
    conn = get_db()
    att = conn.execute("SELECT * FROM coursework_attachments WHERE id = ?", (att_id,)).fetchone()
    file_path = None
    original_filename = None

    if att and os.path.exists(att["file_path"]):
        file_path = att["file_path"]
        original_filename = att["original_filename"]
    else:
        ann = conn.execute("SELECT * FROM announcements WHERE id = ?", (att_id,)).fetchone()
        if ann and ann["attachment_path"] and os.path.exists(ann["attachment_path"]):
            file_path = ann["attachment_path"]
            original_filename = ann["attachment_name"]

    conn.close()
    if not file_path:
        abort(404, "Attachment not found")

    is_inline = request.path.startswith("/view/") or request.args.get("view") == "1" or request.args.get("inline") == "1"
    ext = Path(original_filename).suffix.lower()
    mimetype = "application/pdf" if ext == ".pdf" else None

    return send_file(
        file_path,
        mimetype=mimetype,
        as_attachment=not is_inline,
        download_name=original_filename
    )


@app.route("/view/submission/<int:sub_id>")
@app.route("/download/submission/<int:sub_id>")
@login_required
def download_submission(sub_id):
    conn = get_db()
    sub = conn.execute("SELECT * FROM submissions WHERE id = ?", (sub_id,)).fetchone()
    if not sub or not os.path.exists(sub["file_path"]):
        conn.close()
        abort(404, "Submission file not found")

    is_authorized = False
    if session.get("role") in ("teacher", "admin") or sub["student_id"] == session["user_id"]:
        is_authorized = True
    else:
        cw = conn.execute("SELECT course_id FROM coursework WHERE id = ?", (sub["coursework_id"],)).fetchone()
        if cw:
            enr = conn.execute("SELECT role FROM course_enrollments WHERE course_id = ? AND user_id = ? AND role = 'ta'", (cw["course_id"], session["user_id"])).fetchone()
            if enr:
                is_authorized = True

    conn.close()
    if not is_authorized:
        abort(403, "Unauthorized access to submission")

    is_inline = request.path.startswith("/view/") or request.args.get("view") == "1" or request.args.get("inline") == "1"
    ext = Path(sub["original_filename"]).suffix.lower()
    mimetype = "application/pdf" if ext == ".pdf" else None

    return send_file(
        sub["file_path"],
        mimetype=mimetype,
        as_attachment=not is_inline,
        download_name=sub["original_filename"]
    )


# --- Hoodle Brand Assets & Logo Downloads ---

@app.route("/brand")
def brand_assets():
    current_user = get_current_user()
    return render_template("brand_assets.html", current_user=current_user)


@app.route("/brand/download/<asset_name>")
def download_brand_asset(asset_name):
    allowed_assets = {
        "icon-png": ("hoodle_icon.png", "Hoodle_Icon_Transparent.png", "image/png"),
        "icon-svg": ("hoodle_icon.svg", "Hoodle_Icon_Transparent.svg", "image/svg+xml"),
        "logo-png": ("hoodle_icon.png", "Hoodle_Icon_Transparent.png", "image/png"),
        "logo-svg": ("hoodle_logo.svg", "Hoodle_Logo_Full.svg", "image/svg+xml"),
        "banner-png": ("hoodle_banner.png", "Hoodle_Brand_Banner.png", "image/png"),
        "app-icon-png": ("hoodle_icon.png", "Hoodle_Icon_Transparent.png", "image/png"),
        "mark-svg": ("hoodle_mark.svg", "Hoodle_Mark_Transparent.svg", "image/svg+xml"),
        "kit": ("hoodle_brand_kit.zip", "Hoodle_Brand_Kit.zip", "application/zip"),
    }
    if asset_name not in allowed_assets:
        abort(404, "Brand asset not found")

    file_rel, download_name, mimetype = allowed_assets[asset_name]
    file_path = os.path.join(app.root_path, "static", "images", file_rel)

    # Generate ZIP on the fly if missing
    if asset_name == "kit" and not os.path.exists(file_path):
        import zipfile
        img_dir = os.path.join(app.root_path, "static", "images")
        try:
            with zipfile.ZipFile(file_path, "w", zipfile.ZIP_DEFLATED) as z:
                for fn in ("hoodle_logo.png", "hoodle_logo.svg", "hoodle_banner.png", "hoodle_app_icon.png", "hoodle_mark.svg"):
                    p = os.path.join(img_dir, fn)
                    if os.path.exists(p):
                        z.write(p, fn)
                if os.path.exists(os.path.join(img_dir, "accl_logo.png")):
                    z.write(os.path.join(img_dir, "accl_logo.png"), "partner_logos/accl_logo.png")
                z.writestr("BRAND_GUIDELINES.txt", "HOODLE LMS BRAND ASSETS\nACCL Research Lab\nColors: #1F216C, #2F2483, #38BDF8, #F59E0B\n")
        except Exception as e:
            app.logger.warning("Failed to generate brand kit zip: %s", e)

    if not os.path.exists(file_path):
        abort(404, "Brand asset file missing")

    return send_file(
        file_path,
        mimetype=mimetype,
        as_attachment=True,
        download_name=download_name
    )


# --- Admin & Faculty User Management ---

@app.route("/admin/users")
@admin_required
def admin_users():
    conn = get_db()
    users = conn.execute("""
        SELECT u.*,
               (SELECT COUNT(*) FROM course_enrollments ce WHERE ce.user_id = u.id) as enrolled_courses_count,
               (SELECT COALESCE(SUM(file_size), 0) FROM student_locker_files slf WHERE slf.user_id = u.id) as locker_used_bytes
        FROM users u
        ORDER BY u.role DESC, u.created_at DESC
    """).fetchall()
    conn.close()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@teacher_required
def admin_change_role(user_id):
    curr_user = get_current_user()
    new_role = request.form.get("role", "student").strip().lower()
    target_redirect = request.referrer or (url_for("admin_users") if curr_user["role"] == "admin" else url_for("dashboard"))
    if new_role not in ("student", "teacher", "admin"):
        flash("Invalid role.", "danger")
        return redirect(target_redirect)

    if new_role == "admin" and curr_user["role"] != "admin":
        flash("Only an administrator can assign the Administrator role.", "danger")
        return redirect(target_redirect)

    conn = get_db()
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    conn.commit()
    conn.close()
    flash("User role updated successfully.", "success")
    return redirect(target_redirect)


@app.route("/teacher/students/<int:user_id>/role", methods=["POST"])
@teacher_required
def teacher_change_student_role(user_id):
    curr_user = get_current_user()
    new_role = request.form.get("role", "student").strip().lower()
    target_redirect = request.referrer or url_for("dashboard")
    if new_role not in ("student", "teacher"):
        flash("Teachers can only set Student or Teacher roles.", "danger")
        return redirect(target_redirect)

    conn = get_db()
    target_user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target_user:
        conn.close()
        abort(404)

    if target_user["role"] == "admin":
        conn.close()
        flash("Cannot change the role of an Administrator.", "danger")
        return redirect(target_redirect)

    conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    conn.commit()
    conn.close()
    flash(f"Updated {target_user['display_name']}'s role to {new_role.capitalize()}.", "success")
    return redirect(target_redirect)


@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def admin_reset_password(user_id):
    new_pwd = request.form.get("new_password", "").strip()
    if not new_pwd or len(new_pwd) < 6:
        flash("New password must be at least 6 characters.", "danger")
        return redirect(url_for("admin_users"))

    pwd_hash = hash_password(new_pwd)
    conn = get_db()
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pwd_hash, user_id))
    conn.commit()
    conn.close()
    flash("Password reset successfully.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    if user_id == session["user_id"]:
        flash("You cannot delete your own active administrator account.", "danger")
        return redirect(url_for("admin_users"))

    conn = get_db()
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    flash("User deleted.", "info")
    return redirect(url_for("admin_users"))



# --- Dynamic Anti-Proxy QR Attendance System ---

ATTENDANCE_ROTATION_SECONDS = 45  # QR code rotates dynamically every 45 seconds


def get_dynamic_attendance_token(course_id, session_type="Lecture", time_block=None):
    """
    Generates a cryptographic 8-character rotating token based on 45-second time blocks.
    Anti-Proxy Protection: Any photo/link shared expires in 45 seconds.
    """
    if time_block is None:
        time_block = int(time.time() // ATTENDANCE_ROTATION_SECONDS)
    raw = f"{SECRET_KEY}_ATTEND_{course_id}_{session_type.upper()}_{time_block}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest[:8].upper()


def validate_dynamic_attendance_token(course_id, session_type, scanned_token):
    """
    Validates token against current 45-second block and immediately preceding block
    (allowing 30s grace boundary for students scanning near transition).
    """
    if not scanned_token:
        return False
    current_block = int(time.time() // ATTENDANCE_ROTATION_SECONDS)
    valid_tokens = [
        get_dynamic_attendance_token(course_id, session_type, current_block),
        get_dynamic_attendance_token(course_id, session_type, current_block - 1)
    ]
    return scanned_token.strip().upper() in valid_tokens


def get_attendance_seconds_remaining():
    return int(ATTENDANCE_ROTATION_SECONDS - (time.time() % ATTENDANCE_ROTATION_SECONDS))


def generate_qr_svg(data_url):
    """Generate high-quality vector SVG QR code."""
    try:
        import qrcode
        import qrcode.image.svg
        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(data_url, image_factory=factory)
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue()
    except Exception:
        # Standalone vector SVG fallback
        escaped_url = data_url.replace("&", "&amp;")
        return f"""<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" viewBox="0 0 300 300">
            <rect width="300" height="300" fill="#ffffff" rx="12"/>
            <rect x="20" y="20" width="260" height="260" fill="#f8fafc" stroke="#e2e8f0" stroke-width="2" rx="8"/>
            <text x="150" y="110" font-family="sans-serif" font-size="28" text-anchor="middle" fill="#0f172a">📱</text>
            <text x="150" y="150" font-family="sans-serif" font-size="14" font-weight="bold" text-anchor="middle" fill="#0f172a">Scan with Phone Camera</text>
            <text x="150" y="180" font-family="monospace" font-size="11" text-anchor="middle" fill="#2563eb">{escaped_url[:35]}...</text>
        </svg>""".encode("utf-8")


@app.route("/api/attendance/qr/<int:course_id>")
@login_required
def attendance_qr_svg(course_id):
    session_type = request.args.get("type", "Lecture")
    token = request.args.get("token") or get_dynamic_attendance_token(course_id, session_type)
    
    # Construct student scan URL
    # Respect reverse-proxy prefix (e.g. /lms)
    base_url = request.host_url.rstrip("/")
    prefix = request.headers.get("X-Forwarded-Prefix", "")
    if prefix and not prefix.startswith("/"):
        prefix = "/" + prefix
    scan_url = f"{base_url}{prefix}/attend/{course_id}?token={token}&type={session_type}"
    
    svg_data = generate_qr_svg(scan_url)
    return Response(svg_data, mimetype="image/svg+xml")


@app.route("/api/attendance/token/<int:course_id>")
@teacher_required
def api_attendance_token(course_id):
    """API for projector screen to fetch rotating dynamic token and stats."""
    session_type = request.args.get("type", "Lecture")
    token = get_dynamic_attendance_token(course_id, session_type)
    seconds_remaining = get_attendance_seconds_remaining()
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    count_row = conn.execute("""
        SELECT COUNT(*) as count FROM attendance_logs
        WHERE course_id = ? AND session_type = ? AND attendance_date = ?
    """, (course_id, session_type, today_str)).fetchone()
    conn.close()
    
    return jsonify({
        "token": token,
        "seconds_remaining": seconds_remaining,
        "rotation_interval": ATTENDANCE_ROTATION_SECONDS,
        "session_type": session_type,
        "attendees_count": count_row["count"] if count_row else 0
    })


@app.route("/api/attendance/live-poll/<int:course_id>")
@teacher_required
def api_attendance_live_poll(course_id):
    """Poll live attendees for projector screen live ticker."""
    session_type = request.args.get("type", "Lecture")
    today_str = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    
    attendees = conn.execute("""
        SELECT roll_number, student_name, marked_at, method
        FROM attendance_logs
        WHERE course_id = ? AND session_type = ? AND attendance_date = ?
        ORDER BY id DESC LIMIT 12
    """, (course_id, session_type, today_str)).fetchall()
    
    total_count = conn.execute("""
        SELECT COUNT(*) as cnt FROM attendance_logs
        WHERE course_id = ? AND session_type = ? AND attendance_date = ?
    """, (course_id, session_type, today_str)).fetchone()["cnt"]
    conn.close()
    
    recent_list = []
    for r in attendees:
        marked_at = r["marked_at"] or ""
        time_part = marked_at.split(" ")[-1] if " " in marked_at else marked_at
        recent_list.append({
            "roll_number": r["roll_number"] or "STUDENT",
            "student_name": r["student_name"] or "Student",
            "name": r["student_name"] or "Student",
            "marked_at": marked_at,
            "marked_time": time_part,
            "time": time_part,
            "method": r["method"] or "QR_SCAN"
        })
    
    return jsonify({
        "total_count": total_count,
        "attendee_count": total_count,
        "recent": recent_list
    })


@app.route("/api/courses/<int:course_id>/attendance/student/<int:student_id>")
@teacher_required
def api_student_attendance_detail(course_id, student_id):
    """Returns detailed attendance history for an individual student in a course."""
    conn = get_db()
    student = conn.execute("SELECT id, roll_number, display_name, email FROM users WHERE id = ?", (student_id,)).fetchone()
    if not student:
        conn.close()
        return jsonify({"error": "Student not found"}), 404
        
    logs = conn.execute("""
        SELECT id, session_type, attendance_date, marked_at, method, ip_address, status
        FROM attendance_logs
        WHERE course_id = ? AND student_id = ?
        ORDER BY attendance_date DESC, marked_at DESC
    """, (course_id, student_id)).fetchall()
    
    sessions_row = conn.execute("""
        SELECT COUNT(DISTINCT attendance_date || '_' || session_type) as total_sessions
        FROM attendance_logs WHERE course_id = ?
    """, (course_id,)).fetchone()
    total_sessions = sessions_row["total_sessions"] or 0
    attended_count = len(logs)
    pct = round((attended_count / total_sessions * 100), 1) if total_sessions > 0 else 100.0
    
    conn.close()
    return jsonify({
        "student": {
            "id": student["id"],
            "roll_number": student["roll_number"] or "",
            "display_name": student["display_name"],
            "email": student["email"] or ""
        },
        "total_sessions": total_sessions,
        "attended_count": attended_count,
        "attendance_pct": pct,
        "status": "Satisfactory (>=75%)" if pct >= 75.0 else "Shortage (<75%)",
        "logs": [dict(l) for l in logs]
    })


# --- Teacher Projector Screen ---

@app.route("/courses/<int:course_id>/attendance/projector")
@teacher_required
def attendance_projector(course_id):
    """
    Live Projector Display:
    Full-screen dynamic rotating QR code display for the classroom projector.
    Features 45-second countdown ring, live scan counter, and anti-proxy rotation.
    """
    course = get_course_or_404(course_id)
    session_type = request.args.get("type", "Lecture")
    
    token = get_dynamic_attendance_token(course_id, session_type)
    seconds_remaining = get_attendance_seconds_remaining()
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    count_row = conn.execute("""
        SELECT COUNT(*) as count FROM attendance_logs
        WHERE course_id = ? AND session_type = ? AND attendance_date = ?
    """, (course_id, session_type, today_str)).fetchone()
    conn.close()
    
    attendees_count = count_row["count"] if count_row else 0
    
    return render_template(
        "attendance_projector.html",
        course=course,
        session_type=session_type,
        initial_token=token,
        seconds_remaining=seconds_remaining,
        rotation_interval=ATTENDANCE_ROTATION_SECONDS,
        attendees_count=attendees_count,
        today_str=today_str
    )


# --- Student QR Scan & Mobile Confirmation Flow ---

@app.route("/attend/<int:course_id>")
def attend_scan_landing(course_id):
    """
    Mobile landing page when student scans the projector QR code.
    Validates dynamic anti-proxy token and displays student verification screen.
    """
    if "user_id" not in session:
        flash("Please sign in with your student account to record attendance.", "info")
        return redirect(url_for("login", next=request.full_path))
    
    user = get_current_user()
    course = get_course_or_404(course_id)
    session_type = request.args.get("type", "Lecture").strip()
    scanned_token = request.args.get("token", "").strip().upper()
    
    # 1. Anti-Proxy Token Check
    is_valid_token = validate_dynamic_attendance_token(course_id, session_type, scanned_token)
    if not is_valid_token:
        return render_template(
            "attendance_confirm.html",
            course=course,
            error="EXPIRED_TOKEN",
            session_type=session_type,
            user=user
        )
    
    # 2. Enrollment check
    conn = get_db()
    enrollment = conn.execute("""
        SELECT * FROM course_enrollments WHERE course_id = ? AND user_id = ?
    """, (course_id, user["id"])).fetchone()
    
    if not enrollment and user["role"] == "student":
        conn.close()
        return render_template(
            "attendance_confirm.html",
            course=course,
            error="NOT_ENROLLED",
            session_type=session_type,
            user=user
        )
    
    # 3. Duplicate attendance check
    today_str = datetime.now().strftime("%Y-%m-%d")
    target_key = f"{user['id']}_{session_type.upper()}_{today_str}"
    
    existing = conn.execute("""
        SELECT * FROM attendance_logs WHERE attendance_key = ?
    """, (target_key,)).fetchone()
    conn.close()
    
    if existing:
        return render_template(
            "attendance_confirm.html",
            course=course,
            error="ALREADY_LOGGED",
            session_type=session_type,
            existing_log=existing,
            today_str=today_str,
            user=user
        )
    
    return render_template(
        "attendance_confirm.html",
        course=course,
        error=None,
        token=scanned_token,
        session_type=session_type,
        today_str=today_str,
        user=user
    )


@app.route("/attend/<int:course_id>/submit", methods=["POST"])
@login_required
def attend_submit(course_id):
    """
    Submits and atomically logs student attendance with anti-proxy validation.
    """
    user = get_current_user()
    course = get_course_or_404(course_id)
    session_type = request.form.get("session_type", "Lecture").strip()
    token = request.form.get("token", "").strip().upper()
    
    # Re-validate dynamic token
    if not validate_dynamic_attendance_token(course_id, session_type, token):
        flash("❌ QR Code Expired: The attendance token changed before your confirmation was sent. Please scan the current projector code.", "danger")
        return redirect(url_for("course_attendance", course_id=course_id))
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target_key = f"{user['id']}_{session_type.upper()}_{today_str}"
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    roll_number = user["roll_number"] or user["username"].upper()
    
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO attendance_logs (
                course_id, session_id, student_id, roll_number, student_name,
                section, session_type, attendance_date, status, method, ip_address, marked_at, attendance_key
            ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, 'PRESENT', 'QR_SCAN', ?, ?, ?)
        """, (
            course_id, user["id"], roll_number, user["display_name"],
            course["section"] or "Section A", session_type, today_str,
            client_ip, now_str, target_key
        ))
        conn.commit()
        conn.close()
        
        return render_template(
            "attendance_confirm.html",
            course=course,
            success=True,
            session_type=session_type,
            today_str=today_str,
            now_str=now_str,
            user=user
        )
    except sqlite3.IntegrityError:
        conn.close()
        flash(f"Attendance for today's {session_type} has already been logged.", "info")
        return redirect(url_for("course_attendance", course_id=course_id))


# --- Main Course Attendance Tab & Logs Dashboard ---

@app.route("/courses/<int:course_id>/attendance")
@login_required
def course_attendance(course_id):
    course = get_course_or_404(course_id)
    user = get_current_user()
    conn = get_db()
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    # Check whether user has instructor, co-teacher, TA, or admin privileges for this course
    is_course_teacher = False
    if user["role"] in ("teacher", "admin"):
        is_course_teacher = True
    elif course["teacher_id"] == user["id"]:
        is_course_teacher = True
    else:
        co_t = conn.execute("""
            SELECT 1 FROM course_enrollments
            WHERE course_id = ? AND user_id = ? AND role IN ('teacher', 'ta', 'co-teacher')
        """, (course_id, user["id"])).fetchone()
        if co_t:
            is_course_teacher = True

    if not is_course_teacher:
        # Student view: personal attendance summary & logs
        my_logs = conn.execute("""
            SELECT * FROM attendance_logs
            WHERE course_id = ? AND student_id = ?
            ORDER BY marked_at DESC
        """, (course_id, user["id"])).fetchall()
        
        # Total unique course sessions conducted
        sessions_row = conn.execute("""
            SELECT COUNT(DISTINCT attendance_date || '_' || session_type) as total_sessions
            FROM attendance_logs WHERE course_id = ?
        """, (course_id,)).fetchone()
        total_sessions = (sessions_row["total_sessions"] if sessions_row else 0) or 0
        attended_count = len(my_logs)
        pct = round((attended_count / total_sessions * 100), 1) if total_sessions > 0 else 100.0
        
        conn.close()
        return render_template(
            "course_attendance.html",
            course=course,
            is_course_teacher=False,
            my_logs=my_logs,
            total_sessions=total_sessions,
            attended_count=attended_count,
            attendance_pct=pct,
            today_str=today_str,
            active_tab="attendance"
        )
        
    else:
        # Teacher / Co-Teacher / Admin view: full class roster, statistics, logs, and manual marker
        enrolled_students = conn.execute("""
            SELECT u.id, u.roll_number, u.display_name, u.email,
                   (SELECT COUNT(*) FROM attendance_logs al WHERE al.course_id = ? AND al.student_id = u.id) as attended_count
            FROM course_enrollments ce
            JOIN users u ON ce.user_id = u.id
            WHERE ce.course_id = ? AND ce.role = 'student'
            ORDER BY u.roll_number ASC
        """, (course_id, course_id)).fetchall()
        
        sessions_row = conn.execute("""
            SELECT COUNT(DISTINCT attendance_date || '_' || session_type) as total_sessions
            FROM attendance_logs WHERE course_id = ?
        """, (course_id,)).fetchone()
        total_sessions = (sessions_row["total_sessions"] if sessions_row else 0) or 0
        
        # Recent logs
        recent_logs = conn.execute("""
            SELECT * FROM attendance_logs
            WHERE course_id = ?
            ORDER BY marked_at DESC LIMIT 50
        """, (course_id,)).fetchall()
        
        today_row = conn.execute("""
            SELECT COUNT(*) as count FROM attendance_logs
            WHERE course_id = ? AND attendance_date = ?
        """, (course_id, today_str)).fetchone()
        today_count = (today_row["count"] if today_row else 0) or 0
        
        conn.close()
        return render_template(
            "course_attendance.html",
            course=course,
            is_course_teacher=True,
            students=enrolled_students,
            total_sessions=total_sessions,
            today_count=today_count,
            recent_logs=recent_logs,
            today_str=today_str,
            attendance_pct=0.0,
            active_tab="attendance"
        )


@app.route("/courses/<int:course_id>/attendance/manual-bulk", methods=["POST"])
@teacher_required
def attendance_manual_bulk(course_id):
    """
    Teacher bulk manual attendance marker with date and session type selection.
    Handles dead phone batteries or technical issues.
    """
    course = get_course_or_404(course_id)
    raw_identifiers = request.form.get("manual_identifiers", "").strip()
    session_type = request.form.get("session_type", "Lecture").strip()
    custom_date = request.form.get("custom_date", "").strip()
    target_date = custom_date if custom_date else datetime.now().strftime("%Y-%m-%d")
    
    if not raw_identifiers:
        flash("Please enter at least one Roll Number or Email.", "warning")
        return redirect(url_for("course_attendance", course_id=course_id))
    
    identifiers = [re.sub(r'[^a-zA-Z0-9@._-]', '', x.strip().lower()) for x in re.split(r'[,;\s\n]+', raw_identifiers) if x.strip()]
    
    conn = get_db()
    # Fetch all enrolled students
    students = conn.execute("""
        SELECT u.id, u.roll_number, u.username, u.email, u.display_name
        FROM course_enrollments ce
        JOIN users u ON ce.user_id = u.id
        WHERE ce.course_id = ? AND ce.role = 'student'
    """, (course_id,)).fetchall()
    
    student_map = {}
    for s in students:
        if s["roll_number"]:
            student_map[s["roll_number"].lower().strip()] = s
        if s["username"]:
            student_map[s["username"].lower().strip()] = s
        if s["email"]:
            student_map[s["email"].lower().strip()] = s
    
    marked_count = 0
    duplicate_count = 0
    not_found = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    for ident in identifiers:
        if ident in student_map:
            st = student_map[ident]
            target_key = f"{st['id']}_{session_type.upper()}_{target_date}"
            
            # Check duplicate
            exists = conn.execute("SELECT id FROM attendance_logs WHERE attendance_key = ?", (target_key,)).fetchone()
            if exists:
                duplicate_count += 1
            else:
                conn.execute("""
                    INSERT INTO attendance_logs (
                        course_id, session_id, student_id, roll_number, student_name,
                        section, session_type, attendance_date, status, method, marked_at, attendance_key
                    ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, 'PRESENT (MANUAL)', 'MANUAL_ADMIN', ?, ?)
                """, (
                    course_id, st["id"], st["roll_number"] or st["username"].upper(),
                    st["display_name"], course["section"] or "Section A", session_type,
                    target_date, now_str, target_key
                ))
                marked_count += 1
        else:
            not_found.append(ident)
            
    conn.commit()
    conn.close()
    
    msg = f"Bulk attendance for {target_date} ({session_type}): {marked_count} marked successfully."
    if duplicate_count > 0:
        msg += f" {duplicate_count} already recorded."
    if not_found:
        msg += f" {len(not_found)} not found ({', '.join(not_found[:5])})."
    
    flash(msg, "success" if marked_count > 0 else "info")
    return redirect(url_for("course_attendance", course_id=course_id))


@app.route("/courses/<int:course_id>/attendance/export-csv")
@teacher_required
def attendance_export_csv(course_id):
    course = get_course_or_404(course_id)
    conn = get_db()
    
    sessions_row = conn.execute("""
        SELECT COUNT(DISTINCT attendance_date || '_' || session_type) as total_sessions
        FROM attendance_logs WHERE course_id = ?
    """, (course_id,)).fetchone()
    total_sessions = sessions_row["total_sessions"] or 0
    
    students = conn.execute("""
        SELECT u.roll_number, u.display_name, u.email,
               (SELECT COUNT(*) FROM attendance_logs al WHERE al.course_id = ? AND al.student_id = u.id) as attended_count
        FROM course_enrollments ce
        JOIN users u ON ce.user_id = u.id
        WHERE ce.course_id = ? AND ce.role = 'student'
        ORDER BY u.roll_number ASC
    """, (course_id, course_id)).fetchall()
    conn.close()
    
    output = io.StringIO()
    output.write('"Roll Number","Student Name","Email","Attended Sessions","Total Sessions","Attendance Percentage","Status"\n')
    
    for s in students:
        att = s["attended_count"]
        pct = round((att / total_sessions * 100), 1) if total_sessions > 0 else 100.0
        status = "Satisfactory (>=75%)" if pct >= 75.0 else "Shortage (<75%)"
        output.write(f'"{s["roll_number"] or ""}","{s["display_name"]}","{s["email"] or ""}",{att},{total_sessions},{pct}%,{status}\n')
        
    output.seek(0)
    filename = f"{re.sub(r'[^a-zA-Z0-9_-]', '_', course['code'])}_Attendance_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


with app.app_context():
    init_db()


if __name__ == "__main__":
    print(f"🚀 ACCLLMS Classroom Portal listening on http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False)
