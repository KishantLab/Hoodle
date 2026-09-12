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
import subprocess
import sqlite3
import hashlib
import zipfile
import io
import json
import csv
import xml.etree.ElementTree as ET
import secrets
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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

# Automatically load .env into os.environ if present
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    try:
        with open(_env_file, "r", encoding="utf-8") as _ef:
            for _line in _ef:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
    except Exception:
        pass

STORAGE_DIR = Path(os.environ.get("STORAGE_DIR", BASE_DIR / "storage"))
LOCKERS_DIR = STORAGE_DIR / "lockers"
SUBMISSIONS_DIR = STORAGE_DIR / "submissions"
ATTACHMENTS_DIR = STORAGE_DIR / "attachments"
STATIC_DIR = BASE_DIR / "static"
UPLOADS_DIR = STATIC_DIR / "uploads"
BACKUPS_DIR = Path(os.environ.get("BACKUPS_DIR", BASE_DIR / "backups"))
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "accl_lms.db"))

PORT = int(os.environ.get("PORT", 8095))
HOST = os.environ.get("HOST", "0.0.0.0")
MAX_CONTENT_LENGTH = 300 * 1024 * 1024  # 300 MB max upload limit
SECRET_KEY = os.environ.get("SECRET_KEY", "accl_lms_classroom_secret_2026_super_secure")
DEFAULT_LOCKER_QUOTA = 500 * 1024 * 1024  # 500 MB default private storage quota

# Create directories if they do not exist
for d in (STORAGE_DIR, LOCKERS_DIR, SUBMISSIONS_DIR, ATTACHMENTS_DIR, STATIC_DIR, UPLOADS_DIR, BACKUPS_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.config["STORAGE_DIR"] = STORAGE_DIR
app.config["LOCKERS_DIR"] = LOCKERS_DIR
app.config["SUBMISSIONS_DIR"] = SUBMISSIONS_DIR
app.config["ATTACHMENTS_DIR"] = ATTACHMENTS_DIR
app.config["BACKUPS_DIR"] = BACKUPS_DIR
app.config["DB_PATH"] = DB_PATH

# Strict Session Security against XSS and session hijacking
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_NAME"] = "hoodle_session"
app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 14  # 14 days

# Enable proper reverse-proxy handling (Nginx /lms/)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


@app.after_request
def set_security_headers(response):
    """Inject defensive HTTP security headers against clickjacking, sniffing, and XSS."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.route("/favicon.ico")
def favicon():
    """Official Hoodle URL favicon route."""
    return send_from_directory(
        os.path.join(app.root_path, "static", "images"),
        "hoodle_icon.png",
        mimetype="image/png"
    )


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

    # 14. Course Invitations Table (Bulk Roll Number & Email Invitations)
    c.execute("""
        CREATE TABLE IF NOT EXISTS course_invitations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            invited_by INTEGER NOT NULL,
            student_roll TEXT DEFAULT NULL,
            student_email TEXT DEFAULT NULL,
            student_id INTEGER DEFAULT NULL,
            token TEXT UNIQUE NOT NULL,
            status TEXT DEFAULT 'pending',
            role TEXT DEFAULT 'student',
            sent_at TEXT NOT NULL,
            responded_at TEXT DEFAULT NULL,
            FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE,
            FOREIGN KEY (invited_by) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_inv_course ON course_invitations (course_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_inv_roll ON course_invitations (student_roll)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_inv_email ON course_invitations (student_email)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_inv_student ON course_invitations (student_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_inv_token ON course_invitations (token)")

    # Migration for course_invitations table
    try:
        c.execute("PRAGMA table_info(course_invitations)")
        inv_cols = [row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in c.fetchall()]
        if "role" not in inv_cols:
            c.execute("ALTER TABLE course_invitations ADD COLUMN role TEXT DEFAULT 'student'")
    except Exception:
        pass

    # Migration for courses table
    try:
        c.execute("PRAGMA table_info(courses)")
        course_cols = [row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in c.fetchall()]
        if "grading_formula" not in course_cols:
            c.execute("ALTER TABLE courses ADD COLUMN grading_formula TEXT DEFAULT NULL")
        if "grade_calculation_mode" not in course_cols:
            c.execute("ALTER TABLE courses ADD COLUMN grade_calculation_mode TEXT DEFAULT 'weighted_categories'")
        if "attendance_threshold" not in course_cols:
            c.execute("ALTER TABLE courses ADD COLUMN attendance_threshold REAL DEFAULT 50.0")
    except Exception:
        pass

    # Migration for coursework table
    try:
        c.execute("PRAGMA table_info(coursework)")
        cw_cols = [row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in c.fetchall()]
        if "category_id" not in cw_cols:
            c.execute("ALTER TABLE coursework ADD COLUMN category_id INTEGER DEFAULT NULL")
    except Exception:
        pass

    # 13. Direct Messages Table (Student to Teacher/TA, Teacher/TA to Student, strictly no student-student)
    c.execute("""
        CREATE TABLE IF NOT EXISTS direct_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER,
            sender_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE SET NULL,
            FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (recipient_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_dm_users ON direct_messages (sender_id, recipient_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_dm_recipient ON direct_messages (recipient_id, is_read)")

    # 14. System Configuration & Dynamic Settings Table
    c.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

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

    conn.commit()
    conn.close()

# --- Gmail Notification & Email Services ---

def get_smtp_config():
    """
    Returns (gmail_user, gmail_pass, from_name) tuple.
    Checks SQLite system_settings table first, then environment variables,
    defaulting to the verified account hoodle.lms@gmail.com with configured App Password.
    """
    default_user = os.environ.get("GMAIL_SMTP_USER", "hoodle.lms@gmail.com").strip()
    default_pass = os.environ.get("GMAIL_APP_PASSWORD", "hvrnbggfrzmnvrsh").strip()
    default_from = os.environ.get("GMAIL_FROM_NAME", "Hoodle LMS").strip()

    try:
        conn = get_db()
        rows = conn.execute("SELECT key, value FROM system_settings WHERE key IN ('gmail_smtp_user', 'gmail_app_password', 'gmail_from_name')").fetchall()
        conn.close()
        settings = {r["key"]: r["value"] for r in rows if r["value"]}
        user = settings.get("gmail_smtp_user") or default_user
        password = settings.get("gmail_app_password") or default_pass
        from_name = settings.get("gmail_from_name") or default_from
        return user.strip(), password.strip().replace(" ", ""), from_name.strip()
    except Exception:
        return default_user, default_pass.replace(" ", ""), default_from


def get_portal_base_url():
    """
    Returns the fully qualified base URL of the Hoodle LMS portal (e.g. http://10.10.14.104/lms).
    Priority:
    1. SQLite system_settings ('portal_base_url')
    2. os.environ.get('PORTAL_BASE_URL')
    3. Active request context (script_root, X-Forwarded-Prefix, host)
    4. Fallback default: http://10.10.14.104/lms
    """
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM system_settings WHERE key = 'portal_base_url'").fetchone()
        conn.close()
        if row and row["value"] and row["value"].strip():
            return row["value"].strip().rstrip("/")
    except Exception:
        pass

    env_url = os.environ.get("PORTAL_BASE_URL", "").strip().rstrip("/")
    if env_url:
        return env_url

    try:
        from flask import has_request_context, request
        if has_request_context():
            base = request.url_root.rstrip("/")
            prefix = request.headers.get("X-Forwarded-Prefix", "").strip().rstrip("/")
            if prefix and not base.endswith(prefix):
                base = f"{base}{prefix}"
            if ("10.10.14.104" in base or "accl" in base) and not base.endswith(("/lms", "/hoodle")):
                base = f"{base}/lms"
            if base:
                return base
    except Exception:
        pass

    return "http://10.10.14.104/lms"


def resolve_portal_url(path_or_url):
    """
    Safely resolves any relative or absolute path into a full external URL pointing to Hoodle LMS.
    Prevents double prefixes like /lms/lms and ensures the correct base URL.
    """
    if not path_or_url:
        return ""
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url

    base_url = get_portal_base_url().rstrip("/")

    # If base_url ends with /lms or /hoodle and path_or_url already starts with that prefix, strip it from base_url
    for prefix in ("/lms", "/hoodle"):
        if base_url.endswith(prefix) and (path_or_url == prefix or path_or_url.startswith(f"{prefix}/") or path_or_url.startswith(f"{prefix}?")):
            base_url = base_url[:-len(prefix)].rstrip("/")
            break

    if not path_or_url.startswith("/"):
        path_or_url = "/" + path_or_url

    return f"{base_url}{path_or_url}"


def send_course_invitation_email(course, recipient_email, student_roll, teacher_name, token, role="student"):
    """
    Sends a course invitation email via Gmail SMTP in a background daemon thread.
    Gracefully logs and exits if Gmail credentials are not configured.
    Supports both Student and Teaching Assistant (TA) / Co-Teacher invitations.
    """
    gmail_user, gmail_pass, from_name = get_smtp_config()

    if not gmail_user or not gmail_pass or not recipient_email:
        return

    join_url = resolve_portal_url(f"/invitations/accept/{token}")
    course_code = course["code"]
    course_title = course["title"]
    course_section = course["section"] if "section" in course.keys() and course["section"] else "Section A"
    join_code = course["join_code"] if "join_code" in course.keys() and course["join_code"] else ""

    is_ta = role in ("ta", "teacher")
    role_label = "Teaching Assistant / Co-Teacher" if role == "ta" else ("Teacher / Faculty" if role == "teacher" else "Student")

    def _worker():
        try:
            msg = MIMEMultipart("alternative")
            if is_ta:
                msg["Subject"] = f"Course Invitation: Join {course_code} as {role_label} - {course_title}"
            else:
                msg["Subject"] = f"Course Invitation: {course_code} - {course_title}"
            msg["From"] = f"{from_name} <{gmail_user}>"
            msg["To"] = recipient_email

            role_desc = f"as a {role_label}" if is_ta else "to join"
            role_badge = f"""<div style="font-size: 11px; font-weight: 700; color: #3730a3; background: #e0e7ff; display: inline-block; padding: 2px 8px; border-radius: 9999px; margin-top: 6px; text-transform: uppercase;">Role: {role_label}</div>""" if is_ta else ""
            btn_text = f"Accept Invitation & Join as {role_label}" if is_ta else "Accept Invitation & Join Class"

            plain_text = f"""Hello,

You have been invited by Prof. {teacher_name} {role_desc} {course_code}: {course_title} ({course_section}) on Hoodle LMS.

To accept this invitation and enroll immediately, visit:
{join_url}

Alternatively, you can sign in to your Hoodle account where this invitation is waiting on your Home Screen, or enter Class Code: {join_code}

Best regards,
Hoodle LMS • Accelerated Classroom & Lab Learning
ACCL Research Lab, IIT Bhilai
"""
            html_text = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Course Invitation</title></head>
<body style="margin: 0; padding: 0; background-color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #1e293b;">
  <div style="max-width: 580px; margin: 30px auto; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.06);">
    <div style="background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%); padding: 28px 24px; text-align: center; color: white;">
      <h1 style="margin: 0 0 6px; font-size: 22px; font-weight: 800; letter-spacing: -0.5px;">Hoodle LMS</h1>
      <p style="margin: 0; font-size: 13px; opacity: 0.9;">Accelerated Classroom &amp; Lab Learning</p>
    </div>
    <div style="padding: 28px 24px;">
      <div style="font-size: 15px; margin-bottom: 16px;">
        Hello <strong>{student_roll or 'Colleague'}</strong>,
      </div>
      <p style="font-size: 14px; line-height: 1.6; color: #475569; margin-bottom: 20px;">
        <strong>Prof. {teacher_name}</strong> has invited you to join the class {role_desc} on Hoodle:
      </p>
      
      <div style="background: #eff6ff; border-left: 4px solid #2563eb; border-radius: 6px; padding: 16px; margin-bottom: 24px;">
        <div style="font-size: 12px; font-weight: 700; color: #2563eb; text-transform: uppercase;">Classroom</div>
        <div style="font-size: 18px; font-weight: 800; color: #0f172a; margin-top: 2px;">{course_code}: {course_title}</div>
        <div style="font-size: 13px; color: #64748b; margin-top: 4px;">{course_section}</div>
        {role_badge}
        <div style="font-size: 12px; color: #64748b; margin-top: 8px;">Class Code: <code style="background: #dbeafe; color: #1e40af; padding: 2px 6px; border-radius: 4px; font-weight: 700;">{join_code}</code></div>
      </div>

      <div style="text-align: center; margin: 28px 0;">
        <a href="{join_url}" style="background: #2563eb; color: #ffffff; text-decoration: none; padding: 12px 28px; font-size: 15px; font-weight: 700; border-radius: 8px; display: inline-block; box-shadow: 0 4px 12px rgba(37, 99, 235, 0.35);">
          {btn_text} &rarr;
        </a>
      </div>

      <p style="font-size: 12.5px; color: #64748b; line-height: 1.5; margin-top: 24px; border-top: 1px solid #f1f5f9; padding-top: 16px;">
        If you already have a Hoodle account, you can sign in to your home screen where this invitation is waiting for you. If you don't have an account, clicking the button above will guide you to register with roll number <strong>{student_roll or ''}</strong>.
      </p>
    </div>
    <div style="background: #f8fafc; padding: 14px; text-align: center; font-size: 11px; color: #94a3b8; border-top: 1px solid #e2e8f0;">
      Hoodle LMS &bull; Accelerated Computing Research Lab (ACCL), IIT Bhilai
    </div>
  </div>
</body>
</html>
"""
            msg.attach(MIMEText(plain_text, "plain"))
            msg.attach(MIMEText(html_text, "html"))

            server = smtplib.SMTP("smtp.gmail.com", 587, timeout=15)
            server.starttls()
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, [recipient_email], msg.as_string())
            server.quit()
            app.logger.info("Course invitation email sent to %s for %s (role: %s)", recipient_email, course_code, role)
        except Exception as e:
            app.logger.warning("Failed to send course invitation email to %s: %s", recipient_email, e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def send_event_notification_email(recipient_emails, subject, heading, body_text, action_url=None, action_text="View in Hoodle", actor_name=None, actor_role=None):
    """
    Sends notification email via Gmail SMTP in background thread for specific course events:
    1. Assignment / Exam creation
    2. Grade & feedback published
    3. Direct messages between student and teacher/TA
    4. Course announcements
    5. Course enrollment / role assignment
    """
    gmail_user, gmail_pass, from_name = get_smtp_config()

    if not gmail_user or not gmail_pass or not recipient_emails:
        return

    if isinstance(recipient_emails, str):
        recipient_emails = [recipient_emails]

    # Filter unique valid emails
    clean_emails = list({e.strip() for e in recipient_emails if e and "@" in e})
    if not clean_emails:
        return

    full_action_url = resolve_portal_url(action_url) if action_url else None

    def _worker():
        try:
            server = smtplib.SMTP("smtp.gmail.com", 587, timeout=15)
            server.starttls()
            server.login(gmail_user, gmail_pass)

            from_display = f"{actor_name} via Hoodle LMS" if actor_name else from_name

            for rec_email in clean_emails:
                try:
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = subject
                    msg["From"] = f"{from_display} <{gmail_user}>"
                    msg["To"] = rec_email

                    attribution_html = ""
                    attribution_plain = ""
                    if actor_name:
                        role_badge = f"""<span style="display: inline-block; background: #e0e7ff; color: #3730a3; padding: 2px 8px; border-radius: 9999px; font-size: 11px; font-weight: 700; text-transform: uppercase; margin-left: 6px;">{actor_role or 'Instructor'}</span>""" if actor_role else ""
                        attribution_html = f"""
                        <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-left: 4px solid #2563eb; padding: 10px 14px; margin-bottom: 18px; border-radius: 6px; font-size: 13px; color: #334155;">
                          <strong style="color: #0f172a;">Instructor / Staff:</strong> {actor_name} {role_badge}
                        </div>
                        """
                        attribution_plain = f"Action by: {actor_name} ({actor_role or 'Staff'})\n\n"

                    button_html = ""
                    if full_action_url:
                        button_html = f"""
                        <div style="text-align: center; margin: 26px 0;">
                          <a href="{full_action_url}" style="background: #2563eb; color: #ffffff; text-decoration: none; padding: 12px 28px; font-size: 15px; font-weight: 700; border-radius: 8px; display: inline-block; box-shadow: 0 4px 12px rgba(37, 99, 235, 0.35);">
                            {action_text} &rarr;
                          </a>
                        </div>
                        """

                    html_text = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>{subject}</title></head>
<body style="margin: 0; padding: 0; background-color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #1e293b;">
  <div style="max-width: 580px; margin: 30px auto; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.06);">
    <div style="background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%); padding: 24px; text-align: center; color: white;">
      <h1 style="margin: 0 0 4px; font-size: 22px; font-weight: 800; letter-spacing: -0.5px;">Hoodle LMS</h1>
      <p style="margin: 0; font-size: 13px; opacity: 0.9;">Accelerated Classroom &amp; Lab Learning</p>
    </div>
    <div style="padding: 26px 24px;">
      <h2 style="font-size: 18px; font-weight: 700; color: #0f172a; margin-top: 0; margin-bottom: 14px;">{heading}</h2>
      {attribution_html}
      <div style="font-size: 14px; line-height: 1.6; color: #475569; white-space: pre-line; margin-bottom: 20px;">
        {body_text}
      </div>
      {button_html}
    </div>
    <div style="background: #f8fafc; padding: 14px; text-align: center; font-size: 11px; color: #94a3b8; border-top: 1px solid #e2e8f0;">
      Hoodle LMS &bull; Accelerated Computing Research Lab (ACCL), IIT Bhilai
    </div>
  </div>
</body>
</html>"""
                    plain_text = f"{heading}\n\n{attribution_plain}{body_text}\n\n{full_action_url if full_action_url else ''}"
                    msg.attach(MIMEText(plain_text, "plain"))
                    msg.attach(MIMEText(html_text, "html"))
                    server.sendmail(gmail_user, [rec_email], msg.as_string())
                except Exception as ex_single:
                    app.logger.warning("Failed to send notification email to %s: %s", rec_email, ex_single)

            server.quit()
            app.logger.info("Event notification '%s' sent to %d recipients", subject, len(clean_emails))
        except Exception as e:
            app.logger.warning("Failed to send event notification emails: %s", e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


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
        "exam_view", "exam_submit", "exam_receipt", "unsubmit_coursework", "logout", "static", "login",
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
    user_courses = []
    unread_messages_count = 0
    locker_used_bytes = 0

    if user:
        conn = get_db()
        if user["role"] == "student":
            active_exam = get_active_exam_lockdown_for_student(user["id"])
            user_courses = conn.execute("""
                SELECT c.id, c.code, c.title, c.section, c.theme_color
                FROM courses c
                JOIN course_enrollments ce ON c.id = ce.course_id
                WHERE ce.user_id = ? AND ce.role = 'student' AND c.is_archived = 0
                ORDER BY c.title ASC
            """, (user["id"],)).fetchall()
        elif user["role"] in ("teacher", "ta"):
            user_courses = conn.execute("""
                SELECT DISTINCT c.id, c.code, c.title, c.section, c.theme_color
                FROM courses c
                LEFT JOIN course_enrollments ce ON c.id = ce.course_id AND ce.user_id = ?
                WHERE c.is_archived = 0 AND (c.teacher_id = ? OR ce.role IN ('teacher', 'ta'))
                ORDER BY c.title ASC
            """, (user["id"], user["id"])).fetchall()
        elif user["role"] == "admin":
            user_courses = conn.execute("""
                SELECT c.id, c.code, c.title, c.section, c.theme_color
                FROM courses c
                WHERE c.is_archived = 0
                ORDER BY c.title ASC
            """).fetchall()

        unread_row = conn.execute("""
            SELECT COUNT(*) FROM direct_messages WHERE recipient_id = ? AND is_read = 0
        """, (user["id"],)).fetchone()
        unread_messages_count = unread_row[0] if unread_row else 0

        locker_row = conn.execute("""
            SELECT COALESCE(SUM(file_size), 0) FROM student_locker_files WHERE user_id = ?
        """, (user["id"],)).fetchone()
        locker_used_bytes = locker_row[0] if locker_row else 0
        conn.close()

    return {
        "current_user": user,
        "now_iso": now_str,
        "active_exam_lockdown": active_exam,
        "user_courses": user_courses,
        "unread_messages_count": unread_messages_count,
        "locker_used_bytes": locker_used_bytes
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

            # Handle pending course join code from short link
            pending_code = session.pop("pending_join_code", None)
            session.pop("pending_course_name", None)
            if pending_code:
                conn = get_db()
                target_course = conn.execute("SELECT * FROM courses WHERE UPPER(join_code) = ? AND is_archived = 0", (pending_code.upper(),)).fetchone()
                if target_course:
                    existing_enroll = conn.execute("SELECT * FROM course_enrollments WHERE course_id = ? AND user_id = ?", (target_course["id"], user["id"])).fetchone()
                    if not existing_enroll:
                        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        conn.execute("INSERT INTO course_enrollments (course_id, user_id, role, enrolled_at) VALUES (?, ?, 'student', ?)", (target_course["id"], user["id"], now_str))
                        conn.commit()
                        flash(f"Welcome! You have been enrolled in {target_course['code']}: {target_course['title']}.", "success")
                    else:
                        flash(f"Welcome back! Opening {target_course['code']}: {target_course['title']}.", "info")
                    conn.close()
                    return redirect(url_for("course_stream", course_id=target_course["id"]))
                conn.close()

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
        # Security hardening: Public self-registration strictly assigns the Student role.
        # Faculty and Instructor accounts must be created by Administrators.
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
        new_user = conn.execute("SELECT last_insert_rowid() as id").fetchone()
        new_user_id = new_user["id"] if new_user else None

        # Auto-link any pending course invitations matching this student's roll number or email
        if new_user_id:
            conn.execute("""
                UPDATE course_invitations
                SET student_id = ?
                WHERE status = 'pending' AND (
                    (student_roll IS NOT NULL AND UPPER(student_roll) = ?) OR
                    (student_email IS NOT NULL AND student_email != '' AND LOWER(student_email) = ?)
                )
            """, (new_user_id, roll_number.upper(), email.lower()))

        conn.commit()
        conn.close()

        # Auto-login and auto-enroll newly registered student if joining via short link
        pending_code = session.get("pending_join_code")
        if pending_code and new_user_id:
            session["user_id"] = new_user_id
            session["username"] = username
            session["display_name"] = full_name
            session["role"] = role
            session["roll_number"] = roll_number

            session.pop("pending_join_code", None)
            session.pop("pending_course_name", None)

            conn = get_db()
            target_course = conn.execute("SELECT * FROM courses WHERE UPPER(join_code) = ? AND is_archived = 0", (pending_code.upper(),)).fetchone()
            if target_course:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute("INSERT INTO course_enrollments (course_id, user_id, role, enrolled_at) VALUES (?, ?, 'student', ?)", (target_course["id"], new_user_id, now_str))
                conn.commit()
                flash(f"Account created! Welcome to {target_course['code']}: {target_course['title']}.", "success")
                conn.close()
                return redirect(url_for("course_stream", course_id=target_course["id"]))
            conn.close()
            return redirect(url_for("dashboard"))

        flash("Registration successful! You may now sign in.", "success")
        return redirect(url_for("login"))

    return render_template("login.html", register_active=True)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    """Allows any logged-in student, teacher, or admin to securely update their own password."""
    if request.method == "POST":
        current_pwd = request.form.get("current_password", "")
        new_pwd = request.form.get("new_password", "").strip()
        confirm_pwd = request.form.get("confirm_password", "").strip()

        if not current_pwd or not new_pwd:
            flash("All password fields are required.", "danger")
            return render_template("set_password.html", is_self_change=True)

        if new_pwd != confirm_pwd:
            flash("New passwords do not match.", "danger")
            return render_template("set_password.html", is_self_change=True)

        if len(new_pwd) < 6:
            flash("New password must be at least 6 characters.", "danger")
            return render_template("set_password.html", is_self_change=True)

        conn = get_db()
        user = conn.execute("SELECT password_hash FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if not user or not check_password_hash(user["password_hash"], current_pwd):
            conn.close()
            flash("Incorrect current password.", "danger")
            return render_template("set_password.html", is_self_change=True)

        new_hash = hash_password(new_pwd)
        conn.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?", (new_hash, session["user_id"]))
        conn.commit()
        conn.close()

        flash("Your password has been changed successfully.", "success")
        return redirect(url_for("dashboard"))

    return render_template("set_password.html", is_self_change=True)


@app.route("/set-password", methods=["GET", "POST"])
@login_required
def set_password():
    """Handles mandatory password updates upon first login or administrative reset."""
    if request.method == "POST":
        new_pwd = request.form.get("new_password", "").strip()
        confirm_pwd = request.form.get("confirm_password", "").strip()

        if not new_pwd or len(new_pwd) < 6:
            return render_template("set_password.html", error="Password must be at least 6 characters.")

        if new_pwd != confirm_pwd:
            return render_template("set_password.html", error="Passwords do not match.")

        new_hash = hash_password(new_pwd)
        conn = get_db()
        conn.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?", (new_hash, session["user_id"]))
        conn.commit()
        conn.close()

        flash("Password updated successfully.", "success")
        return redirect(url_for("dashboard"))

    return render_template("set_password.html", is_self_change=False)


# --- Main Dashboard & Course Hub ---

@app.route("/usage-guide")
@app.route("/guide")
@app.route("/student-guide")
def usage_guide():
    """
    Comprehensive visual usage guide for students and faculty with step-by-step screenshots:
    - Account login & 1-click joining via invite codes / short links
    - Daily attendance scanning (live camera viewfinder, HTTPS switcher, and photo fallback)
    - Exam mode lockdown, real-time countdown timer, immutable deadlines, and SHA-256 submission receipts
    - Monitoring attendance quotas (>=75%) and coursework grade progress
    """
    user = get_current_user() if "user_id" in session else None
    return render_template("student_guide.html", current_user=user)


student_guide = usage_guide


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
    pending_invitations = []
    locker_stats = {"used_bytes": 0, "quota_bytes": user["storage_quota_bytes"], "percent": 0}

    if user["role"] == "student":
        user_roll = (user["roll_number"] or "").strip().upper()
        user_email = (user["email"] or "").strip().lower()

        # Query active pending invitations for this student
        pending_invitations = conn.execute("""
            SELECT ci.id as invite_id, ci.token, ci.sent_at,
                   c.id as course_id, c.code as course_code, c.title as course_title,
                   c.section as course_section, c.theme_color,
                   u.display_name as teacher_name
            FROM course_invitations ci
            JOIN courses c ON ci.course_id = c.id
            JOIN users u ON ci.invited_by = u.id
            WHERE (ci.student_id = ? OR (ci.student_roll IS NOT NULL AND UPPER(ci.student_roll) = ?) OR (ci.student_email IS NOT NULL AND student_email != '' AND LOWER(ci.student_email) = ?))
              AND ci.status = 'pending'
              AND c.is_archived = 0
              AND NOT EXISTS (
                  SELECT 1 FROM course_enrollments ce WHERE ce.course_id = c.id AND ce.user_id = ?
              )
            ORDER BY ci.sent_at DESC
        """, (user["id"], user_roll, user_email, user["id"])).fetchall()

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
        locker_stats=locker_stats,
        pending_invitations=pending_invitations
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


@app.route("/j/<join_code>")
@app.route("/join/<join_code>")
def quick_join(join_code):
    """Shorter 1-click course invite link for students."""
    join_code = (join_code or "").strip().upper()
    conn = get_db()
    course = conn.execute("SELECT * FROM courses WHERE UPPER(join_code) = ? AND is_archived = 0", (join_code,)).fetchone()
    conn.close()

    if not course:
        flash("Invalid or expired class invitation link. Please check with your instructor.", "danger")
        return redirect(url_for("dashboard") if "user_id" in session else url_for("login"))

    # If the user is already authenticated:
    if "user_id" in session:
        user_id = session["user_id"]
        conn = get_db()
        existing = conn.execute("""
            SELECT * FROM course_enrollments WHERE course_id = ? AND user_id = ?
        """, (course["id"], user_id)).fetchone()

        if not existing:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""
                INSERT INTO course_enrollments (course_id, user_id, role, enrolled_at)
                VALUES (?, ?, 'student', ?)
            """, (course["id"], user_id, now_str))
            conn.commit()
            flash(f"Welcome! You have successfully enrolled in {course['code']}: {course['title']}.", "success")
        else:
            flash(f"Opening {course['code']}: {course['title']}.", "info")
        conn.close()
        return redirect(url_for("course_stream", course_id=course["id"]))

    # Guest / student not logged in: store pending code in session and prompt sign-in
    session["pending_join_code"] = join_code
    session["pending_course_name"] = f"{course['code']}: {course['title']}"
    flash(f"You've been invited to join {course['code']}: {course['title']}. Please sign in or register below.", "info")
    return redirect(url_for("login", join=join_code))


@app.route("/c/<int:course_id>")
def short_course_link(course_id):
    """Short URL jump to course stream."""
    return redirect(url_for("course_stream", course_id=course_id))


@app.route("/e/<int:coursework_id>")
def short_exam_link(coursework_id):
    """Short URL jump directly into an exam."""
    conn = get_db()
    cw = conn.execute("SELECT course_id FROM coursework WHERE id = ?", (coursework_id,)).fetchone()
    conn.close()
    if not cw:
        abort(404, "Exam not found")
    return redirect(url_for("exam_view", course_id=cw["course_id"], coursework_id=coursework_id))


@app.route("/cw/<int:coursework_id>")
def short_coursework_link(coursework_id):
    """Short URL jump directly into an assignment / lab."""
    conn = get_db()
    cw = conn.execute("SELECT course_id FROM coursework WHERE id = ?", (coursework_id,)).fetchone()
    conn.close()
    if not cw:
        abort(404, "Coursework not found")
    return redirect(url_for("coursework_detail", course_id=cw["course_id"], coursework_id=coursework_id))


@app.route("/p")
@app.route("/go")
def short_portal_link():
    """Short URL jump directly to dashboard or login."""
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


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


@app.route("/courses/<int:course_id>/settings", methods=["POST"])
@teacher_required
def update_course_settings(course_id):
    conn = get_db()
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if not course:
        conn.close()
        abort(404, "Course not found")

    code = request.form.get("code", "").strip().upper()
    title = request.form.get("title", "").strip()
    section = request.form.get("section", "Section A").strip()
    description = request.form.get("description", "").strip()
    theme_color = request.form.get("theme_color", "indigo").strip().lower()

    valid_themes = ("indigo", "emerald", "amber", "rose", "teal", "purple", "slate")
    if theme_color not in valid_themes:
        theme_color = "indigo"

    if not code or not title:
        conn.close()
        flash("Course Code and Title are required.", "danger")
        return redirect(url_for("course_stream", course_id=course_id))

    attendance_threshold_val = request.form.get("attendance_threshold", "50.0").strip()
    try:
        attendance_threshold = max(0.0, min(100.0, float(attendance_threshold_val)))
    except (ValueError, TypeError):
        attendance_threshold = 50.0

    conn.execute("""
        UPDATE courses SET
            code = ?, title = ?, section = ?, description = ?, theme_color = ?, attendance_threshold = ?
        WHERE id = ?
    """, (code, title, section, description, theme_color, attendance_threshold, course_id))
    conn.commit()
    conn.close()

    flash("Class settings updated successfully.", "success")
    return redirect(url_for("course_stream", course_id=course_id))



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

    # Determine if current user has instructor, co-teacher, TA, or admin privileges for this course
    curr_user = get_current_user()
    is_teacher_or_admin = False
    if curr_user and curr_user["role"] in ("teacher", "admin"):
        is_teacher_or_admin = True
    elif course["teacher_id"] == user_id:
        is_teacher_or_admin = True
    else:
        co_t = conn.execute("""
            SELECT 1 FROM course_enrollments
            WHERE course_id = ? AND user_id = ? AND role IN ('teacher', 'ta', 'co-teacher')
        """, (course_id, user_id)).fetchone()
        if co_t:
            is_teacher_or_admin = True

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
    return render_template(
        "course_stream.html",
        course=course,
        announcements=announcements,
        is_teacher_or_admin=is_teacher_or_admin,
        active_tab="stream"
    )


@app.route("/courses/<int:course_id>/announcements", methods=["POST"])
@teacher_required
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

    # Event Notification Email: Teacher/TA created an announcement
    c_info = conn.execute("SELECT code, title FROM courses WHERE id = ?", (course_id,)).fetchone()
    student_rows = conn.execute("""
        SELECT u.email FROM course_enrollments ce
        JOIN users u ON ce.user_id = u.id
        WHERE ce.course_id = ? AND ce.role = 'student' AND u.email IS NOT NULL AND u.email != ''
    """, (course_id,)).fetchall()
    student_emails = [r["email"] for r in student_rows]
    curr_user = get_current_user()
    t_name = curr_user["display_name"] if curr_user else "Instructor"
    t_role = (curr_user["role"] if curr_user else "Teacher").upper()
    conn.close()

    if student_emails and c_info:
        snippet = (content[:280] + "...") if len(content) > 280 else content
        send_event_notification_email(
            recipient_emails=student_emails,
            subject=f"[{c_info['code']}] Announcement by {t_name}: {c_info['title']}",
            heading=f"Class Announcement by {t_name} ({t_role})",
            body_text=f"Announcement posted by {t_name} ({t_role}) for {c_info['code']}: {c_info['title']}:\n\n{snippet}",
            action_url=f"/courses/{course_id}/stream",
            action_text="View in Course Stream",
            actor_name=t_name,
            actor_role=t_role
        )

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

    # Check enrollment
    if session.get("role") not in ("teacher", "admin"):
        enr = conn.execute("SELECT 1 FROM course_enrollments WHERE course_id = ? AND user_id = ?", (ann["course_id"], session["user_id"])).fetchone()
        if not enr:
            conn.close()
            abort(403, "Not enrolled in this course")

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

    curr_user = get_current_user()
    is_teacher_or_admin = False
    if curr_user and curr_user["role"] in ("teacher", "admin"):
        is_teacher_or_admin = True
    elif course["teacher_id"] == user_id:
        is_teacher_or_admin = True
    else:
        co_t = conn.execute("""
            SELECT 1 FROM course_enrollments
            WHERE course_id = ? AND user_id = ? AND role IN ('teacher', 'ta', 'co-teacher')
        """, (course_id, user_id)).fetchone()
        if co_t:
            is_teacher_or_admin = True

    conn.close()
    return render_template(
        "course_classwork.html",
        course=course,
        topics=topics,
        topic_map=topic_map,
        is_teacher_or_admin=is_teacher_or_admin,
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

    # Event Notification Email: Teacher/TA created assignment or exam
    c_info = conn.execute("SELECT code, title FROM courses WHERE id = ?", (course_id,)).fetchone()
    student_rows = conn.execute("""
        SELECT u.email FROM course_enrollments ce
        JOIN users u ON ce.user_id = u.id
        WHERE ce.course_id = ? AND ce.role = 'student' AND u.email IS NOT NULL AND u.email != ''
    """, (course_id,)).fetchall()
    student_emails = [r["email"] for r in student_rows]
    curr_user = get_current_user()
    t_name = curr_user["display_name"] if curr_user else "Instructor"
    t_role = (curr_user["role"] if curr_user else "Teacher").upper()
    conn.close()

    if student_emails and c_info:
        send_event_notification_email(
            recipient_emails=student_emails,
            subject=f"[{c_info['code']}] New {cw_type.capitalize()} Created by {t_name}: {title}",
            heading=f"New {cw_type.capitalize()} Created by {t_name}",
            body_text=f"{t_name} ({t_role}) has created and published a new {cw_type}: '{title}' in {c_info['code']}: {c_info['title']}.\n\nPoints: {points}\nDue Date: {due_date or 'No due date'}",
            action_url=f"/courses/{course_id}/coursework/{coursework_id}",
            action_text=f"View {cw_type.capitalize()} Details",
            actor_name=t_name,
            actor_role=t_role
        )

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


@app.route("/courses/<int:course_id>/coursework/<int:coursework_id>/edit", methods=["POST"])
@teacher_required
def edit_coursework(course_id, coursework_id):
    conn = get_db()
    cw = conn.execute("SELECT * FROM coursework WHERE id = ? AND course_id = ?", (coursework_id, course_id)).fetchone()
    if not cw:
        conn.close()
        abort(404, "Coursework not found")

    title = request.form.get("title", "").strip()
    if not title:
        conn.close()
        flash("Title is required for coursework.", "danger")
        return redirect(url_for("coursework_detail", course_id=course_id, coursework_id=coursework_id))

    description = request.form.get("description", "").strip()
    topic_id_raw = request.form.get("topic_id", "")
    topic_id = int(topic_id_raw) if topic_id_raw and topic_id_raw.isdigit() else None
    points = int(request.form.get("points", 100)) if request.form.get("points") else 100
    category_id_raw = request.form.get("category_id", "")
    category_id = int(category_id_raw) if category_id_raw and category_id_raw.isdigit() else None
    allow_multiple = 1 if request.form.get("allow_multiple") else 0
    allow_late = 1 if request.form.get("allow_late") else 0
    allowed_types = request.form.get("allowed_types", cw["allowed_types"]).strip().lower()
    labs = request.form.get("labs", cw["labs"] or "Lab 1, Lab 2, Lab 3, CC-101").strip()

    due_date = request.form.get("due_date", "").strip() or None
    start_time = request.form.get("start_time", "").strip() or None
    end_time = request.form.get("end_time", "").strip() or None

    conn.execute("""
        UPDATE coursework SET
            title = ?, description = ?, topic_id = ?, points = ?,
            category_id = ?, allow_multiple = ?, allow_late = ?,
            allowed_types = ?, labs = ?, due_date = ?, start_time = ?, end_time = ?
        WHERE id = ? AND course_id = ?
    """, (
        title, description, topic_id, points,
        category_id, allow_multiple, allow_late,
        allowed_types, labs, due_date, start_time, end_time,
        coursework_id, course_id
    ))

    # Optional attachments upload
    uploaded_files = request.files.getlist("attachments")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for f in uploaded_files:
        if f and f.filename:
            fname = secure_filename(f.filename)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            stored = f"att_{course_id}_{coursework_id}_{timestamp}_{fname}"
            dest = ATTACHMENTS_DIR / stored
            f.save(dest)
            conn.execute("""
                INSERT INTO coursework_attachments (coursework_id, original_filename, stored_filename, file_path, file_size, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (coursework_id, fname, stored, str(dest), dest.stat().st_size, now_str))

    conn.commit()
    conn.close()

    flash(f"Coursework '{title}' updated successfully.", "success")
    return redirect(url_for("coursework_detail", course_id=course_id, coursework_id=coursework_id))



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

    topics = conn.execute("SELECT * FROM topics WHERE course_id = ? ORDER BY display_order ASC, id ASC", (course_id,)).fetchall()
    categories = conn.execute("SELECT * FROM course_grading_categories WHERE course_id = ? ORDER BY id ASC", (course_id,)).fetchall()


    now = datetime.now()
    is_past_due = False
    if cw["due_date"]:
        due_dt = parse_iso_datetime(cw["due_date"])
        if due_dt and now > due_dt:
            is_past_due = True

    is_exam_ended = False
    if cw["is_exam_mode"] == 1 or cw["type"] == "exam":
        cutoff = parse_iso_datetime(cw["end_time"]) if cw["end_time"] else (parse_iso_datetime(cw["due_date"]) if cw["due_date"] else None)
        if cutoff and now > cutoff:
            is_exam_ended = True

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
        topics=topics,
        categories=categories,
        is_past_due=is_past_due,
        is_exam_ended=is_exam_ended,
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

    # Strict authorization: user must be enrolled in the course or instructor/admin
    enr = conn.execute("SELECT 1 FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course_id, user_id)).fetchone()
    if not enr and user["role"] not in ("teacher", "admin"):
        conn.close()
        flash("You must be enrolled in this course to submit work.", "danger")
        return redirect(url_for("dashboard"))

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    is_late = 0
    late_minutes = 0

    # If coursework is an exam or strict exam mode is active:
    if cw["is_exam_mode"] == 1 or cw["type"] == "exam":
        cutoff = parse_iso_datetime(cw["end_time"]) if cw["end_time"] else (parse_iso_datetime(cw["due_date"]) if cw["due_date"] else None)
        if cutoff and now > cutoff:
            conn.close()
            flash("The exam deadline has passed. Modifying or re-submitting after the exam has ended is strictly locked.", "danger")
            return redirect(url_for("exam_view", course_id=course_id, coursework_id=coursework_id))


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
    cw = conn.execute("SELECT * FROM coursework WHERE id = ? AND course_id = ?", (coursework_id, course_id)).fetchone()
    if not cw:
        conn.close()
        abort(404)

    # 1. Exam locking check: Exams can NEVER be unsubmitted
    if cw["is_exam_mode"] == 1 or cw["type"] == "exam":
        conn.close()
        flash("Submissions for exams cannot be unsubmitted. All exam submissions are permanently recorded.", "danger")
        return redirect(url_for("exam_view", course_id=course_id, coursework_id=coursework_id))

    # 2. Deadline check for regular coursework: Cannot unsubmit after deadline has passed
    now = datetime.now()
    if cw["due_date"]:
        due_dt = parse_iso_datetime(cw["due_date"])
        if due_dt and now > due_dt:
            conn.close()
            flash("Submission deadline has passed. Work cannot be unsubmitted after the deadline.", "danger")
            return redirect(url_for("coursework_detail", course_id=course_id, coursework_id=coursework_id))

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
    et = parse_iso_datetime(cw["end_time"]) if cw["end_time"] else (parse_iso_datetime(cw["due_date"]) if cw["due_date"] else None)

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
        end_iso=cw["end_time"] or cw["due_date"]
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

    # Enrollment check
    enr = conn.execute("SELECT 1 FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course_id, user_id)).fetchone()
    if not enr and user["role"] not in ("teacher", "admin"):
        conn.close()
        flash("You are not enrolled in this course.", "danger")
        return redirect(url_for("dashboard"))

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

    is_valid_zip = zipfile.is_zipfile(file)
    file.seek(0)
    if not is_valid_zip:
        conn.close()
        flash("Invalid archive format: The file uploaded is not a valid ZIP archive.", "danger")
        return redirect(url_for("exam_view", course_id=course_id, coursework_id=coursework_id))

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # Strict Exam Cutoff Check: Once exam time is over, everything is permanently locked
    cutoff = None
    if cw["end_time"]:
        cutoff = parse_iso_datetime(cw["end_time"])
    elif cw["due_date"]:
        cutoff = parse_iso_datetime(cw["due_date"])

    if cutoff and now > cutoff:
        conn.close()
        flash("The exam deadline has passed. Modifying or re-submitting after the exam has ended is strictly locked.", "danger")
        return redirect(url_for("exam_view", course_id=course_id, coursework_id=coursework_id))


    existing = conn.execute("SELECT * FROM submissions WHERE coursework_id = ? AND student_id = ?", (coursework_id, user_id)).fetchone()
    if existing:
        if cw["allow_multiple"] == 0:
            conn.close()
            flash("Single submission policy: You have already submitted your exam. Re-submissions are not permitted.", "warning")
            return redirect(url_for("exam_view", course_id=course_id, coursework_id=coursework_id))

    is_late = 0
    late_minutes = 0


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
        SELECT s.*, cw.title as exam_title, cw.type as cw_type, c.code as course_code, c.title as course_title,
               c.id as course_id, c.teacher_id
        FROM submissions s
        JOIN coursework cw ON s.coursework_id = cw.id
        JOIN courses c ON cw.course_id = c.id
        WHERE s.receipt_token = ?
    """, (receipt_token,)).fetchone()

    if not sub:
        conn.close()
        abort(404, "Receipt not found")

    user_id = session.get("user_id")
    role = session.get("role")
    is_owner = (sub["student_id"] == user_id)
    is_admin = (role == "admin")
    is_instructor = (sub["teacher_id"] == user_id)

    if not (is_owner or is_admin or is_instructor):
        co_t = conn.execute("""
            SELECT 1 FROM course_enrollments
            WHERE course_id = ? AND user_id = ? AND role IN ('teacher', 'ta', 'co-teacher')
        """, (sub["course_id"], user_id)).fetchone()
        if not co_t:
            conn.close()
            abort(403, "Access restricted: You cannot view other students' receipts.")

    conn.close()
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

    pending_invitations = []
    if is_teacher_or_admin:
        pending_invitations = conn.execute("""
            SELECT ci.*, u.display_name as student_name
            FROM course_invitations ci
            LEFT JOIN users u ON ci.student_id = u.id
            WHERE ci.course_id = ? AND ci.status = 'pending'
            ORDER BY ci.sent_at DESC
        """, (course_id,)).fetchall()

    conn.close()
    return render_template(
        "course_people.html",
        course=course,
        teachers=teachers,
        students=students,
        available_users=available_users,
        pending_invitations=pending_invitations,
        is_teacher_or_admin=is_teacher_or_admin,
        active_tab="people"
    )


@app.route("/courses/<int:course_id>/people/add-coteacher", methods=["POST"])
@teacher_required
def add_co_teacher(course_id):
    course = get_course_or_404(course_id)
    curr_user = get_current_user()
    teacher_name = curr_user["display_name"] if curr_user else "Instructor"
    teacher_role = (curr_user["role"] if curr_user else "Teacher").upper()

    target_id = request.form.get("user_id")
    identifier = request.form.get("identifier", "").strip()
    assigned_role = request.form.get("role", "ta").strip().lower()
    if assigned_role not in ("student", "ta", "teacher"):
        assigned_role = "ta"

    if assigned_role == "teacher":
        enroll_role = "ta"
        role_label = "Faculty / Teacher"
    elif assigned_role == "student":
        enroll_role = "student"
        role_label = "Student"
    else:
        enroll_role = "ta"
        role_label = "Co-Teacher / TA"

    conn = get_db()
    user = None
    if target_id and target_id.isdigit():
        user = conn.execute("SELECT * FROM users WHERE id = ?", (int(target_id),)).fetchone()
    elif identifier:
        user = conn.execute("""
            SELECT * FROM users
            WHERE LOWER(username) = ? OR LOWER(roll_number) = ? OR (email != '' AND LOWER(email) = ?)
        """, (identifier.lower(), identifier.lower(), identifier.lower())).fetchone()

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not user:
        if not identifier:
            conn.close()
            flash("Please choose a registered user or enter a roll number or email.", "danger")
            return redirect(url_for("course_people", course_id=course_id))

        # Identifier provided but user not registered yet: create pending invitation with assigned_role
        is_email = "@" in identifier
        if is_email:
            student_email = identifier.lower()
            student_roll = None
        else:
            student_roll = identifier.upper()
            student_email = f"{identifier.lower()}@iitbhilai.ac.in"

        # Check existing invitation in this course
        existing_inv = conn.execute("""
            SELECT id FROM course_invitations
            WHERE course_id = ? AND status = 'pending' AND (
                (student_roll IS NOT NULL AND UPPER(student_roll) = ?) OR 
                (student_email IS NOT NULL AND LOWER(student_email) = ?)
            )
        """, (course_id, student_roll or "", student_email or "")).fetchone()

        invite_token = secrets.token_urlsafe(24)
        if existing_inv:
            conn.execute("""
                UPDATE course_invitations
                SET token = ?, sent_at = ?, role = ?, student_roll = COALESCE(?, student_roll), student_email = COALESCE(?, student_email)
                WHERE id = ?
            """, (invite_token, now_str, assigned_role, student_roll, student_email, existing_inv["id"]))
        else:
            conn.execute("""
                INSERT INTO course_invitations (course_id, invited_by, student_roll, student_email, student_id, token, status, role, sent_at)
                VALUES (?, ?, ?, ?, NULL, ?, 'pending', ?, ?)
            """, (course_id, curr_user["id"], student_roll, student_email, invite_token, assigned_role, now_str))

        conn.commit()
        conn.close()

        # Send invitation email
        send_course_invitation_email(course, student_email, student_roll, teacher_name, invite_token, role=assigned_role)
        flash(f"Invitation to join as {role_label} sent to '{identifier}'. An invitation email has been dispatched.", "success")
        return redirect(url_for("course_people", course_id=course_id))

    # If assigning teacher role, elevate user system role to teacher
    if assigned_role == "teacher":
        conn.execute("UPDATE users SET role = 'teacher' WHERE id = ?", (user["id"],))

    existing = conn.execute("SELECT * FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course_id, user["id"])).fetchone()

    if existing:
        conn.execute("UPDATE course_enrollments SET role = ? WHERE course_id = ? AND user_id = ?", (enroll_role, course_id, user["id"]))
    else:
        conn.execute("INSERT INTO course_enrollments (course_id, user_id, role, enrolled_at) VALUES (?, ?, ?, ?)", (course_id, user["id"], enroll_role, now_str))

    conn.commit()
    conn.close()

    # Dispatch email notification to user if email exists
    if user["email"]:
        c_code = course["code"]
        c_title = course["title"]
        c_sec = course["section"] if "section" in course.keys() and course["section"] else "Section A"
        if enroll_role == "student":
            subj = f"[{c_code}] Enrolled in {c_title}"
            head = f"You have been enrolled as a Student"
            body = f"Hello {user['display_name']},\n\nProf. {teacher_name} has enrolled you as a Student in {c_code}: {c_title} ({c_sec}).\n\nYou can now access the course stream, classwork, attendance, and grades directly on Hoodle LMS."
            act_text = "Open Course"
        else:
            subj = f"[{c_code}] Added as {role_label}: {c_title}"
            head = f"You have been added as {role_label}"
            body = f"Hello {user['display_name']},\n\nProf. {teacher_name} has added you as {role_label} for {c_code}: {c_title} ({c_sec}).\n\nYou now have teaching and course management privileges (classwork creation, grading, attendance tracking, and announcements) for this course on Hoodle LMS."
            act_text = "Open Course & Manage"

        send_event_notification_email(
            recipient_emails=[user["email"]],
            subject=subj,
            heading=head,
            body_text=body,
            action_url=f"/courses/{course_id}",
            action_text=act_text,
            actor_name=teacher_name,
            actor_role=teacher_role
        )

    flash(f"'{user['display_name']}' is now configured as {role_label} for {course['code']}. Notification email dispatched.", "success")
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
        if session.get("role") == "admin":
            conn.execute("UPDATE users SET role = 'teacher' WHERE id = ?", (target_user_id,))
        conn.execute("UPDATE course_enrollments SET role = 'ta' WHERE course_id = ? AND user_id = ?", (course_id, target_user_id))
        label = "Teacher / Co-Instructor"
    elif new_role == "ta":
        conn.execute("UPDATE course_enrollments SET role = 'ta' WHERE course_id = ? AND user_id = ?", (course_id, target_user_id))
        label = "Co-Teacher"
    else:  # student
        conn.execute("UPDATE course_enrollments SET role = 'student' WHERE course_id = ? AND user_id = ?", (course_id, target_user_id))
        label = "Student"

    conn.commit()
    conn.close()

    curr_user = get_current_user()
    teacher_name = curr_user["display_name"] if curr_user else "Instructor"
    teacher_role = (curr_user["role"] if curr_user else "Teacher").upper()

    if target_user["email"]:
        send_event_notification_email(
            recipient_emails=[target_user["email"]],
            subject=f"[{course['code']}] Course Role Updated to {label}",
            heading=f"Role Updated in {course['code']}",
            body_text=f"Hello {target_user['display_name']},\n\nYour role in {course['code']}: {course['title']} has been updated to {label} by Prof. {teacher_name}.\n\nYou can access the course now on Hoodle LMS.",
            action_url=f"/courses/{course_id}",
            action_text="Open Course",
            actor_name=teacher_name,
            actor_role=teacher_role
        )

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


# --- Course Invitations & Student Home Screen Join ---

@app.route("/courses/<int:course_id>/invite", methods=["POST"])
@teacher_required
def invite_students(course_id):
    course = get_course_or_404(course_id)
    curr_user = get_current_user()
    teacher_name = curr_user["display_name"]
    raw_input = request.form.get("students_input", "").strip()
    invite_role = request.form.get("role", "student").strip().lower()
    if invite_role not in ("student", "ta", "teacher"):
        invite_role = "student"

    if not raw_input:
        flash("Please enter one or more roll numbers or email addresses.", "warning")
        return redirect(url_for("course_people", course_id=course_id))

    # Split by comma, semicolon, newline, or whitespace
    entries = [tok.strip() for tok in re.split(r"[,;\s\n\r]+", raw_input) if tok.strip()]
    if not entries:
        flash("No valid roll numbers or emails found.", "warning")
        return redirect(url_for("course_people", course_id=course_id))

    conn = get_db()
    enrolled_user_ids = {row["user_id"] for row in conn.execute(
        "SELECT user_id FROM course_enrollments WHERE course_id = ?", (course_id,)
    ).fetchall()}

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    added_count = 0
    already_enrolled = 0
    registered_now = 0
    pending_signup = 0

    # Avoid duplicate processing within the same batch
    seen_entries = set()

    for entry in entries:
        entry_clean = entry.strip()
        entry_lower = entry_clean.lower()
        entry_upper = entry_clean.upper()

        if entry_lower in seen_entries:
            continue
        seen_entries.add(entry_lower)

        is_email = "@" in entry_clean
        user = None

        if is_email:
            student_email = entry_lower
            user = conn.execute("SELECT * FROM users WHERE LOWER(email) = ?", (entry_lower,)).fetchone()
            student_roll = (user["roll_number"] or "").upper() if user and user["roll_number"] else None
        else:
            student_roll = entry_upper
            user = conn.execute("""
                SELECT * FROM users
                WHERE UPPER(roll_number) = ? OR LOWER(username) = ?
            """, (entry_upper, entry_lower)).fetchone()
            student_email = user["email"].lower() if user and user["email"] else f"{entry_lower}@iitbhilai.ac.in"

        if user and user["id"] in enrolled_user_ids:
            already_enrolled += 1
            continue

        student_id = user["id"] if user else None

        # Check existing invitation in this course
        existing_inv = None
        if student_id:
            existing_inv = conn.execute("""
                SELECT id FROM course_invitations
                WHERE course_id = ? AND status = 'pending' AND (
                    student_id = ? OR 
                    (student_roll IS NOT NULL AND UPPER(student_roll) = ?) OR 
                    (student_email IS NOT NULL AND LOWER(student_email) = ?)
                )
            """, (course_id, student_id, student_roll or "", student_email or "")).fetchone()
        elif student_roll:
            existing_inv = conn.execute("""
                SELECT id FROM course_invitations
                WHERE course_id = ? AND status = 'pending' AND UPPER(student_roll) = ?
            """, (course_id, student_roll)).fetchone()
        elif student_email:
            existing_inv = conn.execute("""
                SELECT id FROM course_invitations
                WHERE course_id = ? AND status = 'pending' AND LOWER(student_email) = ?
            """, (course_id, student_email)).fetchone()

        invite_token = secrets.token_urlsafe(24)

        if existing_inv:
            conn.execute("""
                UPDATE course_invitations
                SET token = ?, sent_at = ?, role = ?, student_id = COALESCE(?, student_id), student_email = COALESCE(?, student_email)
                WHERE id = ?
            """, (invite_token, now_str, invite_role, student_id, student_email, existing_inv["id"]))
        else:
            conn.execute("""
                INSERT INTO course_invitations (course_id, invited_by, student_roll, student_email, student_id, token, status, role, sent_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (course_id, curr_user["id"], student_roll, student_email, student_id, invite_token, invite_role, now_str))

        added_count += 1
        if student_id:
            registered_now += 1
        else:
            pending_signup += 1

        # Dispatch background Gmail email if recipient email exists
        if student_email:
            send_course_invitation_email(course, student_email, student_roll, teacher_name, invite_token, role=invite_role)

    conn.commit()
    conn.close()

    role_label = "Student(s)" if invite_role == "student" else "Co-Teacher(s) / TA(s)"
    msg_parts = [f"Processed {len(seen_entries)} entry(ies): {added_count} {role_label} invitation(s) saved."]
    if registered_now:
        msg_parts.append(f"{registered_now} registered user(s) will see the invitation immediately on their home screen.")
    if pending_signup:
        msg_parts.append(f"{pending_signup} unregistered user(s) will receive the invitation automatically when they sign up.")
    if already_enrolled:
        msg_parts.append(f"{already_enrolled} already enrolled user(s) were skipped.")

    flash(" ".join(msg_parts), "success")
    return redirect(url_for("course_people", course_id=course_id))


@app.route("/courses/<int:course_id>/invitations/<int:invite_id>/revoke", methods=["POST"])
@teacher_required
def revoke_invitation(course_id, invite_id):
    conn = get_db()
    conn.execute("DELETE FROM course_invitations WHERE id = ? AND course_id = ?", (invite_id, course_id))
    conn.commit()
    conn.close()
    flash("Course invitation revoked.", "info")
    return redirect(url_for("course_people", course_id=course_id))


@app.route("/invitations/<int:invite_id>/accept", methods=["POST"])
@login_required
def accept_invitation(invite_id):
    user = get_current_user()
    user_id = user["id"]
    user_roll = (user["roll_number"] or "").strip().upper()
    user_email = (user["email"] or "").strip().lower()

    conn = get_db()
    inv = conn.execute("""
        SELECT ci.*, c.code as course_code, c.title as course_title, c.is_archived
        FROM course_invitations ci
        JOIN courses c ON ci.course_id = c.id
        WHERE ci.id = ? AND ci.status = 'pending'
    """, (invite_id,)).fetchone()

    if not inv:
        conn.close()
        flash("Invitation not found or has already been accepted/expired.", "warning")
        return redirect(url_for("dashboard"))

    # Verify student identity matches this invitation
    authorized = (
        inv["student_id"] == user_id or
        (user_roll and inv["student_roll"] and inv["student_roll"].upper() == user_roll) or
        (user_email and inv["student_email"] and inv["student_email"].lower() == user_email)
    )

    if not authorized:
        conn.close()
        flash("You are not authorized to accept this invitation.", "danger")
        return redirect(url_for("dashboard"))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inv_role = "student"
    if "role" in inv.keys() and inv["role"] in ("student", "ta", "teacher"):
        inv_role = "ta" if inv["role"] in ("ta", "teacher") else "student"

    if "role" in inv.keys() and inv["role"] == "teacher":
        conn.execute("UPDATE users SET role = 'teacher' WHERE id = ?", (user_id,))

    # Enroll user in course
    existing_enr = conn.execute("SELECT id FROM course_enrollments WHERE course_id = ? AND user_id = ?", (inv["course_id"], user_id)).fetchone()
    if existing_enr:
        conn.execute("UPDATE course_enrollments SET role = ? WHERE course_id = ? AND user_id = ?", (inv_role, inv["course_id"], user_id))
    else:
        conn.execute("""
            INSERT INTO course_enrollments (course_id, user_id, role, enrolled_at)
            VALUES (?, ?, ?, ?)
        """, (inv["course_id"], user_id, inv_role, now_str))

    # Mark invitation accepted
    conn.execute("""
        UPDATE course_invitations
        SET status = 'accepted', student_id = ?, responded_at = ?
        WHERE id = ?
    """, (user_id, now_str, invite_id))
    conn.commit()
    conn.close()

    role_desc = " as Co-Teacher / TA" if inv_role == "ta" else ""
    flash(f"Welcome! You have successfully joined {inv['course_code']}: {inv['course_title']}{role_desc}.", "success")
    return redirect(url_for("course_stream", course_id=inv["course_id"]))


@app.route("/invitations/<int:invite_id>/decline", methods=["POST"])
@login_required
def decline_invitation(invite_id):
    user = get_current_user()
    user_id = user["id"]
    user_roll = (user["roll_number"] or "").strip().upper()
    user_email = (user["email"] or "").strip().lower()

    conn = get_db()
    inv = conn.execute("SELECT * FROM course_invitations WHERE id = ? AND status = 'pending'", (invite_id,)).fetchone()
    if not inv:
        conn.close()
        flash("Invitation not found.", "warning")
        return redirect(url_for("dashboard"))

    authorized = (
        inv["student_id"] == user_id or
        (user_roll and inv["student_roll"] and inv["student_roll"].upper() == user_roll) or
        (user_email and inv["student_email"] and inv["student_email"].lower() == user_email)
    )
    if not authorized:
        conn.close()
        flash("You are not authorized to decline this invitation.", "danger")
        return redirect(url_for("dashboard"))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE course_invitations SET status = 'declined', responded_at = ? WHERE id = ?", (now_str, invite_id))
    conn.commit()
    conn.close()

    flash("Course invitation declined.", "info")
    return redirect(url_for("dashboard"))


@app.route("/invitations/accept/<token>")
def accept_invitation_by_token(token):
    conn = get_db()
    inv = conn.execute("""
        SELECT ci.*, c.code as course_code, c.title as course_title, c.is_archived
        FROM course_invitations ci
        JOIN courses c ON ci.course_id = c.id
        WHERE ci.token = ? AND ci.status = 'pending' AND c.is_archived = 0
    """, (token,)).fetchone()

    if not inv:
        conn.close()
        flash("This course invitation link is invalid or has already been accepted.", "warning")
        return redirect(url_for("dashboard" if "user_id" in session else "login"))

    # If student is already logged in
    if "user_id" in session:
        user_id = session["user_id"]
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        inv_role = "student"
        if "role" in inv.keys() and inv["role"] in ("student", "ta", "teacher"):
            inv_role = "ta" if inv["role"] in ("ta", "teacher") else "student"

        if "role" in inv.keys() and inv["role"] == "teacher":
            conn.execute("UPDATE users SET role = 'teacher' WHERE id = ?", (user_id,))

        existing_enr = conn.execute("SELECT id FROM course_enrollments WHERE course_id = ? AND user_id = ?", (inv["course_id"], user_id)).fetchone()
        if existing_enr:
            conn.execute("UPDATE course_enrollments SET role = ? WHERE course_id = ? AND user_id = ?", (inv_role, inv["course_id"], user_id))
        else:
            conn.execute("""
                INSERT INTO course_enrollments (course_id, user_id, role, enrolled_at)
                VALUES (?, ?, ?, ?)
            """, (inv["course_id"], user_id, inv_role, now_str))

        conn.execute("""
            UPDATE course_invitations
            SET status = 'accepted', student_id = ?, responded_at = ?
            WHERE id = ?
        """, (user_id, now_str, inv["id"]))
        conn.commit()
        conn.close()
        role_desc = " as Co-Teacher / TA" if inv_role == "ta" else ""
        flash(f"Successfully joined {inv['course_code']}: {inv['course_title']}{role_desc}!", "success")
        return redirect(url_for("course_stream", course_id=inv["course_id"]))

    conn.close()
    flash(f"Please sign in or register to join {inv['course_code']}: {inv['course_title']}.", "info")
    return redirect(url_for("login", next=url_for("accept_invitation_by_token", token=token)))


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
                threshold = 50.0
                if course and "attendance_threshold" in course.keys() and course["attendance_threshold"] is not None:
                    try:
                        threshold = float(course["attendance_threshold"])
                    except (ValueError, TypeError):
                        threshold = 50.0

                if att_pct <= threshold:
                    effective_att_pct = 0.0
                else:
                    denominator = max(0.1, 100.0 - threshold)
                    effective_att_pct = min(100.0, ((att_pct - threshold) / denominator) * 100.0)

                cat_earned_pct = round(effective_att_pct, 1)
                cat_weighted_pts = round(effective_att_pct * (weight / 100.0), 2)
                cat_scores[cat_id] = {
                    "id": cat_id,
                    "name": cat["name"],
                    "weight": weight,
                    "earned_points": present_count,
                    "max_points": total_attendance_sessions,
                    "raw_percentage": att_pct,
                    "percentage": cat_earned_pct,
                    "effective_percentage": cat_earned_pct,
                    "threshold": threshold,
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
                "effective_percentage": cat_scores.get(next((c["id"] for c in categories if c["is_attendance"]), None), {}).get("effective_percentage", att_pct),
                "threshold": cat_scores.get(next((c["id"] for c in categories if c["is_attendance"]), None), {}).get("threshold", 50.0),
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

    # Process attendance cutoff threshold if provided
    att_thresh_val = request.form.get("attendance_threshold")
    if att_thresh_val is not None:
        try:
            att_thresh = max(0.0, min(100.0, float(att_thresh_val.strip())))
            conn.execute("UPDATE courses SET attendance_threshold = ? WHERE id = ?", (att_thresh, course_id))
        except (ValueError, TypeError):
            pass

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

    # Event Notification Email: Teacher/TA graded coursework
    sub_info = conn.execute("""
        SELECT s.student_id, u.display_name as student_name, u.email as student_email,
               cw.title as cw_title, cw.points as cw_points,
               c.code as course_code, c.title as course_title
        FROM submissions s
        JOIN users u ON s.student_id = u.id
        JOIN coursework cw ON s.coursework_id = cw.id
        JOIN courses c ON cw.course_id = c.id
        WHERE s.id = ?
    """, (submission_id,)).fetchone()
    curr_user = get_current_user()
    grader_name = curr_user["display_name"] if curr_user else "Instructor"
    grader_role = (curr_user["role"] if curr_user else "Teacher").upper()

    if sub_info and sub_info["student_email"]:
        send_event_notification_email(
            recipient_emails=[sub_info["student_email"]],
            subject=f"Grade Published by {grader_name}: [{sub_info['course_code']}] {sub_info['cw_title']}",
            heading=f"Coursework Evaluated & Graded by {grader_name}",
            body_text=f"Hello {sub_info['student_name']},\n\nYour submission for '{sub_info['cw_title']}' in {sub_info['course_code']}: {sub_info['course_title']} has been evaluated and graded by {grader_name} ({grader_role}).\n\nScore: {grade} / {sub_info['cw_points'] or 100}\nFeedback: {feedback or 'No written comments'}",
            action_url=f"/courses/{course_id}/coursework/{coursework_id}",
            action_text="View Feedback & Submission",
            actor_name=grader_name,
            actor_role=grader_role
        )

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

    safe_inline_exts = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
    ext = Path(f["original_filename"]).suffix.lower()
    is_safe = ext in safe_inline_exts
    mimetype = "application/pdf" if ext == ".pdf" else None
    return send_file(f["file_path"], mimetype=mimetype, as_attachment=not is_safe, download_name=f["original_filename"])


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
    course_id = None

    if att and os.path.exists(att["file_path"]):
        file_path = att["file_path"]
        original_filename = att["original_filename"]
        cw = conn.execute("SELECT course_id FROM coursework WHERE id = ?", (att["coursework_id"],)).fetchone()
        if cw:
            course_id = cw["course_id"]
    else:
        ann = conn.execute("SELECT * FROM announcements WHERE id = ?", (att_id,)).fetchone()
        if ann and ann["attachment_path"] and os.path.exists(ann["attachment_path"]):
            file_path = ann["attachment_path"]
            original_filename = ann["attachment_name"]
            course_id = ann["course_id"]

    if not file_path:
        conn.close()
        abort(404, "Attachment not found")

    # Authorization check: user must be enrolled in the course or instructor/admin
    if course_id and session.get("role") not in ("teacher", "admin"):
        enr = conn.execute("SELECT 1 FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course_id, session["user_id"])).fetchone()
        if not enr:
            conn.close()
            abort(403, "Access denied: You are not enrolled in the course for this attachment.")

    conn.close()

    safe_inline_exts = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
    ext = Path(original_filename).suffix.lower()
    is_inline = (request.path.startswith("/view/") or request.args.get("view") == "1" or request.args.get("inline") == "1") and (ext in safe_inline_exts)
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

    safe_inline_exts = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
    ext = Path(sub["original_filename"]).suffix.lower()
    is_inline = (request.path.startswith("/view/") or request.args.get("view") == "1" or request.args.get("inline") == "1") and (ext in safe_inline_exts)
    mimetype = "application/pdf" if ext == ".pdf" else None

    return send_file(
        sub["file_path"],
        mimetype=mimetype,
        as_attachment=not is_inline,
        download_name=sub["original_filename"]
    )


# --- Hoodle Brand Assets & Logo Downloads ---

@app.route("/brand")
@admin_required
def brand_assets():
    current_user = get_current_user()
    return render_template("brand_assets.html", current_user=current_user)


@app.route("/brand/download/<asset_name>")
@admin_required
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


@app.route("/admin/teachers/create", methods=["POST"])
@admin_required
def admin_create_teacher():
    username = request.form.get("username", "").strip().lower()
    display_name = request.form.get("display_name", "").strip()
    roll_number = request.form.get("roll_number", "").strip().upper()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "").strip()
    role = request.form.get("role", "teacher").strip().lower()
    if role not in ("teacher", "admin"):
        role = "teacher"

    if not username or not display_name or not password:
        flash("Username, Full Name, and Password are required.", "danger")
        return redirect(url_for("admin_users"))

    if len(password) < 6:
        flash("Password must be at least 6 characters.", "danger")
        return redirect(url_for("admin_users"))

    conn = get_db()
    existing = conn.execute("""
        SELECT id FROM users
        WHERE LOWER(username) = ? OR (email != '' AND LOWER(email) = ?) OR (roll_number != '' AND UPPER(roll_number) = ?)
    """, (username, email, roll_number)).fetchone()

    if existing:
        conn.close()
        flash("A user with this Username, Email, or Faculty ID already exists.", "danger")
        return redirect(url_for("admin_users"))

    pwd_hash = hash_password(password)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO users (username, roll_number, email, password_hash, display_name, role, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (username, roll_number, email, pwd_hash, display_name, role, now_str))
    conn.commit()
    conn.close()

    flash(f"Teacher account for {display_name} created successfully!", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@teacher_required
def admin_change_role(user_id):
    curr_user = get_current_user()
    new_role = request.form.get("role", "student").strip().lower()
    target_redirect = request.referrer or (url_for("admin_users") if curr_user and curr_user["role"] == "admin" else url_for("dashboard"))
    if new_role not in ("student", "teacher", "admin"):
        flash("Invalid role.", "danger")
        return redirect(target_redirect)

    if new_role == "admin" and curr_user["role"] != "admin":
        flash("Only an administrator can assign the Administrator role.", "danger")
        return redirect(target_redirect)

    if user_id == session.get("user_id") and new_role != "admin":
        flash("Administrators cannot demote their own account.", "danger")
        return redirect(target_redirect)

    conn = get_db()
    target_user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target_user:
        conn.close()
        abort(404)

    if target_user["role"] == "admin" and curr_user["role"] != "admin":
        conn.close()
        flash("Cannot change the role of an Administrator.", "danger")
        return redirect(target_redirect)

    conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    conn.commit()
    conn.close()
    flash("User role updated successfully.", "success")
    return redirect(target_redirect)


@app.route("/teacher/students/<int:user_id>/role", methods=["POST"])
@teacher_required
def teacher_change_student_role(user_id):
    """Allows instructors to promote/demote students to/from teaching assistants."""
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


@app.route("/admin/users/delete-bulk", methods=["POST"])
@admin_required
def admin_delete_users_bulk():
    user_ids = request.form.getlist("user_ids")
    if not user_ids:
        flash("No users selected for deletion.", "warning")
        return redirect(url_for("admin_users"))

    curr_user_id = session.get("user_id")
    valid_ids = []
    for uid_str in user_ids:
        try:
            uid = int(uid_str)
            if uid != curr_user_id:
                valid_ids.append(uid)
        except (ValueError, TypeError):
            continue

    if not valid_ids:
        flash("Cannot delete selected accounts (you cannot delete your own active administrator account).", "warning")
        return redirect(url_for("admin_users"))

    conn = get_db()
    placeholders = ",".join("?" for _ in valid_ids)
    conn.execute(f"DELETE FROM users WHERE id IN ({placeholders})", valid_ids)
    conn.commit()
    conn.close()

    flash(f"Successfully deleted {len(valid_ids)} user account(s) and their associated records.", "success")
    return redirect(url_for("admin_users"))


# --- Admin Disaster Recovery & Offsite Backup System ---

def load_backup_config():
    backup_dir = Path(app.config.get("BACKUPS_DIR", BASE_DIR / "backups"))
    config_file = backup_dir / "backup_config.json"
    defaults = {
        "remote_host": os.getenv("REMOTE_BACKUP_HOST", "gpu2"),
        "remote_user": os.getenv("REMOTE_BACKUP_USER", "kishan"),
        "remote_dir": os.getenv("REMOTE_BACKUP_DIR", "/data2/kishan/hoodle_backups"),
        "retention_days": int(os.getenv("REMOTE_BACKUP_RETENTION_DAYS", "30")),
    }
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {
                    "remote_host": str(data.get("remote_host", defaults["remote_host"])).strip(),
                    "remote_user": str(data.get("remote_user", defaults["remote_user"])).strip(),
                    "remote_dir": str(data.get("remote_dir", defaults["remote_dir"])).strip(),
                    "retention_days": int(data.get("retention_days", defaults["retention_days"])),
                }
        except Exception:
            pass
    return defaults


def save_backup_config(config):
    backup_dir = Path(app.config.get("BACKUPS_DIR", BASE_DIR / "backups"))
    backup_dir.mkdir(parents=True, exist_ok=True)
    config_json = backup_dir / "backup_config.json"
    config_env = backup_dir / "backup_config.env"
    with open(config_json, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    with open(config_env, "w", encoding="utf-8") as f:
        f.write("# Hoodle LMS Dynamic Backup Configuration\n")
        f.write(f'REMOTE_HOST="{config["remote_host"]}"\n')
        f.write(f'REMOTE_USER="{config["remote_user"]}"\n')
        f.write(f'REMOTE_DIR="{config["remote_dir"]}"\n')
        f.write(f'RETENTION_DAYS="{config["retention_days"]}"\n')


def check_remote_backup_ssh(config):
    target = f"{config['remote_user']}@{config['remote_host']}"
    cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        target,
        f"mkdir -p {config['remote_dir']} 2>/dev/null; df -h {config['remote_dir']} 2>/dev/null | tail -1"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=7)
        if res.returncode == 0 and res.stdout.strip():
            parts = res.stdout.strip().split()
            free_space = parts[3] if len(parts) >= 4 else "Available"
            return True, free_space
    except Exception:
        pass
    return False, None


@app.route("/admin/backup")
@admin_required
def admin_backup():
    """Renders the Disaster Recovery & Offsite Backup console."""
    backup_dir = Path(app.config.get("BACKUPS_DIR", BASE_DIR / "backups"))
    latest_file = backup_dir / "hoodle_backup_latest.tar.gz"

    local_backups = []
    if backup_dir.exists():
        for p in sorted(backup_dir.glob("hoodle_backup_*.tar.gz"), key=os.path.getmtime, reverse=True):
            if p.name in ("hoodle_backup_latest.tar.gz", "hoodle_backup_latest_from_remote.tar.gz", "hoodle_backup_latest_from_gpu2.tar.gz"):
                continue
            st = p.stat()
            local_backups.append({
                "filename": p.name,
                "size_display": format_file_size(st.st_size),
                "modified_display": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "path": str(p)
            })

    latest_info = None
    if latest_file.exists():
        st = latest_file.stat()
        latest_info = {
            "size_display": format_file_size(st.st_size),
            "modified_display": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "exists": True
        }

    backup_config = load_backup_config()
    remote_connected, remote_free_space = check_remote_backup_ssh(backup_config)

    return render_template(
        "admin_backup.html",
        local_backups=local_backups,
        latest_info=latest_info,
        backup_config=backup_config,
        remote_connected=remote_connected,
        remote_free_space=remote_free_space,
        gpu2_connected=remote_connected
    )


@app.route("/admin/backup/settings", methods=["POST"])
@admin_required
def admin_backup_settings():
    """Allows administrators to dynamically configure the remote backup server, SSH credentials, path, and retention."""
    remote_host = request.form.get("remote_host", "").strip()
    remote_user = request.form.get("remote_user", "").strip()
    remote_dir = request.form.get("remote_dir", "").strip()
    try:
        retention_days = max(1, int(request.form.get("retention_days", 30)))
    except (ValueError, TypeError):
        retention_days = 30

    if not remote_host or not remote_user or not remote_dir:
        flash("Remote host, SSH username, and directory path cannot be empty.", "danger")
        return redirect(url_for("admin_backup"))

    config = {
        "remote_host": remote_host,
        "remote_user": remote_user,
        "remote_dir": remote_dir,
        "retention_days": retention_days
    }
    save_backup_config(config)

    ok, free_space = check_remote_backup_ssh(config)
    if ok:
        flash(f"✅ Backup destination updated! Connected to {remote_user}@{remote_host}:{remote_dir} ({free_space} free space).", "success")
    else:
        flash(f"⚠️ Backup destination updated, but could not connect to {remote_user}@{remote_host}. Verify passwordless SSH keys are configured.", "warning")

    return redirect(url_for("admin_backup"))


@app.route("/admin/backup/trigger", methods=["POST"])
@admin_required
def admin_backup_trigger():
    """Triggers an immediate offsite backup to the configured remote server."""
    script_path = BASE_DIR / "scripts" / "hoodle_backup.sh"
    if not script_path.exists():
        flash("Backup script not found on server.", "danger")
        return redirect(url_for("admin_backup"))

    backup_config = load_backup_config()
    try:
        proc = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True, text=True, timeout=180, cwd=str(BASE_DIR)
        )
        if proc.returncode == 0:
            flash(f"✅ Offsite backup completed successfully! Database, submissions, lockers, and attachments synced to {backup_config['remote_host']}:{backup_config['remote_dir']}.", "success")
        else:
            flash(f"⚠️ Backup script finished with warnings: {proc.stderr[:300] or proc.stdout[:300]}", "warning")
    except subprocess.TimeoutExpired:
        flash("Backup process timed out. Check backup log on server.", "warning")
    except Exception as e:
        flash(f"Failed to execute backup: {str(e)}", "danger")

    return redirect(url_for("admin_backup"))


@app.route("/admin/backup/restore", methods=["POST"])
@admin_required
def admin_backup_restore():
    """Restores entire system from latest remote backup with safety confirmation."""
    confirm_phrase = request.form.get("confirm_phrase", "").strip()
    if confirm_phrase != "RESTORE-HOODLE":
        flash("❌ Restoration cancelled: You must type 'RESTORE-HOODLE' exactly to confirm.", "danger")
        return redirect(url_for("admin_backup"))

    script_path = BASE_DIR / "scripts" / "hoodle_restore.sh"
    if not script_path.exists():
        flash("Restore script not found on server.", "danger")
        return redirect(url_for("admin_backup"))

    backup_config = load_backup_config()
    try:
        proc = subprocess.run(
            ["bash", str(script_path), "--from-remote"],
            capture_output=True, text=True, timeout=300, cwd=str(BASE_DIR)
        )
        if proc.returncode == 0:
            flash(f"🎉 System restored successfully from latest backup on {backup_config['remote_host']}! All databases and storage are synchronized.", "success")
        else:
            flash(f"⚠️ Restore script reported an issue: {proc.stderr[:300] or proc.stdout[:300]}", "danger")
    except Exception as e:
        flash(f"Restoration failed: {str(e)}", "danger")

    return redirect(url_for("admin_backup"))


@app.route("/admin/backup/download-latest")
@admin_required
def admin_backup_download_latest():
    """Allows administrator to download the latest backup tar.gz archive directly."""
    backup_dir = Path(app.config.get("BACKUPS_DIR", BASE_DIR / "backups"))
    latest_file = backup_dir / "hoodle_backup_latest.tar.gz"
    if not latest_file.exists():
        flash("No backup archive found. Run a backup first.", "warning")
        return redirect(url_for("admin_backup"))

    return send_file(
        str(latest_file),
        mimetype="application/gzip",
        as_attachment=True,
        download_name=f"hoodle_backup_latest_{datetime.now().strftime('%Y%m%d')}.tar.gz"
    )


# --- Admin Gmail & Notification Settings ---

@app.route("/admin/email")
@admin_required
def admin_email_settings():
    gmail_user, gmail_pass, from_name = get_smtp_config()
    conn = get_db()
    portal_url_row = conn.execute("SELECT value FROM system_settings WHERE key = 'portal_base_url'").fetchone()
    conn.close()
    portal_base_url = portal_url_row["value"] if portal_url_row else os.environ.get("PORTAL_BASE_URL", "http://10.10.14.104/lms")

    masked_pass = ("•" * 12 + gmail_pass[-4:]) if len(gmail_pass) >= 4 else ("•" * 8 if gmail_pass else "Not configured")

    return render_template(
        "admin_email.html",
        gmail_user=gmail_user,
        masked_pass=masked_pass,
        raw_pass_len=len(gmail_pass),
        from_name=from_name,
        portal_base_url=portal_base_url
    )


@app.route("/admin/email/update", methods=["POST"])
@admin_required
def admin_update_email_settings():
    new_user = request.form.get("gmail_user", "").strip()
    new_pass = request.form.get("gmail_password", "").strip().replace(" ", "")
    new_from = request.form.get("from_name", "").strip()
    new_base_url = request.form.get("portal_base_url", "").strip().rstrip("/")

    if not new_user:
        flash("Gmail address cannot be empty.", "danger")
        return redirect(url_for("admin_email_settings"))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO system_settings (key, value, updated_at) VALUES ('gmail_smtp_user', ?, ?)", (new_user, now_str))
    if new_pass:
        conn.execute("INSERT OR REPLACE INTO system_settings (key, value, updated_at) VALUES ('gmail_app_password', ?, ?)", (new_pass, now_str))
    if new_from:
        conn.execute("INSERT OR REPLACE INTO system_settings (key, value, updated_at) VALUES ('gmail_from_name', ?, ?)", (new_from, now_str))
    if new_base_url:
        conn.execute("INSERT OR REPLACE INTO system_settings (key, value, updated_at) VALUES ('portal_base_url', ?, ?)", (new_base_url, now_str))
    conn.commit()
    conn.close()

    flash("Gmail SMTP settings saved successfully. New settings take effect immediately.", "success")
    return redirect(url_for("admin_email_settings"))


@app.route("/admin/email/test", methods=["POST"])
@admin_required
def admin_test_email_settings():
    test_recipient = request.form.get("test_recipient", "").strip()
    if not test_recipient or "@" not in test_recipient:
        flash("Please enter a valid recipient email to send test verification.", "danger")
        return redirect(url_for("admin_email_settings"))

    gmail_user, gmail_pass, from_name = get_smtp_config()
    if not gmail_user or not gmail_pass:
        flash("Gmail user or App Password is not configured.", "danger")
        return redirect(url_for("admin_email_settings"))

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587, timeout=15)
        server.starttls()
        server.login(gmail_user, gmail_pass)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Hoodle LMS - Gmail SMTP Test Verification"
        msg["From"] = f"{from_name} <{gmail_user}>"
        msg["To"] = test_recipient

        html = f"""<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 24px; max-width: 520px; border: 1px solid #e2e8f0; border-radius: 12px; background: #ffffff;">
          <div style="background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%); color: white; padding: 16px 20px; border-radius: 8px; margin-bottom: 20px;">
            <h2 style="margin: 0; font-size: 18px;">Hoodle LMS &bull; SMTP Verification</h2>
          </div>
          <p style="font-size: 14px; color: #334155; line-height: 1.5;">This email confirms that Gmail SMTP credentials for <strong>{gmail_user}</strong> are authenticated and actively transmitting emails.</p>
          <div style="background: #f8fafc; border-left: 4px solid #22c55e; padding: 12px 16px; border-radius: 4px; font-size: 13px; color: #15803d; margin: 18px 0;">
            &check; Gmail SMTP Authentication: Verified OK
          </div>
          <p style="font-size: 11px; color: #94a3b8; margin: 0; border-top: 1px solid #f1f5f9; padding-top: 12px;">Dispatched by Hoodle LMS Administrator.</p>
        </div>"""
        plain = f"Hoodle LMS SMTP Test Verification.\nSent from {gmail_user} to {test_recipient}.\nSMTP Status: Verified OK."
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html, "html"))

        server.sendmail(gmail_user, [test_recipient], msg.as_string())
        server.quit()
        flash(f"✅ Success! Test email was verified and sent to {test_recipient} via {gmail_user}.", "success")
    except Exception as e:
        app.logger.warning("SMTP test verification failed: %s", e)
        flash(f"❌ SMTP verification failed: {str(e)}. Please check your Google App Password.", "danger")

    return redirect(url_for("admin_email_settings"))


@app.route("/admin/email/reset", methods=["POST"])
@admin_required
def admin_reset_email_settings():
    conn = get_db()
    conn.execute("DELETE FROM system_settings WHERE key IN ('gmail_smtp_user', 'gmail_app_password', 'gmail_from_name', 'portal_base_url')")
    conn.commit()
    conn.close()
    flash("Gmail SMTP settings reset to system defaults (hoodle.lms@gmail.com).", "info")
    return redirect(url_for("admin_email_settings"))



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
    
    # Construct student scan URL respecting reverse-proxy prefix
    scan_url = resolve_portal_url(f"/attend/{course_id}?token={token}&type={session_type}")
    
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

    if request.args.get("demo_success") == "1" and request.remote_addr in ("127.0.0.1", "::1", "localhost"):
        return render_template(
            "attendance_confirm.html",
            course=course,
            success=True,
            session_type="Lecture",
            today_str=datetime.now().strftime("%Y-%m-%d"),
            now_str=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            user=user
        )
    
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
    enr = conn.execute("SELECT 1 FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course_id, user["id"])).fetchone()
    if not enr and user["role"] not in ("teacher", "admin"):
        conn.close()
        flash("You are not enrolled in this course.", "danger")
        return redirect(url_for("dashboard"))

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


@app.route("/courses/<int:course_id>/attendance/import", methods=["POST"])
@teacher_required
def attendance_import(course_id):
    """
    Teacher Past Attendance Importer:
    Ingests tabular data copied directly from Google Sheets / Google Apps Script / CSV files.
    Format example:
    Date \t Email/Roll \t Section \t Session Type \t Status \t [Optional Key]
    9/9/2026 \t b26cs021@iitbhilai.ac.in \t Batch 1 \t Lecture \t PRESENT \t b26cs021@iitbhilai.ac.in_LECTURE_2026-09-09
    
    Features:
    - Auto-detects delimiters (tab, comma, semicolon, space)
    - Auto-provisions student accounts with hashed passwords if missing
    - Auto-enrolls students in the course
    - Auto-creates attendance_sessions record if not present
    - Idempotent upsert via attendance_key (no duplicates or crashes)
    """
    course = get_course_or_404(course_id)
    current_u = get_current_user()
    
    raw_text = ""
    if "import_file" in request.files:
        f = request.files["import_file"]
        if f and f.filename:
            raw_text = f.read().decode("utf-8", errors="replace")
            
    if not raw_text.strip():
        raw_text = request.form.get("import_data", "").strip()
        
    if not raw_text.strip():
        flash("Please paste attendance data or upload a CSV/TSV file.", "warning")
        return redirect(url_for("course_attendance", course_id=course_id))
        
    default_session_type = request.form.get("default_session_type", "Lecture").strip() or "Lecture"
    default_date = request.form.get("default_date", "").strip() or datetime.now().strftime("%Y-%m-%d")
    auto_create_users = request.form.get("auto_create_users", "1") == "1"
    auto_enroll = request.form.get("auto_enroll", "1") == "1"
    
    def parse_attendance_date(s):
        s = s.strip()
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
        if m:
            p1, p2, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{yr:04d}-{p1:02d}-{p2:02d}"
        return None

    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    
    conn = get_db()
    
    imported_count = 0
    updated_count = 0
    created_users_count = 0
    enrolled_count = 0
    skipped_count = 0
    distinct_dates = set()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        lower_line = line.lower()
        if any(lower_line.startswith(h) for h in ("date", "timestamp", "roll", "email", "student id", "student_email", "#")):
            continue
            
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        elif "," in line:
            try:
                reader = csv.reader([line])
                parts = [p.strip() for p in next(reader)]
            except Exception:
                parts = [p.strip() for p in line.split(",")]
        elif ";" in line:
            parts = [p.strip() for p in line.split(";")]
        else:
            parts = [p.strip() for p in re.split(r"\s+", line)]
            
        parts = [p for p in parts if p != ""]
        if not parts:
            continue
            
        row_date = None
        row_ident = None
        row_section = None
        row_type = None
        row_status = "PRESENT"
        
        date_candidate = parse_attendance_date(parts[0])
        if date_candidate:
            row_date = date_candidate
            if len(parts) > 1:
                row_ident = parts[1]
            if len(parts) > 2:
                row_section = parts[2]
            if len(parts) > 3:
                row_type = parts[3]
            if len(parts) > 4:
                row_status = parts[4].upper()
        else:
            row_ident = parts[0]
            if len(parts) > 1:
                d_check = parse_attendance_date(parts[1])
                if d_check:
                    row_date = d_check
                else:
                    row_section = parts[1]
            if len(parts) > 2:
                if not row_date:
                    d_check = parse_attendance_date(parts[2])
                    if d_check:
                        row_date = d_check
                else:
                    row_type = parts[2]
            if len(parts) > 3:
                if not row_type:
                    row_type = parts[3]
                else:
                    row_status = parts[3].upper()
            if len(parts) > 4:
                row_status = parts[4].upper()
                
        if not row_date:
            row_date = default_date
        if not row_type:
            row_type = default_session_type
            
        row_type = row_type.strip().capitalize()
        if row_type.upper() in ("LEC", "LECTURE"):
            row_type = "Lecture"
        elif row_type.upper() in ("LAB", "LABORATORY", "PRACTICAL"):
            row_type = "Lab"
        elif row_type.upper() in ("TUT", "TUTORIAL"):
            row_type = "Tutorial"
            
        row_status = row_status.strip().upper()
        if row_status in ("P", "1", "TRUE", "YES"):
            row_status = "PRESENT"
        elif row_status in ("A", "0", "FALSE", "NO"):
            row_status = "ABSENT"
            
        if not row_ident:
            skipped_count += 1
            continue
            
        clean_ident = re.sub(r"[^a-zA-Z0-9@._-]", "", row_ident).strip()
        if not clean_ident:
            skipped_count += 1
            continue
            
        distinct_dates.add(row_date)
        
        sess_row = conn.execute("""
            SELECT id FROM attendance_sessions
            WHERE course_id = ? AND session_date = ? AND LOWER(session_type) = LOWER(?)
        """, (course_id, row_date, row_type)).fetchone()
        
        if not sess_row:
            conn.execute("""
                INSERT INTO attendance_sessions (
                    course_id, title, session_type, session_date, start_time, end_time, is_active, created_by, created_at
                ) VALUES (?, ?, ?, ?, '09:00', '10:00', 0, ?, ?)
            """, (course_id, f"{row_type} - {row_date}", row_type, row_date, current_u["id"], now_str))
            session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        else:
            session_id = sess_row["id"]
            
        user_row = conn.execute("""
            SELECT id, username, roll_number, display_name, email FROM users
            WHERE LOWER(email) = LOWER(?) OR LOWER(roll_number) = LOWER(?) OR LOWER(username) = LOWER(?)
        """, (clean_ident, clean_ident, clean_ident)).fetchone()
        
        if not user_row:
            if auto_create_users:
                username_part = clean_ident.split("@")[0].lower() if "@" in clean_ident else clean_ident.lower()
                roll_val = username_part.upper()
                email_val = clean_ident.lower() if "@" in clean_ident else f"{username_part}@iitbhilai.ac.in"
                pwd_hash = hash_password(username_part)
                
                try:
                    conn.execute("""
                        INSERT INTO users (username, roll_number, email, password_hash, display_name, role, must_change_password, created_at)
                        VALUES (?, ?, ?, ?, ?, 'student', 1, ?)
                    """, (username_part, roll_val, email_val, pwd_hash, roll_val, now_str))
                    user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    display_name = roll_val
                    roll_number = roll_val
                    created_users_count += 1
                except sqlite3.IntegrityError:
                    user_row = conn.execute("SELECT id, username, roll_number, display_name FROM users WHERE LOWER(username) = LOWER(?)", (username_part,)).fetchone()
                    if user_row:
                        user_id = user_row["id"]
                        display_name = user_row["display_name"]
                        roll_number = user_row["roll_number"] or user_row["username"].upper()
                    else:
                        skipped_count += 1
                        continue
            else:
                skipped_count += 1
                continue
        else:
            user_id = user_row["id"]
            display_name = user_row["display_name"]
            roll_number = user_row["roll_number"] or user_row["username"].upper()
            
        if auto_enroll:
            enr = conn.execute("SELECT 1 FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course_id, user_id)).fetchone()
            if not enr:
                conn.execute("""
                    INSERT OR IGNORE INTO course_enrollments (course_id, user_id, role, enrolled_at)
                    VALUES (?, ?, 'student', ?)
                """, (course_id, user_id, now_str))
                enrolled_count += 1
                
        target_key = f"{user_id}_{row_type.upper()}_{row_date}"
        final_section = row_section if row_section else (course["section"] or "Section A")
        
        existing = conn.execute("SELECT id FROM attendance_logs WHERE attendance_key = ?", (target_key,)).fetchone()
        
        conn.execute("""
            INSERT INTO attendance_logs (
                course_id, session_id, student_id, roll_number, student_name,
                section, session_type, attendance_date, status, method, marked_at, attendance_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'GOOGLE_SHEET_IMPORT', ?, ?)
            ON CONFLICT(attendance_key) DO UPDATE SET
                session_id = excluded.session_id,
                section = excluded.section,
                status = excluded.status,
                method = 'GOOGLE_SHEET_IMPORT',
                marked_at = excluded.marked_at
        """, (
            course_id, session_id, user_id, roll_number, display_name,
            final_section, row_type, row_date, row_status, now_str, target_key
        ))
        
        if existing:
            updated_count += 1
        else:
            imported_count += 1
            
    conn.commit()
    conn.close()
    
    msg_parts = [f"Successfully processed {imported_count + updated_count} attendance records across {len(distinct_dates)} date(s)."]
    if imported_count > 0:
        msg_parts.append(f"{imported_count} newly recorded.")
    if updated_count > 0:
        msg_parts.append(f"{updated_count} updated.")
    if created_users_count > 0:
        msg_parts.append(f"{created_users_count} new student account(s) created.")
    if enrolled_count > 0:
        msg_parts.append(f"{enrolled_count} student(s) enrolled into this course.")
    if skipped_count > 0:
        msg_parts.append(f"{skipped_count} row(s) skipped.")
        
    flash(" ".join(msg_parts), "success" if (imported_count + updated_count) > 0 else "warning")
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


# --- 1-on-1 Chat & Messaging System (Teacher-Student & TA-Student) ---

@app.route("/messages")
@login_required
def messages_view():
    curr_user = get_current_user()
    curr_id = curr_user["id"]
    curr_role = curr_user["role"]
    target_user_id = request.args.get("user_id", type=int)
    course_context_id = request.args.get("course_id", type=int)

    conn = get_db()

    # 1. Fetch distinct conversation partners
    raw_convos = conn.execute("""
        SELECT 
            CASE WHEN sender_id = ? THEN recipient_id ELSE sender_id END as other_user_id,
            MAX(created_at) as last_activity,
            (SELECT message FROM direct_messages m2 
             WHERE (m2.sender_id = ? AND m2.recipient_id = CASE WHEN m.sender_id = ? THEN m.recipient_id ELSE m.sender_id END)
                OR (m2.recipient_id = ? AND m2.sender_id = CASE WHEN m.sender_id = ? THEN m.recipient_id ELSE m.sender_id END)
             ORDER BY m2.id DESC LIMIT 1) as last_message,
            (SELECT COUNT(*) FROM direct_messages m3
             WHERE m3.recipient_id = ? AND m3.sender_id = CASE WHEN m.sender_id = ? THEN m.recipient_id ELSE m.sender_id END AND m3.is_read = 0) as unread_count
        FROM direct_messages m
        WHERE sender_id = ? OR recipient_id = ?
        GROUP BY other_user_id
        ORDER BY last_activity DESC
    """, (curr_id, curr_id, curr_id, curr_id, curr_id, curr_id, curr_id, curr_id, curr_id)).fetchall()

    conversations = []
    for c in raw_convos:
        partner = conn.execute("SELECT id, display_name, roll_number, email, role FROM users WHERE id = ?", (c["other_user_id"],)).fetchone()
        if partner:
            # If current user is student, hide any invalid student contacts from conversation list
            if curr_role == "student" and partner["role"] == "student":
                continue
            conversations.append({
                "partner": partner,
                "last_activity": c["last_activity"],
                "last_message": c["last_message"],
                "unread_count": c["unread_count"]
            })

    # 2. Eligible contacts to start new chat
    eligible_contacts = []
    if curr_role == "student":
        # Students can ONLY message teachers, TAs, or admins
        contacts_query = """
            SELECT DISTINCT u.id, u.display_name, u.email, u.roll_number, u.role, c.code as course_code
            FROM users u
            JOIN courses c ON (u.id = c.teacher_id OR u.id IN (SELECT user_id FROM course_enrollments WHERE course_id = c.id AND role IN ('teacher', 'ta')))
            JOIN course_enrollments ce ON c.id = ce.course_id
            WHERE ce.user_id = ? AND ce.role = 'student' AND c.is_archived = 0
            ORDER BY u.display_name ASC
        """
        eligible_contacts = conn.execute(contacts_query, (curr_id,)).fetchall()
    elif curr_role in ("teacher", "ta"):
        # Instructors/TAs can message any student in their courses + co-instructors
        contacts_query = """
            SELECT DISTINCT u.id, u.display_name, u.email, u.roll_number, u.role, c.code as course_code
            FROM users u
            JOIN course_enrollments ce ON u.id = ce.user_id
            JOIN courses c ON ce.course_id = c.id
            WHERE (c.teacher_id = ? OR c.id IN (SELECT course_id FROM course_enrollments WHERE user_id = ? AND role IN ('teacher', 'ta')))
              AND u.id != ? AND c.is_archived = 0
            ORDER BY u.role ASC, u.display_name ASC
        """
        eligible_contacts = conn.execute(contacts_query, (curr_id, curr_id, curr_id)).fetchall()
    else: # admin
        contacts_query = """
            SELECT id, display_name, email, roll_number, role, '' as course_code
            FROM users WHERE id != ?
            ORDER BY role DESC, display_name ASC
        """
        eligible_contacts = conn.execute(contacts_query, (curr_id,)).fetchall()

    # 3. Active contact & thread messages
    active_contact = None
    thread_messages = []

    if target_user_id and target_user_id != curr_id:
        target_user = conn.execute("SELECT id, display_name, roll_number, email, role FROM users WHERE id = ?", (target_user_id,)).fetchone()
        if target_user:
            # Enforce academic integrity: students cannot message students
            if curr_role == "student" and target_user["role"] == "student":
                flash("Direct messaging between students is strictly prohibited by academic policy.", "danger")
                conn.close()
                return redirect(url_for("messages_view"))
            active_contact = target_user
    elif conversations:
        active_contact = conversations[0]["partner"]

    if active_contact:
        # Mark messages from this contact as read
        conn.execute("""
            UPDATE direct_messages SET is_read = 1
            WHERE recipient_id = ? AND sender_id = ?
        """, (curr_id, active_contact["id"]))
        conn.commit()

        thread_messages = conn.execute("""
            SELECT m.*, s.display_name as sender_name, s.role as sender_role
            FROM direct_messages m
            JOIN users s ON m.sender_id = s.id
            WHERE (m.sender_id = ? AND m.recipient_id = ?)
               OR (m.sender_id = ? AND m.recipient_id = ?)
            ORDER BY m.id ASC
        """, (curr_id, active_contact["id"], active_contact["id"], curr_id)).fetchall()

    conn.close()
    return render_template(
        "messages.html",
        conversations=conversations,
        eligible_contacts=eligible_contacts,
        active_contact=active_contact,
        thread_messages=thread_messages,
        course_context_id=course_context_id
    )


@app.route("/api/messages/<int:other_user_id>")
@login_required
def api_get_messages(other_user_id):
    curr_user = get_current_user()
    curr_id = curr_user["id"]
    curr_role = curr_user["role"]

    conn = get_db()
    other_user = conn.execute("SELECT id, display_name, roll_number, email, role FROM users WHERE id = ?", (other_user_id,)).fetchone()
    if not other_user:
        conn.close()
        return jsonify({"error": "User not found"}), 404

    if curr_role == "student" and other_user["role"] == "student":
        conn.close()
        return jsonify({"error": "Direct messaging between students is strictly prohibited."}), 403

    # Mark as read
    conn.execute("UPDATE direct_messages SET is_read = 1 WHERE recipient_id = ? AND sender_id = ?", (curr_id, other_user_id))
    conn.commit()

    messages = conn.execute("""
        SELECT m.id, m.sender_id, m.recipient_id, m.message, m.is_read, m.created_at,
               s.display_name as sender_name, s.role as sender_role
        FROM direct_messages m
        JOIN users s ON m.sender_id = s.id
        WHERE (m.sender_id = ? AND m.recipient_id = ?)
           OR (m.sender_id = ? AND m.recipient_id = ?)
        ORDER BY m.id ASC
    """, (curr_id, other_user_id, other_user_id, curr_id)).fetchall()
    conn.close()

    return jsonify({
        "success": True,
        "other_user": {
            "id": other_user["id"],
            "display_name": other_user["display_name"],
            "roll_number": other_user["roll_number"],
            "role": other_user["role"]
        },
        "messages": [
            {
                "id": m["id"],
                "sender_id": m["sender_id"],
                "recipient_id": m["recipient_id"],
                "message": m["message"],
                "created_at": m["created_at"],
                "is_mine": (m["sender_id"] == curr_id),
                "sender_name": m["sender_name"],
                "sender_role": m["sender_role"]
            }
            for m in messages
        ]
    })


@app.route("/api/messages/poll")
@login_required
def api_messages_poll():
    """
    Live real-time polling endpoint for auto-updating chat.
    Returns any new messages for the currently open conversation thread (id > after_id),
    updates conversation list status with unread badges, and returns global unread count.
    """
    curr_user = get_current_user()
    curr_id = curr_user["id"]
    curr_role = curr_user["role"]

    active_user_id = request.args.get("active_user_id", type=int)
    after_id = request.args.get("after_id", default=0, type=int)

    conn = get_db()

    new_messages = []
    if active_user_id and active_user_id != curr_id:
        other_user = conn.execute("SELECT id, role FROM users WHERE id = ?", (active_user_id,)).fetchone()
        if other_user and not (curr_role == "student" and other_user["role"] == "student"):
            # Mark incoming unread messages as read in real-time as user views them
            conn.execute("""
                UPDATE direct_messages 
                SET is_read = 1 
                WHERE recipient_id = ? AND sender_id = ? AND is_read = 0
            """, (curr_id, active_user_id))
            conn.commit()

            rows = conn.execute("""
                SELECT m.id, m.sender_id, m.recipient_id, m.message, m.is_read, m.created_at,
                       s.display_name as sender_name, s.role as sender_role
                FROM direct_messages m
                JOIN users s ON m.sender_id = s.id
                WHERE ((m.sender_id = ? AND m.recipient_id = ?)
                    OR (m.sender_id = ? AND m.recipient_id = ?))
                  AND m.id > ?
                ORDER BY m.id ASC
            """, (curr_id, active_user_id, active_user_id, curr_id, after_id)).fetchall()

            for r in rows:
                new_messages.append({
                    "id": r["id"],
                    "sender_id": r["sender_id"],
                    "recipient_id": r["recipient_id"],
                    "message": r["message"],
                    "created_at": r["created_at"],
                    "is_mine": (r["sender_id"] == curr_id),
                    "sender_name": r["sender_name"],
                    "sender_role": r["sender_role"]
                })

    # Fetch updated conversations summary
    raw_convos = conn.execute("""
        SELECT 
            CASE WHEN sender_id = ? THEN recipient_id ELSE sender_id END as other_user_id,
            MAX(created_at) as last_activity,
            (SELECT message FROM direct_messages m2 
             WHERE (m2.sender_id = ? AND m2.recipient_id = CASE WHEN m.sender_id = ? THEN m.recipient_id ELSE m.sender_id END)
                OR (m2.recipient_id = ? AND m2.sender_id = CASE WHEN m.sender_id = ? THEN m.recipient_id ELSE m.sender_id END)
             ORDER BY m2.id DESC LIMIT 1) as last_message,
            (SELECT COUNT(*) FROM direct_messages m3
             WHERE m3.recipient_id = ? AND m3.sender_id = CASE WHEN m.sender_id = ? THEN m.recipient_id ELSE m.sender_id END AND m3.is_read = 0) as unread_count
        FROM direct_messages m
        WHERE sender_id = ? OR recipient_id = ?
        GROUP BY other_user_id
        ORDER BY last_activity DESC
    """, (curr_id, curr_id, curr_id, curr_id, curr_id, curr_id, curr_id, curr_id, curr_id)).fetchall()

    conversations = []
    total_unread = 0
    for c in raw_convos:
        partner = conn.execute("SELECT id, display_name, roll_number, email, role FROM users WHERE id = ?", (c["other_user_id"],)).fetchone()
        if partner:
            if curr_role == "student" and partner["role"] == "student":
                continue
            unr = c["unread_count"] or 0
            total_unread += unr
            conversations.append({
                "partner_id": partner["id"],
                "display_name": partner["display_name"],
                "roll_number": partner["roll_number"] or "",
                "role": partner["role"],
                "last_activity": c["last_activity"],
                "last_message": c["last_message"] or "",
                "unread_count": unr
            })

    conn.close()

    return jsonify({
        "success": True,
        "new_messages": new_messages,
        "conversations": conversations,
        "total_unread": total_unread
    })


@app.route("/api/messages/send", methods=["POST"])
@login_required
def api_send_message():
    curr_user = get_current_user()
    curr_id = curr_user["id"]
    curr_role = curr_user["role"]

    data = request.get_json(silent=True) or request.form
    recipient_id = data.get("recipient_id")
    message = (data.get("message") or "").strip()
    course_id = data.get("course_id")

    if not recipient_id or not message:
        return jsonify({"error": "Recipient and message content are required."}), 400

    try:
        recipient_id = int(recipient_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid recipient ID."}), 400

    if recipient_id == curr_id:
        return jsonify({"error": "You cannot send a message to yourself."}), 400

    conn = get_db()
    recipient = conn.execute("SELECT id, display_name, email, role FROM users WHERE id = ?", (recipient_id,)).fetchone()
    if not recipient:
        conn.close()
        return jsonify({"error": "Recipient not found."}), 404

    # Strict Anti-Cheating Protection: Students CANNOT message other students
    if curr_role == "student" and recipient["role"] == "student":
        conn.close()
        return jsonify({"error": "Direct messaging between students is strictly prohibited by academic policy."}), 403

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c = conn.cursor()
    c.execute("""
        INSERT INTO direct_messages (course_id, sender_id, recipient_id, message, is_read, created_at)
        VALUES (?, ?, ?, ?, 0, ?)
    """, (course_id, curr_id, recipient_id, message, now_str))
    new_id = c.lastrowid
    conn.commit()
    conn.close()

    # Event Notification Email: Send email alert to recipient
    if recipient["email"]:
        snippet = (message[:200] + "...") if len(message) > 200 else message
        sender_role = curr_user["role"].upper()
        send_event_notification_email(
            recipient_emails=[recipient["email"]],
            subject=f"New Message from {curr_user['display_name']} ({sender_role})",
            heading=f"New Direct Message from {curr_user['display_name']}",
            body_text=f"{curr_user['display_name']} ({sender_role}) sent you a message on Hoodle:\n\n\"{snippet}\"",
            action_url=f"/messages?user_id={curr_id}",
            action_text="View & Reply to Message",
            actor_name=curr_user["display_name"],
            actor_role=sender_role
        )

    return jsonify({
        "success": True,
        "message_id": new_id,
        "created_at": now_str,
        "message": message,
        "sender_id": curr_id,
        "recipient_id": recipient_id
    })


@app.route("/api/messages/mark-read/<int:other_user_id>", methods=["POST"])
@login_required
def api_mark_messages_read(other_user_id):
    curr_id = session.get("user_id")
    conn = get_db()
    conn.execute("UPDATE direct_messages SET is_read = 1 WHERE recipient_id = ? AND sender_id = ?", (curr_id, other_user_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


with app.app_context():
    init_db()


if __name__ == "__main__":
    print(f"🚀 ACCLLMS Classroom Portal listening on http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False)
