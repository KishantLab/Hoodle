#!/usr/bin/env python3
"""
Lab Exam Portal
Host: 10.10.14.104
Directory: /data/admin/lab_exam
Developed & Maintained by Advanced Computing & Communications Laboratory (ACCL), IIT Bhilai
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
    abort
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

# Configuration
BASE_DIR = Path(__file__).resolve().parent
SUBMISSIONS_DIR = Path(os.environ.get("SUBMISSION_DIR", BASE_DIR / "submissions"))
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "submissions.db"))
PORT = int(os.environ.get("PORT", 8090))
HOST = os.environ.get("HOST", "0.0.0.0")
MAX_CONTENT_LENGTH = 200 * 1024 * 1024  # 200 MB max upload size
SECRET_KEY = os.environ.get("SECRET_KEY", "accl_lab_exam_portal_secret_key_2026")
UPLOAD_FOLDER = BASE_DIR / "static" / "uploads"

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.config["SUBMISSIONS_DIR"] = SUBMISSIONS_DIR
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# Proxy handling
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Jinja2 newline filter
@app.template_filter("nl2br")
def nl2br_filter(s):
    if not s:
        return ""
    from markupsafe import Markup, escape
    return Markup("<br>".join(escape(s).splitlines()))

# Ensure directories exist
SUBMISSIONS_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

RESERVED_ROUTES = {"login", "logout", "admin", "static", "api", "exam", "favicon.ico"}


def get_db():
    """Get database connection with row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize SQLite database tables and default admin user."""
    conn = get_db()
    cursor = conn.cursor()

    # 1. Teachers / Users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT DEFAULT 'teacher',
            created_at TEXT NOT NULL
        )
    """)

    # 2. Course Exams table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            course_code TEXT NOT NULL,
            course_name TEXT NOT NULL,
            exam_title TEXT NOT NULL,
            teacher_name TEXT NOT NULL,
            created_by_user_id INTEGER,
            instructions TEXT,
            folder_name TEXT NOT NULL,
            allowed_types TEXT DEFAULT 'zip',
            allow_multiple INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1,
            logo_filename TEXT DEFAULT 'iitbhilai_logo.png',
            created_at TEXT NOT NULL
        )
    """)

    # 3. Submissions table (Single latest record per student per exam)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL,
            roll_number TEXT NOT NULL,
            student_name TEXT,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            ip_address TEXT,
            submitted_at TEXT NOT NULL,
            version INTEGER DEFAULT 1,
            is_latest INTEGER DEFAULT 1,
            FOREIGN KEY (exam_id) REFERENCES exams (id),
            UNIQUE(exam_id, roll_number)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sub_exam_roll ON submissions (exam_id, roll_number)")

    # Seed default teacher if no users exist
    cursor.execute("SELECT id FROM users WHERE username = ?", ("kishan",))
    if not cursor.fetchone():
        pwd_hash = generate_password_hash("password123")
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT INTO users (username, password_hash, display_name, role, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, ("kishan", pwd_hash, "Prof. Kishan", "admin", now_str))

    conn.commit()
    conn.close()


init_db()


def sanitize_roll_number(raw_roll):
    """Clean and convert roll number to uppercase alphanumeric string."""
    if not raw_roll:
        return ""
    cleaned = raw_roll.strip().upper()
    cleaned = re.sub(r"[^A-Z0-9_-]", "", cleaned)
    return cleaned


def slugify(text):
    """Convert text to URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[-\s]+", "-", text)


def is_file_allowed(filename, allowed_types_str):
    """Check if file matches allowed extensions configured for the exam."""
    if not allowed_types_str or allowed_types_str.strip().lower() in ("any", "*", "all"):
        return True
    
    allowed = [ext.strip().lower().lstrip(".") for ext in allowed_types_str.split(",") if ext.strip()]
    if not allowed:
        return True
    
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in allowed


def compute_sha256(filepath):
    """Compute SHA-256 hash of a file."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def get_client_ip():
    """Get submitter client IP address, handling proxy headers."""
    if request.headers.get("X-Forwarded-For"):
        return request.headers.get("X-Forwarded-For").split(",")[0].strip()
    if request.headers.get("X-Real-IP"):
        return request.headers.get("X-Real-IP")
    return request.remote_addr or "unknown"


def login_required(f):
    """Decorator to require teacher/admin login."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated_function


def find_exam_by_identifier(identifier):
    """Find exam by slug or course_code (case-insensitive)."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM exams 
        WHERE LOWER(slug) = ? OR LOWER(course_code) = ?
        ORDER BY id DESC LIMIT 1
    """, (identifier.lower(), identifier.lower()))
    exam = cursor.fetchone()
    conn.close()
    return exam


# ==========================================
# PUBLIC STUDENT ROUTES (NO LOGIN REQUIRED)
# ==========================================

@app.route("/", methods=["GET"])
def root_redirect():
    """Redirect to active exam or show portal login."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT slug FROM exams WHERE is_active = 1 ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()

    prefix = request.headers.get("X-Forwarded-Prefix", "").rstrip("/")
    if row:
        return redirect(f"{prefix}/{row['slug']}")
    return redirect(f"{prefix}/login")


# Short URL direct endpoint: e.g. /lab_exam/csl100
@app.route("/<path:identifier>", methods=["GET"])
def short_exam_page(identifier):
    """Render exam page via short URL e.g. /csl100 or /csl100-lab-exam-1."""
    identifier_clean = identifier.strip("/").lower()
    if identifier_clean in RESERVED_ROUTES or "/" in identifier_clean:
        abort(404)

    exam = find_exam_by_identifier(identifier_clean)
    if not exam:
        abort(404, description="Exam submission page not found.")

    return render_template("index.html", exam=exam)


@app.route("/exam/<slug>", methods=["GET"])
def exam_page(slug):
    """Legacy route for exam submission page."""
    exam = find_exam_by_identifier(slug)
    if not exam:
        abort(404, description="Exam submission page not found.")
    return render_template("index.html", exam=exam)


@app.route("/<path:identifier>/status/<roll_number>", methods=["GET"])
@app.route("/exam/<slug>/status/<roll_number>", methods=["GET"])
def check_student_status(roll_number, identifier=None, slug=None):
    """Check if a roll number has already submitted for this specific exam."""
    exam_id_str = identifier or slug
    clean_roll = sanitize_roll_number(roll_number)
    if not clean_roll:
        return jsonify({"success": False, "message": "Invalid roll number"}), 400

    exam = find_exam_by_identifier(exam_id_str)
    if not exam:
        return jsonify({"success": False, "message": "Exam not found"}), 404

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT version, original_filename, file_size, submitted_at, sha256
        FROM submissions
        WHERE exam_id = ? AND roll_number = ?
    """, (exam["id"], clean_roll))
    row = cursor.fetchone()
    conn.close()

    if row:
        return jsonify({
            "success": True,
            "has_submitted": True,
            "roll_number": clean_roll,
            "version": row["version"],
            "filename": row["original_filename"],
            "file_size": row["file_size"],
            "submitted_at": row["submitted_at"],
            "sha256": row["sha256"],
            "allow_multiple": bool(exam["allow_multiple"])
        })
    return jsonify({
        "success": True,
        "has_submitted": False,
        "roll_number": clean_roll
    })


@app.route("/<path:identifier>/submit", methods=["POST"])
@app.route("/exam/<slug>/submit", methods=["POST"])
def submit_exam_file(identifier=None, slug=None):
    """
    Handle direct exam file upload without login.
    Enforces exam-specific rules (allowed file types, single vs multiple submissions).
    Stores only a single latest file per student in the course folder.
    """
    exam_id_str = identifier or slug
    exam = find_exam_by_identifier(exam_id_str)

    if not exam:
        return jsonify({"success": False, "error": "Exam not found."}), 404

    if not exam["is_active"]:
        return jsonify({"success": False, "error": "This exam submission portal is currently closed."}), 403

    raw_roll = request.form.get("roll_number", "")
    roll_number = sanitize_roll_number(raw_roll)

    if not roll_number:
        return jsonify({"success": False, "error": "Please enter a valid Roll Number (alphanumeric characters)."}), 400

    if len(roll_number) < 2 or len(roll_number) > 30:
        return jsonify({"success": False, "error": "Roll number length must be between 2 and 30 characters."}), 400

    conn = get_db()
    cursor = conn.cursor()

    # Check if student already submitted and if multiple submissions are allowed
    cursor.execute("""
        SELECT id, version, file_path FROM submissions WHERE exam_id = ? AND roll_number = ?
    """, (exam["id"], roll_number))
    existing_sub = cursor.fetchone()

    if existing_sub and not exam["allow_multiple"]:
        conn.close()
        return jsonify({
            "success": False,
            "error": f"You have already submitted for {exam['course_code']}. Multiple submissions are not allowed for this exam."
        }), 400

    if "exam_file" not in request.files:
        conn.close()
        return jsonify({"success": False, "error": "No file uploaded. Please select your exam file."}), 400

    file = request.files["exam_file"]
    if not file or file.filename == "":
        conn.close()
        return jsonify({"success": False, "error": "No file selected. Please choose a file to submit."}), 400

    original_filename = secure_filename(file.filename)
    if not original_filename:
        original_filename = f"{roll_number}_submission.zip"

    # Validate file extension against exam's allowed types
    if not is_file_allowed(original_filename, exam["allowed_types"]):
        conn.close()
        allowed_desc = exam["allowed_types"] if exam["allowed_types"] != "any" else "valid format"
        return jsonify({
            "success": False,
            "error": f"File type not supported. Allowed extensions for this exam: {allowed_desc}"
        }), 400

    # Course folder: submissions/<course_folder>/
    course_folder = SUBMISSIONS_DIR / exam["folder_name"]
    course_folder.mkdir(parents=True, exist_ok=True)

    # Determine file extension
    ext = ""
    if "." in original_filename:
        ext = "." + original_filename.rsplit(".", 1)[1].lower()

    # SINGLE FILE STORAGE: <ROLL_NUMBER>.<ext> directly in course folder
    stored_filename = f"{roll_number}{ext}"
    saved_path = course_folder / stored_filename

    # If re-submitting, remove previous file first
    if saved_path.exists():
        try:
            saved_path.unlink()
        except Exception as e:
            app.logger.warning(f"Error removing old submission file: {e}")

    file.save(str(saved_path))
    file_size = saved_path.stat().st_size

    if file_size == 0:
        saved_path.unlink(missing_ok=True)
        conn.close()
        return jsonify({"success": False, "error": "Uploaded file is empty (0 bytes). Please upload a valid file."}), 400

    # Compute SHA-256
    file_sha256 = compute_sha256(saved_path)
    client_ip = get_client_ip()
    display_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    version = 1
    if existing_sub:
        version = existing_sub["version"] + 1
        cursor.execute("""
            UPDATE submissions SET
                original_filename = ?,
                stored_filename = ?,
                file_path = ?,
                file_size = ?,
                sha256 = ?,
                ip_address = ?,
                submitted_at = ?,
                version = ?
            WHERE id = ?
        """, (
            original_filename, stored_filename, str(saved_path),
            file_size, file_sha256, client_ip, display_time,
            version, existing_sub["id"]
        ))
    else:
        cursor.execute("""
            INSERT INTO submissions (
                exam_id, roll_number, original_filename, stored_filename,
                file_path, file_size, sha256, ip_address, submitted_at, version, is_latest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)
        """, (
            exam["id"], roll_number, original_filename, stored_filename,
            str(saved_path), file_size, file_sha256, client_ip, display_time
        ))

    conn.commit()
    conn.close()

    # Format file size
    if file_size < 1024:
        size_str = f"{file_size} B"
    elif file_size < 1024 * 1024:
        size_str = f"{file_size / 1024:.1f} KB"
    else:
        size_str = f"{file_size / (1024 * 1024):.2f} MB"

    return jsonify({
        "success": True,
        "message": "Submitted",
        "details": {
            "roll_number": roll_number,
            "version": version,
            "filename": original_filename,
            "stored_filename": stored_filename,
            "file_size": size_str,
            "file_size_bytes": file_size,
            "submitted_at": display_time,
            "sha256": file_sha256,
            "is_update": version > 1,
            "course_code": exam["course_code"],
            "exam_title": exam["exam_title"]
        }
    })


# ==========================================
# TEACHER AUTHENTICATION & TEACHER MANAGEMENT
# ==========================================

@app.route("/login", methods=["GET", "POST"])
def login():
    """Teacher Login page."""
    if session.get("logged_in"):
        return redirect(url_for("admin_dashboard"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
        user = cursor.fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["logged_in"] = True
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["display_name"] = user["display_name"]
            session["role"] = user["role"]
            next_page = request.args.get("next")
            if next_page and not next_page.startswith("//"):
                return redirect(next_page)
            return redirect(url_for("admin_dashboard"))
        else:
            error = "Invalid username or password."

    return render_template("login.html", error=error)


@app.route("/logout", methods=["GET"])
def logout():
    """Clear session and log out."""
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin/teachers/add", methods=["POST"])
@login_required
def add_teacher():
    """Allow an instructor to register a new teacher account."""
    username = request.form.get("username", "").strip().lower()
    display_name = request.form.get("display_name", "").strip()
    password = request.form.get("password", "").strip()

    if not username or not password or not display_name:
        flash("All fields are required to add a teacher.", "error")
        return redirect(url_for("admin_dashboard"))

    if len(password) < 6:
        flash("Password must be at least 6 characters.", "error")
        return redirect(url_for("admin_dashboard"))

    # Clean username
    clean_username = re.sub(r"[^a-z0-9_-]", "", username)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ?", (clean_username,))
    if cursor.fetchone():
        conn.close()
        flash(f"Username '{clean_username}' already exists. Please choose a different username.", "error")
        return redirect(url_for("admin_dashboard"))

    pwd_hash = generate_password_hash(password)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO users (username, password_hash, display_name, role, created_at)
        VALUES (?, ?, ?, 'teacher', ?)
    """, (clean_username, pwd_hash, display_name, now_str))
    conn.commit()
    conn.close()

    flash(f"Teacher account '{clean_username}' ({display_name}) created successfully!", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/change-password", methods=["POST"])
@login_required
def change_password():
    """Change teacher's password."""
    old_password = request.form.get("old_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not new_password or len(new_password) < 6:
        flash("New password must be at least 6 characters long.", "error")
        return redirect(url_for("admin_dashboard"))

    if new_password != confirm_password:
        flash("New passwords do not match.", "error")
        return redirect(url_for("admin_dashboard"))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT password_hash FROM users WHERE id = ?", (session["user_id"],))
    user = cursor.fetchone()

    if not user or not check_password_hash(user["password_hash"], old_password):
        conn.close()
        flash("Incorrect current password.", "error")
        return redirect(url_for("admin_dashboard"))

    new_hash = generate_password_hash(new_password)
    cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, session["user_id"]))
    conn.commit()
    conn.close()

    flash("Password updated successfully!", "success")
    return redirect(url_for("admin_dashboard"))


# ==========================================
# TEACHER / ADMIN DASHBOARD & EXAM MANAGEMENT
# ==========================================

@app.route("/admin", methods=["GET"])
@login_required
def admin_dashboard():
    """
    Teacher dashboard:
    - View 1: Clean Course Overview grid (when ?exam is not specified).
    - View 2: Detailed Course View with submissions and downloads (when ?exam=<slug>).
    """
    conn = get_db()
    cursor = conn.cursor()

    # Get all exams with submission counts
    cursor.execute("""
        SELECT e.*, 
               (SELECT COUNT(*) FROM submissions s WHERE s.exam_id = e.id) AS submission_count
        FROM exams e
        ORDER BY e.id DESC
    """)
    exams = [dict(row) for row in cursor.fetchall()]

    # Get all teachers for management modal
    cursor.execute("SELECT id, username, display_name, role, created_at FROM users ORDER BY id ASC")
    teachers = [dict(row) for row in cursor.fetchall()]

    # Check if a specific course/exam is opened
    selected_slug = request.args.get("exam")
    selected_exam = None
    submissions = []

    if selected_slug:
        for ex in exams:
            if ex["slug"] == selected_slug or ex["course_code"].lower() == selected_slug.lower():
                selected_exam = ex
                break

        if selected_exam:
            cursor.execute("""
                SELECT * FROM submissions
                WHERE exam_id = ?
                ORDER BY roll_number ASC
            """, (selected_exam["id"],))
            submissions = [dict(row) for row in cursor.fetchall()]

    conn.close()

    return render_template(
        "admin.html",
        exams=exams,
        teachers=teachers,
        current_exam=selected_exam,
        submissions=submissions,
        username=session.get("username"),
        display_name=session.get("display_name")
    )


@app.route("/admin/exams/create", methods=["POST"])
@login_required
def create_exam():
    """Create a new course exam submission page."""
    course_code = request.form.get("course_code", "").strip().upper()
    course_name = request.form.get("course_name", "").strip()
    exam_title = request.form.get("exam_title", "").strip()
    teacher_name = request.form.get("teacher_name", "").strip() or session.get("display_name", "Course Instructor")
    instructions = request.form.get("instructions", "").strip()
    allowed_types = request.form.get("allowed_types", "zip").strip().lower()
    allow_multiple = 1 if request.form.get("allow_multiple") == "1" else 0
    short_code = request.form.get("short_code", "").strip().lower()

    if not course_code or not exam_title:
        flash("Course Code and Exam Title are required.", "error")
        return redirect(url_for("admin_dashboard"))

    # Determine slug (use short_code if provided, else course_code)
    if short_code:
        slug = slugify(short_code)
    else:
        slug = slugify(course_code)

    folder_name = re.sub(r"[^\w-]", "_", f"{course_code}_{exam_title}")

    # Handle custom lab logo upload (if provided)
    logo_filename = "iitbhilai_logo.png"
    if "lab_logo" in request.files:
        logo_file = request.files["lab_logo"]
        if logo_file and logo_file.filename != "":
            safe_logo = secure_filename(logo_file.filename)
            ext = safe_logo.rsplit(".", 1)[1].lower() if "." in safe_logo else "png"
            logo_filename = f"logo_{slug}_{int(time.time())}.{ext}"
            logo_path = BASE_DIR / "static" / "uploads" / logo_filename
            logo_file.save(str(logo_path))

    conn = get_db()
    cursor = conn.cursor()

    # Ensure unique slug
    cursor.execute("SELECT id FROM exams WHERE slug = ?", (slug,))
    if cursor.fetchone():
        slug = f"{slug}-{slugify(exam_title)}"
        cursor.execute("SELECT id FROM exams WHERE slug = ?", (slug,))
        if cursor.fetchone():
            slug = f"{slug}-{int(time.time()) % 1000}"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO exams (
            slug, course_code, course_name, exam_title, teacher_name,
            created_by_user_id, instructions, folder_name, allowed_types,
            allow_multiple, is_active, logo_filename, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
    """, (
        slug, course_code, course_name, exam_title, teacher_name,
        session.get("user_id"), instructions, folder_name, allowed_types,
        allow_multiple, logo_filename, now_str
    ))
    conn.commit()
    conn.close()

    # Create server directory for this course
    course_dir = SUBMISSIONS_DIR / folder_name
    course_dir.mkdir(parents=True, exist_ok=True)

    flash(f"Course exam page created for {course_code} - {exam_title}! Short link: /{slug}", "success")
    return redirect(url_for("admin_dashboard", exam=slug))


@app.route("/admin/exams/<int:exam_id>/toggle-status", methods=["POST"])
@login_required
def toggle_exam_status(exam_id):
    """Toggle exam active / closed."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT is_active, slug FROM exams WHERE id = ?", (exam_id,))
    exam = cursor.fetchone()
    if exam:
        new_status = 0 if exam["is_active"] else 1
        cursor.execute("UPDATE exams SET is_active = ? WHERE id = ?", (new_status, exam_id))
        conn.commit()
        flash(f"Exam status updated to {'Active' if new_status else 'Closed'}.", "success")
        conn.close()
        return redirect(url_for("admin_dashboard", exam=exam["slug"]))
    conn.close()
    return redirect(url_for("admin_dashboard"))


# ==========================================
# DOWNLOAD ROUTES (BULK AND SINGLE)
# ==========================================

@app.route("/admin/exam/<int:exam_id>/download-all", methods=["GET"])
@login_required
def download_exam_all(exam_id):
    """
    Bulk download all submissions for an exam as a single ZIP archive.
    Uses relative routing to prevent 404 behind proxy.
    """
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM exams WHERE id = ?", (exam_id,))
    exam = cursor.fetchone()

    if not exam:
        conn.close()
        abort(404, description="Exam not found.")

    cursor.execute("""
        SELECT roll_number, original_filename, stored_filename, file_path
        FROM submissions
        WHERE exam_id = ?
        ORDER BY roll_number ASC
    """, (exam_id,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        flash("No submissions found for this exam yet.", "info")
        return redirect(url_for("admin_dashboard", exam=exam["slug"]))

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_STORED) as zf:
        for r in rows:
            path = Path(r["file_path"])
            if path.exists():
                arcname = r["stored_filename"]
                zf.write(path, arcname=arcname)

    memory_file.seek(0)
    zip_name = f"{exam['course_code']}_{slugify(exam['exam_title'])}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(
        memory_file,
        mimetype="application/zip",
        as_attachment=True,
        download_name=zip_name
    )


@app.route("/admin/download-file/<int:sub_id>", methods=["GET"])
@login_required
def download_single_file(sub_id):
    """Download a single student's submitted file directly."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT s.*, e.course_code, e.exam_title
        FROM submissions s
        JOIN exams e ON s.exam_id = e.id
        WHERE s.id = ?
    """, (sub_id,))
    sub = cursor.fetchone()
    conn.close()

    if not sub:
        abort(404, description="Submission file not found.")

    file_path = Path(sub["file_path"])
    if not file_path.exists():
        abort(404, description="File does not exist on disk.")

    return send_file(
        file_path,
        as_attachment=True,
        download_name=sub["stored_filename"]
    )


if __name__ == "__main__":
    print(f"Starting Lab Exam Portal on {HOST}:{PORT}...")
    app.run(host=HOST, port=PORT, debug=False)
