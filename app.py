#!/usr/bin/env python3
"""
Lab Exam File Submission Portal
Host: 10.10.14.104
Directory: /data/admin/lab_exam
"""

import os
import re
import sys
import time
import shutil
import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path
from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    send_from_directory,
    send_file,
    abort
)
from werkzeug.utils import secure_filename

# Configuration
BASE_DIR = Path(__file__).resolve().parent
SUBMISSIONS_DIR = Path(os.environ.get("SUBMISSION_DIR", BASE_DIR / "submissions"))
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "submissions.db"))
PORT = int(os.environ.get("PORT", 8090))
HOST = os.environ.get("HOST", "0.0.0.0")
MAX_CONTENT_LENGTH = 150 * 1024 * 1024  # 150 MB max upload size
ADMIN_KEY = os.environ.get("ADMIN_KEY", "accl_exam_admin_2026")

# Allowed extensions (zip is primary, also allow common code/archive formats)
ALLOWED_EXTENSIONS = {
    "zip", "tar", "gz", "tgz", "7z", "rar",
    "c", "cpp", "h", "hpp", "py", "java", "sh", "txt", "pdf"
}

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.config["SUBMISSIONS_DIR"] = SUBMISSIONS_DIR

# Ensure submission directory exists
SUBMISSIONS_DIR.mkdir(parents=True, exist_ok=True)


def init_db():
    """Initialize SQLite database for submission logs."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            roll_number TEXT NOT NULL,
            version INTEGER NOT NULL,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            ip_address TEXT,
            submitted_at TEXT NOT NULL,
            is_latest INTEGER DEFAULT 1
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_roll ON submissions (roll_number)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_latest ON submissions (is_latest)")
    conn.commit()
    conn.close()


init_db()


def sanitize_roll_number(raw_roll):
    """
    Clean and convert roll number to uppercase alphanumeric string.
    Only alphanumeric characters, dashes, and underscores allowed.
    """
    if not raw_roll:
        return ""
    cleaned = raw_roll.strip().upper()
    cleaned = re.sub(r"[^A-Z0-9_-]", "", cleaned)
    return cleaned


def is_allowed_file(filename):
    """Check if file extension is allowed."""
    if "." not in filename:
        return True
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS


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


@app.route("/", methods=["GET"])
def index():
    """Render the student submission page."""
    return render_template("index.html")


@app.route("/api/status/<roll_number>", methods=["GET"])
def check_status(roll_number):
    """Check if a roll number has already submitted and return latest submission metadata."""
    clean_roll = sanitize_roll_number(roll_number)
    if not clean_roll:
        return jsonify({"success": False, "message": "Invalid roll number"}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT version, original_filename, file_size, submitted_at, sha256
        FROM submissions
        WHERE roll_number = ? AND is_latest = 1
        ORDER BY id DESC LIMIT 1
    """, (clean_roll,))
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
            "sha256": row["sha256"]
        })
    else:
        return jsonify({
            "success": True,
            "has_submitted": False,
            "roll_number": clean_roll
        })


@app.route("/submit", methods=["POST"])
def submit_file():
    """
    Handle exam file submission without login.
    Multiple submissions allowed; latest file is recorded and retained.
    """
    raw_roll = request.form.get("roll_number", "")
    roll_number = sanitize_roll_number(raw_roll)

    if not roll_number:
        return jsonify({
            "success": False,
            "error": "Please enter a valid Roll Number (alphanumeric characters)."
        }), 400

    if len(roll_number) < 2 or len(roll_number) > 30:
        return jsonify({
            "success": False,
            "error": "Roll number length must be between 2 and 30 characters."
        }), 400

    if "exam_file" not in request.files:
        return jsonify({
            "success": False,
            "error": "No file uploaded. Please select your exam .zip file."
        }), 400

    file = request.files["exam_file"]
    if not file or file.filename == "":
        return jsonify({
            "success": False,
            "error": "No file selected. Please choose a file to submit."
        }), 400

    original_filename = secure_filename(file.filename)
    if not original_filename:
        original_filename = f"{roll_number}_submission.zip"

    if not is_allowed_file(original_filename):
        return jsonify({
            "success": False,
            "error": "File type not supported. Please upload a .zip file (or supported archive/code file)."
        }), 400

    # Student specific submission directory
    student_dir = SUBMISSIONS_DIR / roll_number
    student_dir.mkdir(parents=True, exist_ok=True)

    # Determine submission version
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*), MAX(version) FROM submissions WHERE roll_number = ?
    """, (roll_number,))
    count, max_v = cursor.fetchone()
    version = (max_v or 0) + 1

    # Timestamp
    now = datetime.now()
    timestamp_str = now.strftime("%Y%m%d_%H%M%S")
    display_time = now.strftime("%Y-%m-%d %H:%M:%S")

    # Extension
    ext = ""
    if "." in original_filename:
        ext = "." + original_filename.rsplit(".", 1)[1]

    # Stored filename: <ROLL>_v<VERSION>_<TIMESTAMP>_<SAFE_NAME>
    stored_filename = f"{roll_number}_v{version}_{timestamp_str}_{original_filename}"
    saved_path = student_dir / stored_filename

    # Save file
    file.save(str(saved_path))
    file_size = saved_path.stat().st_size

    if file_size == 0:
        saved_path.unlink(missing_ok=True)
        conn.close()
        return jsonify({
            "success": False,
            "error": "Uploaded file is empty (0 bytes). Please upload your valid exam file."
        }), 400

    # Compute SHA256 checksum
    file_sha256 = compute_sha256(saved_path)

    # Update latest file copy in student's directory: <ROLL>_latest<ext>
    latest_filename = f"{roll_number}_latest{ext}"
    latest_path = student_dir / latest_filename
    try:
        shutil.copy2(str(saved_path), str(latest_path))
    except Exception as e:
        app.logger.warning(f"Could not copy latest file: {e}")

    # Mark previous submissions for this roll number as not latest
    cursor.execute("""
        UPDATE submissions SET is_latest = 0 WHERE roll_number = ?
    """, (roll_number,))

    # Insert new submission record
    client_ip = get_client_ip()
    cursor.execute("""
        INSERT INTO submissions (
            roll_number, version, original_filename, stored_filename,
            file_path, file_size, sha256, ip_address, submitted_at, is_latest
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
    """, (
        roll_number, version, original_filename, stored_filename,
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
            "latest_notice": "Latest submission recorded successfully" if version > 1 else "First submission recorded"
        }
    })


@app.route("/admin", methods=["GET"])
def admin_dashboard():
    """
    Instructor / Admin view listing all submissions and offering latest-files export.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT roll_number, version, original_filename, file_size, submitted_at, sha256, ip_address
        FROM submissions
        WHERE is_latest = 1
        ORDER BY roll_number ASC
    """)
    latest_submissions = [dict(row) for row in cursor.fetchall()]

    cursor.execute("SELECT COUNT(*), COUNT(DISTINCT roll_number) FROM submissions")
    total_submissions, unique_students = cursor.fetchone()
    conn.close()

    return render_template(
        "admin.html",
        submissions=latest_submissions,
        total_count=total_submissions,
        unique_students=unique_students
    )


@app.route("/admin/download-latest", methods=["GET"])
def download_all_latest():
    """
    Package all students' latest submitted files into a single ZIP archive for grading.
    """
    import zipfile
    import io

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT roll_number, original_filename, file_path, version
        FROM submissions
        WHERE is_latest = 1
        ORDER BY roll_number ASC
    """)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return "No submissions found to export.", 404

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in rows:
            path = Path(r["file_path"])
            if path.exists():
                ext = path.suffix
                arcname = f"{r['roll_number']}_latest{ext}"
                zf.write(path, arcname=arcname)

    memory_file.seek(0)
    zip_name = f"lab_exam_all_latest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(
        memory_file,
        mimetype="application/zip",
        as_attachment=True,
        download_name=zip_name
    )


if __name__ == "__main__":
    print(f"Starting Lab Exam Submission Portal on {HOST}:{PORT}...")
    app.run(host=HOST, port=PORT, debug=False)
