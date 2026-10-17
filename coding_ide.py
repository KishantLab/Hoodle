"""
coding_ide.py - VS Code Monaco Cloud IDE & Autograder Blueprint for Hoodle LMS
Features:
- Multi-language support: CUDA (nvcc 12.1), C (gcc), C++ (g++), Python 3, Java.
- Course-level enablement: strictly checks enable_cloud_ide and enable_student_lockers.
- Student Locker isolation with quota limits (200MB default, up to 500MB by teacher, >500MB by admin).
- Sandboxed execution inside bwrap with 1GB GPU VRAM cap (NVIDIA MPS) & dual A6000 load scheduler.
- Autograder with code rubrics (kernel checks, cleanup checks, float precision, banned libraries).
- Smart Problem Importer: Upload assignment documents & test suites via PDF or JSON.
- Teacher Studio: Configure problems, starter code, test cases, and quotas.
"""

import os
import json
import sqlite3
from pathlib import Path
from datetime import datetime
from functools import wraps

from plagiarism_detector import calculate_similarity_matrix
from pip_manager import (list_course_packages, list_system_packages, get_popular_packages_catalog, STANDARD_MODULES, start_install_job, get_job_status, uninstall_course_package)
from terminal_server import create_terminal_session, write_to_terminal, read_from_terminal, resize_terminal, close_terminal_session


from flask import Blueprint, render_template, request, jsonify, session, abort, redirect, url_for, Response
from runner import execute_in_sandbox, evaluate_autograder, get_locker_usage, DEFAULT_LOCKER_QUOTA, MAX_TEACHER_QUOTA
from problem_importer import parse_uploaded_problem_file, get_sample_problem_json_template, get_sample_problem_txt_template

coding_ide_bp = Blueprint("coding_ide", __name__)
LOCKERS_BASE_DIR = Path("/data/admin/ACCLLMS/storage/lockers")
LOCKERS_BASE_DIR.mkdir(parents=True, exist_ok=True)

try:
    from db_adapter import get_db, execute_db_write_with_retry
except ImportError:
    DB_PATH = Path("/data/admin/ACCLLMS/accl_lms.db")
    def get_db(read_only=False):
        conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn


def course_ide_access_required(f):
    @wraps(f)
    def decorated_function(course_id, *args, **kwargs):
        user_id = session.get("user_id")
        if not user_id:
            return redirect("/lms/login")

        conn = get_db()
        course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
        if not course:
            conn.close()
            abort(404, "Course not found.")

        # Check enrollment or ownership or admin
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        is_admin = user and user["role"] == "admin"
        is_teacher = course["teacher_id"] == user_id or is_admin

        enrollment = conn.execute(
            "SELECT * FROM course_enrollments WHERE course_id = ? AND user_id = ?",
            (course_id, user_id)
        ).fetchone()

        if not (is_teacher or enrollment):
            conn.close()
            abort(403, "You are not enrolled in this course.")

        # Check if Cloud IDE is enabled for this course
        if not course["enable_cloud_ide"] and not is_admin:
            conn.close()
            abort(403, "Cloud IDE is not enabled for this course. Your instructor can request it from Admin in Course Settings.")

        conn.close()
        return f(course_id, *args, **kwargs)
    return decorated_function


def get_student_locker(user_id: int) -> Path:
    p = LOCKERS_BASE_DIR / f"user_{user_id}"
    p.mkdir(parents=True, exist_ok=True)
    return p


# =====================================================================
# 1. MAIN CLOUD IDE INTERFACE
# =====================================================================

@coding_ide_bp.route("/courses/<int:course_id>/ide")
@course_ide_access_required
def open_ide(course_id):
    user_id = session["user_id"]
    conn = get_db()
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    is_admin = (user["role"] == "admin")
    is_teacher = (course["teacher_id"] == user_id) or is_admin

    assignment_id = request.args.get("assignment_id", type=int)
    assignment = None
    if assignment_id:
        if is_teacher:
            assignment = conn.execute(
                "SELECT * FROM coding_assignments WHERE id = ? AND course_id = ?",
                (assignment_id, course_id)
            ).fetchone()
        else:
            assignment = conn.execute(
                "SELECT * FROM coding_assignments WHERE id = ? AND course_id = ? AND (is_published = 1 OR is_published IS NULL)",
                (assignment_id, course_id)
            ).fetchone()

    # Default problem if none assigned
    if assignment:
        problem = {
            "id": assignment["id"],
            "title": assignment["title"],
            "description": assignment["description"],
            "starter_code": assignment["starter_code"],
            "language": assignment["language"],
            "test_cases": json.loads(assignment["test_cases_json"] or "[]"),
            "code_checks": json.loads(assignment["code_checks_json"] or "{}"),
            "student_quota_mb": assignment["student_quota_mb"] or 200,
            "is_exam": assignment["is_exam"] or 0
        }
    else:
        problem = {
            "id": 0,
            "title": f"{course['code']} - Accelerated Computing Workspace",
            "description": "General purpose Cloud IDE workspace. Use this environment to compile CUDA, C, C++, Python, and Java programs.",
            "starter_code": "#include <stdio.h>\n#include <cuda_runtime.h>\n\nint main() {\n    printf(\"Hello from Cloud IDE on GPU cluster!\\n\");\n    return 0;\n}\n",
            "language": "cuda",
            "test_cases": [],
            "code_checks": {
                "require_cuda_kernel": False,
                "cuda_kernel_points": 0,
                "check_memory_cleanup": False,
                "cleanup_points": 0,
                "float_tolerance": 0.001,
                "disallowed_keywords": []
            },
            "student_quota_mb": 200,
            "is_exam": 0
        }

    locker_dir = get_student_locker(user_id)
    # Ensure at least one starter file exists in locker
    default_src = locker_dir / "main.cu"
    if not default_src.exists() and not any(locker_dir.iterdir()):
        default_src.write_text(problem["starter_code"], encoding="utf-8")

    files = [f.name for f in sorted(locker_dir.iterdir()) if f.is_file()]
    quota_mb = problem["student_quota_mb"]
    used_bytes, quota_bytes, exceeded = get_locker_usage(str(locker_dir), quota_mb * 1024 * 1024)

    conn.close()
    return render_template(
        "ide.html",
        course=course,
        user=user,
        is_teacher=is_teacher,
        is_admin=is_admin,
        problem=problem,
        files=files,
        used_mb=round(used_bytes / (1024 * 1024), 2),
        quota_mb=quota_mb,
        quota_pct=min(100, round((used_bytes / quota_bytes) * 100, 1)),
        is_exceeded=exceeded
    )


# =====================================================================
# 2. FILE MANAGEMENT APIS
# =====================================================================

@coding_ide_bp.route("/courses/<int:course_id>/ide/api/files", methods=["GET"])
@course_ide_access_required
def list_locker_files(course_id):
    user_id = session["user_id"]
    locker_dir = get_student_locker(user_id)

    files = []
    for f in sorted(locker_dir.iterdir()):
        if f.is_file():
            files.append({
                "name": f.name,
                "size_bytes": f.stat().st_size,
                "modified": f.stat().st_mtime
            })

    used, quota, exceeded = get_locker_usage(str(locker_dir), 200 * 1024 * 1024)
    return jsonify({
        "files": files,
        "used_bytes": used,
        "used_mb": round(used / (1024 * 1024), 2),
        "quota_mb": 200,
        "quota_pct": min(100, round((used / (200 * 1024 * 1024)) * 100, 1)),
        "is_exceeded": exceeded
    })


@coding_ide_bp.route("/courses/<int:course_id>/ide/api/files/read", methods=["GET"])
@course_ide_access_required
def read_locker_file(course_id):
    user_id = session["user_id"]
    locker_dir = get_student_locker(user_id)
    filename = request.args.get("filename", "").strip()

    target = (locker_dir / filename).resolve()
    if not target.is_relative_to(locker_dir) or not target.exists():
        return jsonify({"success": False, "error": "File not found"}), 404

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        return jsonify({"success": True, "filename": filename, "content": content})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@coding_ide_bp.route("/courses/<int:course_id>/ide/api/files/save", methods=["POST"])
@course_ide_access_required
def save_locker_file(course_id):
    user_id = session["user_id"]
    locker_dir = get_student_locker(user_id)
    req = request.get_json() or {}

    filename = req.get("filename", "").strip()
    content = req.get("content", "")

    if not filename:
        return jsonify({"success": False, "error": "Filename required"}), 400

    target = (locker_dir / filename).resolve()
    if not target.is_relative_to(locker_dir):
        return jsonify({"success": False, "error": "Path traversal prohibited"}), 403

    target.write_text(content, encoding="utf-8")
    used, quota, exceeded = get_locker_usage(str(locker_dir), 200 * 1024 * 1024)

    return jsonify({
        "success": True,
        "filename": filename,
        "used_mb": round(used / (1024 * 1024), 2),
        "quota_pct": min(100, round((used / (200 * 1024 * 1024)) * 100, 1)),
        "is_exceeded": exceeded
    })


@coding_ide_bp.route("/courses/<int:course_id>/ide/api/files/create", methods=["POST"])
@course_ide_access_required
def create_locker_file(course_id):
    user_id = session["user_id"]
    locker_dir = get_student_locker(user_id)
    req = request.get_json() or {}
    filename = req.get("filename", "").strip()

    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        return jsonify({"success": False, "error": "Invalid filename"}), 400

    target = locker_dir / filename
    if target.exists():
        return jsonify({"success": False, "error": "File already exists"}), 400

    target.write_text("// Created in Cloud IDE\n", encoding="utf-8")
    return jsonify({"success": True, "filename": filename})


@coding_ide_bp.route("/courses/<int:course_id>/ide/api/files/delete", methods=["POST"])
@course_ide_access_required
def delete_locker_file(course_id):
    user_id = session["user_id"]
    locker_dir = get_student_locker(user_id)
    req = request.get_json() or {}
    filename = req.get("filename", "").strip()

    target = (locker_dir / filename).resolve()
    if not target.is_relative_to(locker_dir) or not target.exists():
        return jsonify({"success": False, "error": "File not found"}), 404

    target.unlink()
    return jsonify({"success": True, "filename": filename})


# =====================================================================
# 3. CODE EXECUTION & AUTOGRADER APIS
# =====================================================================

@coding_ide_bp.route("/courses/<int:course_id>/ide/api/execute", methods=["POST"])
@course_ide_access_required
def execute_code(course_id):
    user_id = session["user_id"]
    locker_dir = get_student_locker(user_id)
    req = request.get_json() or {}

    filename = req.get("filename", "main.cu")
    stdin_input = req.get("stdin", "")
    target = (locker_dir / filename).resolve()

    if not target.is_relative_to(locker_dir) or not target.exists():
        return jsonify({"success": False, "stdout": "", "stderr": "File not found."}), 404

    result = execute_in_sandbox(str(locker_dir), filename, stdin_input=stdin_input)
    return jsonify({
        "success": result.get("success", False),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", result.get("error", "")),
        "node": result.get("node", "gpu1"),
        "is_cuda": filename.endswith(('.cu', '.cuh')),
        "compile_time_ms": result.get("compile_time_ms", 0),
        "run_time_ms": result.get("duration_ms", 0),
        "exit_code": result.get("exit_code", -1)
    })


@coding_ide_bp.route("/courses/<int:course_id>/ide/api/autograder", methods=["POST"])
@course_ide_access_required
def run_autograder(course_id):
    user_id = session["user_id"]
    locker_dir = get_student_locker(user_id)
    req = request.get_json() or {}

    filename = req.get("filename", "main.cu")
    assignment_id = req.get("assignment_id")
    if assignment_id:
        assignment_id = int(assignment_id)

    conn = get_db()
    if assignment_id:
        assignment = conn.execute(
            "SELECT * FROM coding_assignments WHERE id = ? AND course_id = ?",
            (assignment_id, course_id)
        ).fetchone()
        test_cases = json.loads(assignment["test_cases_json"] or "[]")
        code_checks = json.loads(assignment["code_checks_json"] or "{}")
    else:
        test_cases = req.get("test_cases", [])
        code_checks = req.get("code_checks", {})

    conn.close()
    grade_report = evaluate_autograder(str(locker_dir), filename, test_cases, code_checks)
    return jsonify(grade_report)


# =====================================================================
# 4. TEACHER CONFIG & SMART IMPORT (PDF / JSON / TXT)
# =====================================================================

@coding_ide_bp.route("/courses/<int:course_id>/ide/api/teacher/import-problem", methods=["POST"])
@course_ide_access_required
def import_problem(course_id):
    user_id = session["user_id"]
    conn = get_db()
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    is_admin = (user["role"] == "admin")
    is_teacher = (course["teacher_id"] == user_id) or is_admin
    conn.close()

    if not is_teacher:
        return jsonify({"success": False, "error": "Instructor permission required."}), 403

    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded."}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"success": False, "error": "Empty filename."}), 400

    try:
        extracted = parse_uploaded_problem_file(file)
        return jsonify({
            "success": True,
            "filename": file.filename,
            "data": extracted
        })
    except Exception as e:
        return jsonify({"success": False, "error": f"Failed to parse file: {str(e)}"}), 400


@coding_ide_bp.route("/courses/<int:course_id>/ide/api/teacher/sample-template")
@course_ide_access_required
def sample_problem_template(course_id):
    fmt = request.args.get("format", "json").lower()
    if fmt == "txt":
        content = get_sample_problem_txt_template()
        filename = f"hoodle_coding_template_course_{course_id}.txt"
        mimetype = "text/plain"
    else:
        sample = get_sample_problem_json_template()
        content = json.dumps(sample, indent=2)
        filename = f"hoodle_coding_template_course_{course_id}.json"
        mimetype = "application/json"

    return Response(
        content,
        mimetype=mimetype,
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@coding_ide_bp.route("/courses/<int:course_id>/ide/api/teacher/save-assignment", methods=["POST"])
@course_ide_access_required
def save_assignment(course_id):
    user_id = session["user_id"]
    conn = get_db()
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    is_admin = (user["role"] == "admin")
    is_teacher = (course["teacher_id"] == user_id) or is_admin

    if not is_teacher:
        conn.close()
        return jsonify({"success": False, "error": "Instructor permission required."}), 403

    req = request.get_json() or {}
    title = req.get("title", "").strip() or "Untitled Coding Task"
    description = req.get("description", "")
    starter_code = req.get("starter_code", "")
    language = req.get("language", "cuda")
    test_cases = req.get("test_cases", [])
    code_checks = req.get("code_checks", {})
    quota_mb = int(req.get("quota_mb", 200))
    is_exam = 1 if req.get("is_exam") else 0
    duration_min = int(req.get("duration_minutes", 60))

    assignment_id = req.get("assignment_id")
    if assignment_id:
        assignment_id = int(assignment_id)
    now = datetime.now().isoformat()

    if assignment_id:
        conn.execute("""
            UPDATE coding_assignments
            SET title = ?, description = ?, starter_code = ?, language = ?,
                test_cases_json = ?, code_checks_json = ?, student_quota_mb = ?,
                is_exam = ?, duration_minutes = ?
            WHERE id = ? AND course_id = ?
        """, (
            title, description, starter_code, language,
            json.dumps(test_cases), json.dumps(code_checks), quota_mb,
            is_exam, duration_min, assignment_id, course_id
        ))
    else:
        cursor = conn.execute("""
            INSERT INTO coding_assignments (
                course_id, title, description, starter_code, language,
                max_points, test_cases_json, code_checks_json, student_quota_mb,
                is_exam, duration_minutes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            course_id, title, description, starter_code, language,
            100.0, json.dumps(test_cases), json.dumps(code_checks), quota_mb,
            is_exam, duration_min, now
        ))
        assignment_id = cursor.lastrowid

    conn.commit()
    conn.close()
    return jsonify({"success": True, "assignment_id": assignment_id})


# =====================================================================
# 5. COURSE FEATURE REQUESTS & ADMIN TOGGLES
# =====================================================================

@coding_ide_bp.route("/courses/<int:course_id>/request-feature", methods=["POST"])
def request_course_feature(course_id):
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    conn = get_db()
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if not course or course["teacher_id"] != user_id:
        conn.close()
        return jsonify({"success": False, "error": "Teacher permission required"}), 403

    req = request.get_json() or {}
    feature = req.get("feature")  # "cloud_ide" or "lockers"

    if feature == "cloud_ide":
        conn.execute("UPDATE courses SET ide_request_status = 'requested' WHERE id = ?", (course_id,))
    elif feature == "lockers":
        conn.execute("UPDATE courses SET locker_request_status = 'requested' WHERE id = ?", (course_id,))
    else:
        conn.close()
        return jsonify({"success": False, "error": "Invalid feature"}), 400

    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "Request submitted to Administrator."})


@coding_ide_bp.route("/admin/courses/<int:course_id>/toggle-feature", methods=["POST"])
def admin_toggle_course_feature(course_id):
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user or user["role"] != "admin":
        conn.close()
        return jsonify({"success": False, "error": "Administrator privilege required"}), 403

    req = request.get_json() or {}
    feature = req.get("feature")
    enable = 1 if req.get("enable") else 0

    if feature == "cloud_ide":
        conn.execute("UPDATE courses SET enable_cloud_ide = ?, ide_request_status = ? WHERE id = ?",
                     (enable, "approved" if enable else "rejected", course_id))
    elif feature == "lockers":
        conn.execute("UPDATE courses SET enable_student_lockers = ?, locker_request_status = ? WHERE id = ?",
                     (enable, "approved" if enable else "rejected", course_id))
    else:
        conn.close()
        return jsonify({"success": False, "error": "Invalid feature"}), 400

    conn.commit()
    conn.close()
    return jsonify({"success": True, "enabled": enable})


# =====================================================================
# 6. INTERACTIVE BASH TERMINAL (SANDBOXED PTY AS NIS USER hoodle_ide)
# =====================================================================

@coding_ide_bp.route("/courses/<int:course_id>/ide/api/terminal/init", methods=["POST"])
@course_ide_access_required
def init_terminal(course_id):
    user_id = session["user_id"]
    locker_dir = str(get_student_locker(user_id))
    session_id = f"term_{course_id}_{user_id}"
    ok = create_terminal_session(session_id, locker_dir, course_id=course_id)
    return jsonify({"success": ok, "session_id": session_id})


@coding_ide_bp.route("/courses/<int:course_id>/ide/api/terminal/write", methods=["POST"])
@course_ide_access_required
def terminal_write(course_id):
    user_id = session["user_id"]
    session_id = f"term_{course_id}_{user_id}"
    req = request.get_json() or {}
    data = req.get("data", "")
    ok = write_to_terminal(session_id, data)
    return jsonify({"success": ok})


@coding_ide_bp.route("/courses/<int:course_id>/ide/api/terminal/read", methods=["GET"])
@course_ide_access_required
def terminal_read(course_id):
    user_id = session["user_id"]
    session_id = f"term_{course_id}_{user_id}"
    output = read_from_terminal(session_id)
    return jsonify({"output": output})


@coding_ide_bp.route("/courses/<int:course_id>/ide/api/terminal/resize", methods=["POST"])
@course_ide_access_required
def terminal_resize(course_id):
    user_id = session["user_id"]
    session_id = f"term_{course_id}_{user_id}"
    req = request.get_json() or {}
    rows = int(req.get("rows", 24))
    cols = int(req.get("cols", 80))
    resize_terminal(session_id, rows, cols)
    return jsonify({"success": True})


# =====================================================================
# 7. TIMED LAB EXAM LOCKDOWN SHIELD & PROCTORING
# =====================================================================

@coding_ide_bp.route("/courses/<int:course_id>/ide/api/exam/strike", methods=["POST"])
@course_ide_access_required
def exam_record_strike(course_id):
    user_id = session["user_id"]
    req = request.get_json() or {}
    assignment_id = req.get("assignment_id")
    reason = req.get("reason", "Window defocused or fullscreen exited")

    conn = get_db()
    proctor = conn.execute(
        "SELECT * FROM coding_exam_proctor WHERE assignment_id = ? AND user_id = ?",
        (assignment_id, user_id)
    ).fetchone()

    now = datetime.now().isoformat()
    strikes = 1
    audit = [{"timestamp": now, "event": reason, "strike": 1}]

    if proctor:
        strikes = proctor["strikes_count"] + 1
        audit = json.loads(proctor["audit_log"] or "[]")
        audit.append({"timestamp": now, "event": reason, "strike": strikes})
        conn.execute("""
            UPDATE coding_exam_proctor
            SET strikes_count = ?, audit_log = ?, last_heartbeat = CURRENT_TIMESTAMP
            WHERE assignment_id = ? AND user_id = ?
        """, (strikes, json.dumps(audit), assignment_id, user_id))
    else:
        conn.execute("""
            INSERT INTO coding_exam_proctor (assignment_id, user_id, strikes_count, audit_log, ip_address)
            VALUES (?, ?, ?, ?, ?)
        """, (assignment_id, user_id, strikes, json.dumps(audit), request.remote_addr))

    conn.commit()
    conn.close()
    return jsonify({"success": True, "strikes": strikes, "is_locked": strikes >= 3})


@coding_ide_bp.route("/courses/<int:course_id>/ide/api/exam/submit", methods=["POST"])
@course_ide_access_required
def exam_submit_code(course_id):
    user_id = session["user_id"]
    locker_dir = get_student_locker(user_id)
    req = request.get_json() or {}
    assignment_id = req.get("assignment_id")

    # Read latest main.cu or first file
    code_snapshot = ""
    target_filename = "main.cu"
    for f in locker_dir.iterdir():
        if f.is_file() and f.suffix in ('.cu', '.c', '.cpp', '.py', '.java'):
            target_filename = f.name
            code_snapshot = f.read_text(encoding="utf-8", errors="replace")
            break

    conn = get_db()
    assignment = conn.execute(
        "SELECT * FROM coding_assignments WHERE id = ? AND course_id = ?",
        (assignment_id, course_id)
    ).fetchone()
    test_cases = json.loads(assignment["test_cases_json"] or "[]") if assignment else []
    code_checks = json.loads(assignment["code_checks_json"] or "{}") if assignment else {}

    cur = conn.cursor()
    cur.execute("""
        INSERT INTO coding_submissions (assignment_id, user_id, submitted_at, code_snapshot, autograde_score)
        VALUES (?, ?, CURRENT_TIMESTAMP, ?, 0.0)
    """, (assignment_id, user_id, code_snapshot))
    submission_id = cur.lastrowid

    conn.execute("""
        UPDATE coding_exam_proctor
        SET submitted_at = CURRENT_TIMESTAMP, is_locked = 1
        WHERE assignment_id = ? AND user_id = ?
    """, (assignment_id, user_id))

    conn.commit()
    conn.close()

    # Enqueue background evaluation via Task Queue (non-blocking for 25,000+ students)
    task_id = None
    try:
        from task_queue import enqueue_task
        task_id = enqueue_task("exam_submission", {
            "submission_id": submission_id,
            "assignment_id": assignment_id,
            "locker_path": str(locker_dir),
            "filename": target_filename,
            "test_cases": test_cases,
            "code_checks": code_checks
        })
    except Exception:
        pass

    return jsonify({
        "success": True,
        "message": "Exam submitted and workspace frozen. Evaluation queued.",
        "submission_id": submission_id,
        "task_id": task_id
    })


@coding_ide_bp.route("/courses/<int:course_id>/ide/api/task/<task_id>", methods=["GET"])
@course_ide_access_required
def get_ide_task_status(course_id, task_id):
    from task_queue import get_task_status
    st = get_task_status(task_id)
    if not st:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(st)


# =====================================================================
# 8. BACKGROUND PLAGIARISM & CODE SIMILARITY SCANNER
# =====================================================================

@coding_ide_bp.route("/courses/<int:course_id>/assignments/<int:assignment_id>/plagiarism", methods=["GET"])
@course_ide_access_required
def plagiarism_report(course_id, assignment_id):
    user_id = session["user_id"]
    conn = get_db()
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    is_teacher = (course["teacher_id"] == user_id) or (user["role"] == "admin")

    if not is_teacher:
        conn.close()
        abort(403, "Instructor permission required.")

    scan = conn.execute(
        "SELECT * FROM coding_plagiarism_scans WHERE assignment_id = ? ORDER BY id DESC LIMIT 1",
        (assignment_id,)
    ).fetchone()

    assignment = conn.execute("SELECT * FROM coding_assignments WHERE id = ?", (assignment_id,)).fetchone()
    conn.close()

    results = json.loads(scan["results_json"]) if scan and scan["results_json"] else []
    return jsonify({
        "status": scan["status"] if scan else "none",
        "assignment_title": assignment["title"] if assignment else "",
        "matches": results,
        "count": len(results)
    })


@coding_ide_bp.route("/courses/<int:course_id>/assignments/<int:assignment_id>/plagiarism/scan", methods=["POST"])
@course_ide_access_required
def trigger_plagiarism_scan(course_id, assignment_id):
    user_id = session["user_id"]
    conn = get_db()
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    is_teacher = (course["teacher_id"] == user_id) or (user["role"] == "admin")

    if not is_teacher:
        conn.close()
        abort(403, "Instructor permission required.")

    # Record scan entry in database
    cur = conn.execute("""
        INSERT INTO coding_plagiarism_scans (assignment_id, status)
        VALUES (?, 'processing')
    """, (assignment_id,))
    scan_id = cur.lastrowid
    conn.commit()

    # Launch background thread with nice +10
    import threading
    def _run_scan_worker():
        try:
            import os
            try:
                os.nice(10)
            except Exception:
                pass
            bg_conn = get_db()
            subs = bg_conn.execute("""
                SELECT cs.id as submission_id, cs.user_id, cs.code_snapshot, u.display_name, u.roll_number
                FROM coding_submissions cs
                JOIN users u ON cs.user_id = u.id
                WHERE cs.assignment_id = ?
            """, (assignment_id,)).fetchall()

            assign = bg_conn.execute("SELECT starter_code FROM coding_assignments WHERE id = ?", (assignment_id,)).fetchone()
            starter = assign["starter_code"] if assign else ""

            submissions_data = [
                {
                    "submission_id": s["submission_id"],
                    "user_id": s["user_id"],
                    "display_name": s["display_name"],
                    "roll_number": s["roll_number"],
                    "code": s["code_snapshot"] or ""
                }
                for s in subs
            ]

            similarity_results = calculate_similarity_matrix(submissions_data, starter_code=starter)
            bg_conn.execute("""
                UPDATE coding_plagiarism_scans
                SET status = 'completed', results_json = ?, total_submissions = ?
                WHERE id = ?
            """, (json.dumps(similarity_results), len(subs), scan_id))
            bg_conn.commit()
            bg_conn.close()
        except Exception as e:
            try:
                bg_conn = get_db()
                bg_conn.execute("UPDATE coding_plagiarism_scans SET status = 'failed', error_message = ? WHERE id = ?", (str(e), scan_id))
                bg_conn.commit()
                bg_conn.close()
            except Exception:
                pass

    t = threading.Thread(target=_run_scan_worker, daemon=True)
    t.start()

    conn.close()
    return jsonify({"success": True, "message": "Plagiarism scan initiated in background.", "scan_id": scan_id})


# =====================================================================
# 9. COURSE PYTHON PIP PACKAGE & MODULE MANAGER
# =====================================================================

@coding_ide_bp.route("/courses/<int:course_id>/ide/api/pip/list", methods=["GET"])
@course_ide_access_required
def course_pip_list(course_id):
    pkgs = list_course_packages(course_id)
    return jsonify({"packages": pkgs})


@coding_ide_bp.route("/courses/<int:course_id>/ide/api/pip/available", methods=["GET"])
@course_ide_access_required
def course_pip_available(course_id):
    """Returns comprehensive list of course packages, pre-installed system packages, popular catalog, and standard library."""
    course_pkgs = list_course_packages(course_id)
    popular_pkgs = get_popular_packages_catalog(course_id)
    system_pkgs = list_system_packages()
    return jsonify({
        "success": True,
        "course_packages": course_pkgs,
        "popular_packages": popular_pkgs,
        "system_packages": system_pkgs,
        "standard_modules": STANDARD_MODULES
    })


@coding_ide_bp.route("/courses/<int:course_id>/ide/api/pip/install", methods=["POST"])
@course_ide_access_required
def course_pip_install(course_id):
    user_id = session["user_id"]
    conn = get_db()
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    is_teacher = (course["teacher_id"] == user_id) or (user["role"] == "admin")
    conn.close()

    if not is_teacher:
        abort(403, "Instructor permission required to install course pip modules.")

    req = request.get_json() or {}
    package_name = req.get("package_name", "").strip()
    # Start asynchronous background job
    result = start_install_job(course_id, package_name)
    return jsonify(result)


@coding_ide_bp.route("/courses/<int:course_id>/ide/api/pip/job/<job_id>", methods=["GET"])
@course_ide_access_required
def course_pip_job_status(course_id, job_id):
    result = get_job_status(job_id)
    return jsonify(result)


@coding_ide_bp.route("/courses/<int:course_id>/ide/api/pip/uninstall", methods=["POST"])
@course_ide_access_required
def course_pip_uninstall(course_id):
    user_id = session["user_id"]
    conn = get_db()
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    is_teacher = (course["teacher_id"] == user_id) or (user["role"] == "admin")
    conn.close()

    if not is_teacher:
        abort(403, "Instructor permission required to uninstall course pip modules.")

    req = request.get_json() or {}
    package_name = req.get("package_name", "").strip()
    result = uninstall_course_package(course_id, package_name)
    return jsonify(result)
