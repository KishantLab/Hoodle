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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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

    conn.commit()

    # Pre-seed initial default accounts if not existing
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Superadmin
    c.execute("SELECT id FROM users WHERE LOWER(username) = 'admin'")
    if not c.fetchone():
        pwd = generate_password_hash("admin@accl")
        c.execute("""
            INSERT INTO users (username, roll_number, email, password_hash, display_name, role, created_at)
            VALUES ('admin', 'ADMIN', 'admin@iitbhilai.ac.in', ?, 'LMS Administrator', 'admin', ?)
        """, (pwd, now_str))

    # Teacher Kishan
    c.execute("SELECT id FROM users WHERE LOWER(username) = 'kishan'")
    t_user = c.fetchone()
    if not t_user:
        pwd = generate_password_hash("password123")
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
        pwd = generate_password_hash("student123")
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
            flash("Please sign in to proceed.", "warning")
            return redirect(url_for("login", next=request.path))
        role = session.get("role")
        if role not in ("teacher", "admin"):
            flash("Access restricted to faculty and instructors.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
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
    conn.close()

    if not row:
        return None

    now = datetime.now()
    st = parse_iso_datetime(row["start_time"])
    et = parse_iso_datetime(row["end_time"])

    # If exam mode is explicitly toggled ON, or if we are within the start-end window
    if row["is_exam_mode"] == 1:
        return dict(row)
    if st and et and (st <= now <= et):
        return dict(row)

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
        "exam_view", "exam_submit", "exam_receipt", "logout", "static", "login"
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

        pwd_hash = generate_password_hash(password)
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

    existing = conn.execute("""
        SELECT * FROM course_enrollments WHERE course_id = ? AND user_id = ?
    """, (course["id"], session["user_id"])).fetchone()

    if existing:
        conn.close()
        flash(f"You are already enrolled in {course['code']} - {course['title']}.", "info")
        return redirect(url_for("course_stream", course_id=course["id"]))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO course_enrollments (course_id, user_id, role, enrolled_at)
        VALUES (?, ?, 'student', ?)
    """, (course["id"], session["user_id"], now_str))
    conn.commit()
    conn.close()

    flash(f"Successfully joined {course['code']}: {course['title']}!", "success")
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

        for s in all_submissions:
            if s["submission_id"]:
                if s["status"] == "graded":
                    stats["graded"] += 1
                else:
                    stats["turned_in"] += 1
            else:
                stats["assigned"] += 1

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

    if cw["end_time"]:
        et = parse_iso_datetime(cw["end_time"])
        if et and now > et:
            if cw["allow_late"] == 0:
                conn.close()
                flash("Exam submission cutoff has expired. Submissions are closed.", "danger")
                return redirect(url_for("exam_view", course_id=course_id, coursework_id=coursework_id))
            is_late = 1
            late_minutes = int((now - et).total_seconds() / 60)

    existing = conn.execute("SELECT * FROM submissions WHERE coursework_id = ? AND student_id = ?", (coursework_id, user_id)).fetchone()
    if existing and cw["allow_multiple"] == 0:
        conn.close()
        flash("Single submission policy: You have already submitted your exam.", "warning")
        return redirect(url_for("exam_view", course_id=course_id, coursework_id=coursework_id))

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

    teachers = conn.execute("""
        SELECT u.id, u.display_name, u.email, u.role
        FROM course_enrollments ce
        JOIN users u ON ce.user_id = u.id
        WHERE ce.course_id = ? AND ce.role IN ('teacher', 'ta')
        ORDER BY u.display_name ASC
    """, (course_id,)).fetchall()

    students = conn.execute("""
        SELECT u.id, u.display_name, u.roll_number, u.email, ce.enrolled_at,
               (SELECT COUNT(*) FROM submissions s
                JOIN coursework cw ON s.coursework_id = cw.id
                WHERE cw.course_id = ? AND s.student_id = u.id) as submissions_count
        FROM course_enrollments ce
        JOIN users u ON ce.user_id = u.id
        WHERE ce.course_id = ? AND ce.role = 'student'
        ORDER BY u.roll_number ASC, u.display_name ASC
    """, (course_id, course_id)).fetchall()

    conn.close()
    return render_template(
        "course_people.html",
        course=course,
        teachers=teachers,
        students=students,
        active_tab="people"
    )


@app.route("/courses/<int:course_id>/people/remove/<int:target_user_id>", methods=["POST"])
@teacher_required
def remove_student(course_id, target_user_id):
    conn = get_db()
    conn.execute("DELETE FROM course_enrollments WHERE course_id = ? AND user_id = ?", (course_id, target_user_id))
    conn.commit()
    conn.close()
    flash("Student removed from course roster.", "info")
    return redirect(url_for("course_people", course_id=course_id))


# --- Tab 4: Grades ---

@app.route("/courses/<int:course_id>/grades")
@teacher_required
def course_grades(course_id):
    course = get_course_or_404(course_id)
    conn = get_db()

    coursework_list = conn.execute("""
        SELECT id, title, type, points, due_date
        FROM coursework
        WHERE course_id = ? AND type != 'material'
        ORDER BY created_at ASC
    """, (course_id,)).fetchall()

    students = conn.execute("""
        SELECT u.id, u.display_name, u.roll_number, u.email
        FROM course_enrollments ce
        JOIN users u ON ce.user_id = u.id
        WHERE ce.course_id = ? AND ce.role = 'student'
        ORDER BY u.roll_number ASC
    """, (course_id,)).fetchall()

    submissions_raw = conn.execute("""
        SELECT s.student_id, s.coursework_id, s.grade, s.status, s.is_late, s.id as submission_id
        FROM submissions s
        JOIN coursework cw ON s.coursework_id = cw.id
        WHERE cw.course_id = ?
    """, (course_id,)).fetchall()

    sub_map = {}
    for r in submissions_raw:
        sub_map[(r["student_id"], r["coursework_id"])] = r

    conn.close()
    return render_template(
        "course_grades.html",
        course=course,
        coursework_list=coursework_list,
        students=students,
        sub_map=sub_map,
        active_tab="grades"
    )


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
    conn = get_db()

    cw_list = conn.execute("""
        SELECT id, title, points FROM coursework WHERE course_id = ? AND type != 'material' ORDER BY created_at ASC
    """, (course_id,)).fetchall()

    students = conn.execute("""
        SELECT u.id, u.roll_number, u.display_name, u.email
        FROM course_enrollments ce
        JOIN users u ON ce.user_id = u.id
        WHERE ce.course_id = ? AND ce.role = 'student'
        ORDER BY u.roll_number ASC
    """, (course_id,)).fetchall()

    sub_raw = conn.execute("""
        SELECT s.student_id, s.coursework_id, s.grade, s.status, s.is_late
        FROM submissions s
        JOIN coursework cw ON s.coursework_id = cw.id
        WHERE cw.course_id = ?
    """, (course_id,)).fetchall()
    conn.close()

    sub_map = {(r["student_id"], r["coursework_id"]): r for r in sub_raw}

    output = io.StringIO()
    header = ["Roll Number", "Student Name", "Email"]
    for cw in cw_list:
        header.append(f"{cw['title']} (Max {cw['points']})")
    output.write(",".join([f'"{h}"' for h in header]) + "\n")

    for s in students:
        row = [s["roll_number"] or "", s["display_name"], s["email"] or ""]
        for cw in cw_list:
            sub = sub_map.get((s["id"], cw["id"]))
            if sub and sub["grade"] is not None:
                row.append(str(sub["grade"]))
            elif sub:
                row.append("Turned In")
            else:
                row.append("Missing")
        output.write(",".join([f'"{c}"' for c in row]) + "\n")

    output.seek(0)
    clean_code = re.sub(r'[^a-zA-Z0-9_-]', '_', course["code"])
    filename = f"{clean_code}_gradebook_{datetime.now().strftime('%Y%m%d')}.csv"

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


# --- File Downloads ---

@app.route("/download/attachment/<int:att_id>")
@login_required
def download_attachment(att_id):
    conn = get_db()
    att = conn.execute("SELECT * FROM coursework_attachments WHERE id = ?", (att_id,)).fetchone()
    conn.close()
    if not att or not os.path.exists(att["file_path"]):
        abort(404, "Attachment not found")
    return send_file(att["file_path"], as_attachment=True, download_name=att["original_filename"])


@app.route("/download/submission/<int:sub_id>")
@login_required
def download_submission(sub_id):
    conn = get_db()
    sub = conn.execute("SELECT * FROM submissions WHERE id = ?", (sub_id,)).fetchone()
    conn.close()
    if not sub or not os.path.exists(sub["file_path"]):
        abort(404, "Submission file not found")

    if session.get("role") not in ("teacher", "admin") and sub["student_id"] != session["user_id"]:
        abort(403, "Unauthorized access to submission")

    return send_file(sub["file_path"], as_attachment=True, download_name=sub["original_filename"])


# --- Admin Panel ---

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
@admin_required
def admin_change_role(user_id):
    new_role = request.form.get("role", "student")
    if new_role not in ("student", "teacher", "admin"):
        flash("Invalid role.", "danger")
        return redirect(url_for("admin_users"))

    conn = get_db()
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    conn.commit()
    conn.close()
    flash("User role updated successfully.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def admin_reset_password(user_id):
    new_pwd = request.form.get("new_password", "").strip()
    if not new_pwd or len(new_pwd) < 6:
        flash("New password must be at least 6 characters.", "danger")
        return redirect(url_for("admin_users"))

    pwd_hash = generate_password_hash(new_pwd)
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


with app.app_context():
    init_db()


if __name__ == "__main__":
    print(f"🚀 ACCLLMS Classroom Portal listening on http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False)
