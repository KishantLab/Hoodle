"""
quizzes.py - Interactive Quiz & Assessment System with Anti-Cheat Exam Lockdown
Part of Hoodle LMS. Supports:
- 6 Question Formats: MCQ, MSQ, Fill-in-the-Blank, MCQ+Justification, Code Output Predict, Code Missing Blank
- Exam Lockdown: Fullscreen enforcement, 3-strike tab-switch/blur auto-submit
- Teacher/TA Remote Approval Gate: App/Internet access requires instructor approval
- Scheduling: Scheduled start/end times with synchronized countdowns
- Teacher Reopen Workflow: Reopen locked attempts, grant extra time (+5m, +10m), reset strikes
- Continuous Autosave: Keystroke & option persistence
"""

import os
import re
import json
import time
import random
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, session, abort
)

from db_adapter import get_db, execute_db_write_with_retry

quizzes_bp = Blueprint("quizzes", __name__)


def parse_iso_datetime(dt_str):
    if not dt_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(dt_str.strip(), fmt)
        except ValueError:
            continue
    return None


def get_current_user():
    if "user_id" not in session:
        return None
    conn = get_db(read_only=True)
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()
    return user


def is_course_staff(course_id, user_id):
    if not user_id:
        return False
    user = get_current_user()
    if user and user["role"] in ("teacher", "admin"):
        return True
    conn = get_db(read_only=True)
    c = conn.execute("SELECT teacher_id FROM courses WHERE id = ?", (course_id,)).fetchone()
    if c and c["teacher_id"] == user_id:
        conn.close()
        return True
    staff = conn.execute("""
        SELECT 1 FROM course_enrollments
        WHERE course_id = ? AND user_id = ? AND role IN ('teacher', 'ta', 'co-teacher')
    """, (course_id, user_id)).fetchone()
    conn.close()
    return bool(staff)


def quiz_staff_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please sign in to proceed.", "warning")
            return redirect(url_for("login"))
        course_id = kwargs.get("course_id")
        if not is_course_staff(course_id, session["user_id"]):
            flash("Access restricted to instructors and teaching assistants.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated_function


def is_remote_request(req):
    """
    Detects if request is coming from Android App or Public Internet:
    - User-Agent contains 'Hoodle' or 'Android'
    - Host / X-Forwarded-Host header is hoodle.in or non-intranet IP
    """
    ua = (req.headers.get("User-Agent") or getattr(req.user_agent, "string", "") or "").lower()
    host = (req.headers.get("X-Forwarded-Host") or req.headers.get("Host") or req.host or "").lower()
    is_app = ("hoodle" in ua or "android" in ua or req.headers.get("X-Hoodle-App") == "1" or req.headers.get("X-Requested-With") == "com.hoodle.lms")
    is_internet = ("hoodle.in" in host or (host and not any(h in host for h in ("10.10.14.", "127.0.0.1", "localhost", "192.168."))))
    return bool(is_app or is_internet)


def grade_question(q_type, student_ans, correct_ans, acceptable_vars_json='[]', points=1.0):
    """
    Flexible auto-grading engine for all 6 formats:
    - mcq: Case-insensitive exact option match.
    - msq: Set comparison with partial credit for correct choices without wrong selections.
    - fitb, code_predict, code_blank: Normalized whitespace & linebreak comparison with variations.
    - mcq_justification: Objective option auto-graded; justification stored for TA review.
    """
    student_ans = (student_ans or '').strip()
    correct_ans = (correct_ans or '').strip()
    pts = float(points)
    
    if q_type == 'mcq':
        is_corr = (student_ans.lower() == correct_ans.lower()) and len(student_ans) > 0
        return (1 if is_corr else 0, pts if is_corr else 0.0)
        
    elif q_type == 'msq':
        s_set = set(x.strip().upper() for x in student_ans.split(',') if x.strip())
        c_set = set(x.strip().upper() for x in correct_ans.split(',') if x.strip())
        if not s_set or not c_set:
            return (0, 0.0)
        if s_set == c_set:
            return (1, pts)
        wrong = s_set - c_set
        right = s_set & c_set
        if not wrong and right:
            fraction = len(right) / len(c_set)
            return (0, round(pts * fraction, 2))
        return (0, 0.0)
        
    elif q_type in ('fitb', 'code_predict', 'code_blank'):
        def norm(s):
            return '\n'.join([line.rstrip() for line in s.strip().splitlines() if line.strip()]).strip()
        
        norm_student = norm(student_ans)
        norm_correct = norm(correct_ans)
        acceptable = [norm_correct]
        try:
            vars_list = json.loads(acceptable_vars_json or '[]')
            acceptable.extend([norm(v) for v in vars_list if v])
        except Exception:
            pass
            
        is_corr = any(norm_student.lower() == a.lower() for a in acceptable) and len(norm_student) > 0
        return (1 if is_corr else 0, pts if is_corr else 0.0)
        
    elif q_type == 'mcq_justification':
        is_corr = (student_ans.lower() == correct_ans.lower()) and len(student_ans) > 0
        return (1 if is_corr else 0, pts if is_corr else 0.0)
        
    return (0, 0.0)


# ==========================================================
# 1. QUIZ LISTING & MANAGEMENT
# ==========================================================

@quizzes_bp.route("/courses/<int:course_id>/quizzes")
def course_quizzes(course_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]
    conn = get_db(read_only=True)
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if not course:
        conn.close()
        abort(404)

    is_staff = is_course_staff(course_id, user_id)
    
    quizzes = conn.execute("""
        SELECT q.*, u.display_name as author_name,
               (SELECT COUNT(*) FROM quiz_questions qq WHERE qq.quiz_id = q.id) as question_count,
               (SELECT qa.status FROM quiz_attempts qa WHERE qa.quiz_id = q.id AND qa.student_id = ? ORDER BY qa.id DESC LIMIT 1) as my_status,
               (SELECT qa.score FROM quiz_attempts qa WHERE qa.quiz_id = q.id AND qa.student_id = ? ORDER BY qa.id DESC LIMIT 1) as my_score
        FROM quizzes q
        JOIN users u ON q.created_by = u.id
        WHERE q.course_id = ?
        ORDER BY q.created_at DESC
    """, (user_id, user_id, course_id)).fetchall()

    topics = conn.execute("SELECT * FROM topics WHERE course_id = ? ORDER BY display_order ASC", (course_id,)).fetchall()
    conn.close()

    now = datetime.now()
    quiz_items = []
    for q in quizzes:
        item = dict(q)
        st = parse_iso_datetime(q["start_time"])
        et = parse_iso_datetime(q["end_time"])
        item["is_upcoming"] = bool(st and now < st)
        item["is_active"] = bool((not st or now >= st) and (not et or now <= et))
        item["is_ended"] = bool(et and now > et)
        quiz_items.append(item)

    return render_template(
        "course_quizzes.html",
        course=course,
        quizzes=quiz_items,
        topics=topics,
        is_staff=is_staff,
        active_tab="quizzes"
    )


@quizzes_bp.route("/courses/<int:course_id>/quizzes/create", methods=["POST"])
@quiz_staff_required
def create_quiz(course_id):
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    topic_id = request.form.get("topic_id") or None
    duration = int(request.form.get("duration_minutes", 30))
    pass_marks = float(request.form.get("pass_marks", 40.0))
    total_marks = float(request.form.get("total_marks", 100.0))
    exam_lockdown = 1 if request.form.get("exam_lockdown") else 0
    shuffle_q = 1 if request.form.get("shuffle_questions") else 0
    remote_approval = 1 if request.form.get("remote_approval_required") else 0
    start_time = request.form.get("start_time", "").strip() or None
    end_time = request.form.get("end_time", "").strip() or None

    if not title:
        flash("Quiz title is required.", "danger")
        return redirect(url_for("quizzes.course_quizzes", course_id=course_id))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _do_insert(conn):
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("""
            INSERT INTO quizzes (
                course_id, topic_id, title, description, duration_minutes,
                pass_marks, total_marks, is_published, exam_lockdown,
                shuffle_questions, remote_approval_required, start_time, end_time,
                created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        """, (
            course_id, topic_id, title, description, duration,
            pass_marks, total_marks, exam_lockdown, shuffle_q,
            remote_approval, start_time, end_time, session["user_id"], now_str
        ))
        qid = getattr(cur, "lastrowid", None)
        conn.commit()
        return qid

    quiz_id = execute_db_write_with_retry(_do_insert)
    flash(f"🎉 Quiz '{title}' created! Now add your questions.", "success")
    return redirect(url_for("quizzes.edit_quiz", course_id=course_id, quiz_id=quiz_id))


# ==========================================================
# 2. QUESTION BUILDER & EDITOR (6 FORMATS)
# ==========================================================

@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/edit")
@quiz_staff_required
def edit_quiz(course_id, quiz_id):
    conn = get_db(read_only=True)
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    quiz = conn.execute("SELECT * FROM quizzes WHERE id = ? AND course_id = ?", (quiz_id, course_id)).fetchone()
    if not quiz:
        conn.close()
        abort(404)
        
    questions = conn.execute("""
        SELECT * FROM quiz_questions WHERE quiz_id = ? ORDER BY question_number ASC, id ASC
    """, (quiz_id,)).fetchall()
    conn.close()

    parsed_q = []
    for q in questions:
        item = dict(q)
        try:
            item["options"] = json.loads(q["options_json"] or "[]")
        except Exception:
            item["options"] = []
        try:
            item["variations"] = json.loads(q["acceptable_variations_json"] or "[]")
        except Exception:
            item["variations"] = []
        parsed_q.append(item)

    return render_template(
        "quiz_editor.html",
        course=course,
        quiz=quiz,
        questions=parsed_q,
        active_tab="quizzes"
    )


@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/questions/add", methods=["POST"])
@quiz_staff_required
def add_quiz_question(course_id, quiz_id):
    q_type = request.form.get("question_type", "mcq").strip()
    prompt = request.form.get("prompt", "").strip()
    points = float(request.form.get("points", 1.0))
    code_snippet = request.form.get("code_snippet", "").strip()
    code_language = request.form.get("code_language", "python").strip()
    explanation = request.form.get("explanation", "").strip()

    if not prompt:
        flash("Question prompt is required.", "danger")
        return redirect(url_for("quizzes.edit_quiz", course_id=course_id, quiz_id=quiz_id))

    options_list = []
    correct_ans = ""
    variations_list = []

    if q_type in ("mcq", "mcq_justification"):
        options_raw = request.form.getlist("options[]")
        options_list = [o.strip() for o in options_raw if o.strip()]
        correct_idx = request.form.get("correct_option")
        if correct_idx is not None and correct_idx.isdigit() and int(correct_idx) < len(options_list):
            correct_ans = chr(65 + int(correct_idx))
        else:
            correct_ans = "A"

    elif q_type == "msq":
        options_raw = request.form.getlist("options[]")
        options_list = [o.strip() for o in options_raw if o.strip()]
        correct_checks = request.form.getlist("correct_msq[]")
        correct_letters = sorted([chr(65 + int(i)) for i in correct_checks if i.isdigit() and int(i) < len(options_list)])
        correct_ans = ",".join(correct_letters)

    elif q_type in ("fitb", "code_predict", "code_blank"):
        correct_ans = request.form.get("correct_answer", "").strip()
        vars_raw = request.form.get("acceptable_variations", "").strip()
        if vars_raw:
            variations_list = [v.strip() for v in vars_raw.splitlines() if v.strip()]

    def _do_add_q(conn):
        conn.execute("BEGIN IMMEDIATE")
        max_num = conn.execute("SELECT MAX(question_number) as mn FROM quiz_questions WHERE quiz_id = ?", (quiz_id,)).fetchone()["mn"] or 0
        conn.execute("""
            INSERT INTO quiz_questions (
                quiz_id, question_number, question_type, prompt, code_snippet,
                code_language, options_json, correct_answer, acceptable_variations_json,
                points, explanation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            quiz_id, max_num + 1, q_type, prompt, code_snippet,
            code_language, json.dumps(options_list), correct_ans,
            json.dumps(variations_list), points, explanation
        ))
        # Recalculate total quiz points
        tot = conn.execute("SELECT SUM(points) as sp FROM quiz_questions WHERE quiz_id = ?", (quiz_id,)).fetchone()["sp"] or 0.0
        conn.execute("UPDATE quizzes SET total_marks = ? WHERE id = ?", (tot, quiz_id))
        conn.commit()

    execute_db_write_with_retry(_do_add_q)
    flash("✅ Question added successfully!", "success")
    return redirect(url_for("quizzes.edit_quiz", course_id=course_id, quiz_id=quiz_id))


@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/questions/<int:question_id>/delete", methods=["POST"])
@quiz_staff_required
def delete_quiz_question(course_id, quiz_id, question_id):
    def _do_del_q(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM quiz_questions WHERE id = ? AND quiz_id = ?", (question_id, quiz_id))
        tot = conn.execute("SELECT SUM(points) as sp FROM quiz_questions WHERE quiz_id = ?", (quiz_id,)).fetchone()["sp"] or 0.0
        conn.execute("UPDATE quizzes SET total_marks = ? WHERE id = ?", (tot, quiz_id))
        conn.commit()
    execute_db_write_with_retry(_do_del_q)
    flash("Question deleted.", "info")
    return redirect(url_for("quizzes.edit_quiz", course_id=course_id, quiz_id=quiz_id))


# ==========================================================
# 3. STUDENT EXAM INTERFACE, FULLSCREEN, AUTOSAVE & VIOLATIONS
# ==========================================================

@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/take")
def take_quiz(course_id, quiz_id):
    if "user_id" not in session:
        flash("Please sign in to access this quiz.", "warning")
        return redirect(url_for("login"))
    user_id = session["user_id"]

    conn = get_db(read_only=True)
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    quiz = conn.execute("SELECT * FROM quizzes WHERE id = ? AND course_id = ?", (quiz_id, course_id)).fetchone()
    if not course or not quiz:
        conn.close()
        abort(404)

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # 1. Schedule Check
    st = parse_iso_datetime(quiz["start_time"])
    et = parse_iso_datetime(quiz["end_time"])
    if st and now < st:
        conn.close()
        flash(f"⏳ Quiz Scheduled: This assessment opens on {quiz['start_time']}. Please return then.", "info")
        return redirect(url_for("quizzes.course_quizzes", course_id=course_id))

    if et and now > et:
        conn.close()
        flash("❌ Quiz Ended: The scheduled submission window has closed.", "danger")
        return redirect(url_for("quizzes.course_quizzes", course_id=course_id))

    # 2. Check or Create Attempt
    attempt = conn.execute("""
        SELECT * FROM quiz_attempts
        WHERE quiz_id = ? AND student_id = ?
        ORDER BY id DESC LIMIT 1
    """, (quiz_id, user_id)).fetchone()

    is_remote = 1 if is_remote_request(request) else 0

    if not attempt:
        # Determine initial status: If remote access requires approval, set status = pending_approval
        init_status = "in_progress"
        remote_approved = 1
        if quiz["remote_approval_required"] and is_remote:
            init_status = "pending_approval"
            remote_approved = 0

        def _do_create_att(c):
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute("""
                INSERT INTO quiz_attempts (
                    quiz_id, student_id, status, started_at, total_possible,
                    is_remote, remote_approved
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (quiz_id, user_id, init_status, now_str, quiz["total_marks"] or 0.0, is_remote, remote_approved))
            aid = getattr(cur, "lastrowid", None)
            c.commit()
            return aid

        att_id = execute_db_write_with_retry(_do_create_att)
        attempt = conn.execute("SELECT * FROM quiz_attempts WHERE id = ?", (att_id,)).fetchone()

    conn.close()

    # If pending teacher approval for remote access
    if attempt["status"] == "pending_approval":
        return render_template(
            "quiz_pending_approval.html",
            course=course,
            quiz=quiz,
            attempt=attempt
        )

    # If already submitted or locked out
    if attempt["status"] in ("submitted", "auto_submitted_violation"):
        return render_template(
            "quiz_result.html",
            course=course,
            quiz=quiz,
            attempt=attempt
        )

    # 3. Load Questions & Previously Autosaved Answers
    conn = get_db(read_only=True)
    raw_questions = conn.execute("""
        SELECT * FROM quiz_questions WHERE quiz_id = ? ORDER BY question_number ASC, id ASC
    """, (quiz_id,)).fetchall()

    saved_answers_rows = conn.execute("""
        SELECT * FROM quiz_answers WHERE attempt_id = ?
    """, (attempt["id"],)).fetchall()
    conn.close()

    answers_map = {a["question_id"]: dict(a) for a in saved_answers_rows}

    # Format questions
    questions_list = []
    for q in raw_questions:
        q_dict = dict(q)
        try:
            q_dict["options"] = json.loads(q["options_json"] or "[]")
        except Exception:
            q_dict["options"] = []
        ans = answers_map.get(q["id"], {})
        q_dict["saved_answer"] = ans.get("student_answer", "")
        q_dict["saved_justification"] = ans.get("justification", "")
        questions_list.append(q_dict)

    if quiz["shuffle_questions"]:
        random.seed(attempt["id"])
        random.shuffle(questions_list)

    # Calculate remaining time in seconds
    started_dt = parse_iso_datetime(attempt["started_at"]) or now
    total_allowed_sec = (quiz["duration_minutes"] + (attempt["extra_time_minutes"] or 0)) * 60
    elapsed_sec = (now - started_dt).total_seconds()
    remaining_sec = max(0, int(total_allowed_sec - elapsed_sec))

    return render_template(
        "quiz_take.html",
        course=course,
        quiz=quiz,
        attempt=attempt,
        questions=questions_list,
        remaining_sec=remaining_sec
    )


@quizzes_bp.route("/api/quizzes/<int:quiz_id>/autosave", methods=["POST"])
def api_quiz_autosave(quiz_id):
    """
    Dynamic Continuous Autosave API:
    Saves student selected options or typed code predictions without page refresh.
    Guarantees zero data loss on browser crash, freeze, or auto-submit.
    """
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = session["user_id"]
    data = request.get_json(silent=True) or {}
    attempt_id = data.get("attempt_id")
    question_id = data.get("question_id")
    answer = data.get("answer", "")
    justification = data.get("justification", "")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _do_save(conn):
        conn.execute("BEGIN IMMEDIATE")
        # Validate attempt belongs to student and is active
        att = conn.execute("SELECT id, status FROM quiz_attempts WHERE id = ? AND student_id = ?", (attempt_id, user_id)).fetchone()
        if not att or att["status"] != "in_progress":
            conn.rollback()
            return False

        conn.execute("""
            INSERT INTO quiz_answers (attempt_id, question_id, student_answer, justification, saved_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(attempt_id, question_id) DO UPDATE SET
                student_answer = excluded.student_answer,
                justification = excluded.justification,
                saved_at = excluded.saved_at
        """, (attempt_id, question_id, str(answer), str(justification), now_str))
        conn.commit()
        return True

    success = execute_db_write_with_retry(_do_save)
    return jsonify({"success": bool(success), "saved_at": now_str})


@quizzes_bp.route("/api/quizzes/<int:quiz_id>/strike", methods=["POST"])
def api_quiz_strike(quiz_id):
    """
    Anti-Cheat Violation Strike Endpoint:
    Tracks tab switches, window blurs, and exiting fullscreen.
    - Strike 1: Warning modal
    - Strike 2: Final red warning
    - Strike 3: Immediate auto-submit and lock (auto_submitted_violation)
    """
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = session["user_id"]
    data = request.get_json(silent=True) or {}
    attempt_id = data.get("attempt_id")
    reason = data.get("reason", "Tab switch or window blur detected")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    att = conn.execute("SELECT * FROM quiz_attempts WHERE id = ? AND student_id = ?", (attempt_id, user_id)).fetchone()
    if not att or att["status"] != "in_progress":
        conn.close()
        return jsonify({"error": "Invalid attempt"}), 400

    strikes = (att["strikes"] or 0) + 1
    strike_log = []
    try:
        strike_log = json.loads(att["strike_log_json"] or "[]")
    except Exception:
        pass
    strike_log.append({"strike": strikes, "reason": reason, "time": now_str})
    conn.close()

    new_status = "auto_submitted_violation" if strikes >= 3 else "in_progress"

    def _do_strike(c):
        c.execute("BEGIN IMMEDIATE")
        c.execute("""
            UPDATE quiz_attempts
            SET strikes = ?, strike_log_json = ?, status = ?
            WHERE id = ?
        """, (strikes, json.dumps(strike_log), new_status, attempt_id))
        c.commit()
        return strikes

    execute_db_write_with_retry(_do_strike)

    if strikes >= 3:
        # Trigger auto-grading immediately
        perform_quiz_grading(quiz_id, attempt_id)
        return jsonify({"strikes": strikes, "locked": True, "action": "auto_submitted_violation"})

    return jsonify({"strikes": strikes, "locked": False})


@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/submit", methods=["POST"])
def submit_quiz(course_id, quiz_id):
    """
    Final Student Quiz Submission & Auto-Grading Execution.
    """
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]
    attempt_id = request.form.get("attempt_id")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    att = conn.execute("SELECT * FROM quiz_attempts WHERE id = ? AND student_id = ?", (attempt_id, user_id)).fetchone()
    if not att or att["status"] != "in_progress":
        conn.close()
        flash("Quiz attempt already submitted or locked.", "info")
        return redirect(url_for("quizzes.take_quiz", course_id=course_id, quiz_id=quiz_id))

    # Mark submitted
    conn.execute("""
        UPDATE quiz_attempts
        SET status = 'submitted', submitted_at = ?
        WHERE id = ?
    """, (now_str, attempt_id))
    conn.commit()
    conn.close()

    perform_quiz_grading(quiz_id, attempt_id)
    flash("🎉 Quiz submitted successfully!", "success")
    return redirect(url_for("quizzes.take_quiz", course_id=course_id, quiz_id=quiz_id))


def perform_quiz_grading(quiz_id, attempt_id):
    """Evaluates all student answers and updates final scores in quiz_attempts."""
    conn = get_db()
    quiz = conn.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    questions = conn.execute("SELECT * FROM quiz_questions WHERE quiz_id = ?", (quiz_id,)).fetchall()
    answers = conn.execute("SELECT * FROM quiz_answers WHERE attempt_id = ?", (attempt_id,)).fetchall()

    ans_map = {a["question_id"]: dict(a) for a in answers}
    total_score = 0.0

    for q in questions:
        q_id = q["id"]
        pts_possible = float(q["points"] or 1.0)
        stud_ans = ans_map.get(q_id, {}).get("student_answer", "") if ans_map.get(q_id) else ""
        
        is_corr, awarded = grade_question(
            q["question_type"],
            stud_ans,
            q["correct_answer"],
            q["acceptable_variations_json"],
            pts_possible
        )
        total_score += awarded

        # Update points awarded in answer table
        if q_id in ans_map:
            conn.execute("""
                UPDATE quiz_answers
                SET is_correct = ?, points_awarded = ?
                WHERE attempt_id = ? AND question_id = ?
            """, (is_corr, awarded, attempt_id, q_id))

    pass_threshold = float(quiz["pass_marks"] or 40.0)
    is_passed = 1 if total_score >= pass_threshold else 0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn.execute("""
        UPDATE quiz_attempts
        SET score = ?, is_passed = ?, submitted_at = COALESCE(submitted_at, ?)
        WHERE id = ?
    """, (round(total_score, 2), is_passed, now_str, attempt_id))
    conn.commit()
    conn.close()


# ==========================================================
# 4. TEACHER / TA SUBMISSIONS DASHBOARD, REOPEN & APPROVALS
# ==========================================================

@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/submissions")
@quiz_staff_required
def quiz_submissions(course_id, quiz_id):
    conn = get_db(read_only=True)
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    quiz = conn.execute("SELECT * FROM quizzes WHERE id = ? AND course_id = ?", (quiz_id, course_id)).fetchone()
    if not quiz:
        conn.close()
        abort(404)

    attempts = conn.execute("""
        SELECT qa.*, u.roll_number, u.display_name, u.email
        FROM quiz_attempts qa
        JOIN users u ON qa.student_id = u.id
        WHERE qa.quiz_id = ?
        ORDER BY qa.started_at DESC
    """, (quiz_id,)).fetchall()
    conn.close()

    pending_approvals = [a for a in attempts if a["status"] == "pending_approval"]
    active_attempts = [a for a in attempts if a["status"] == "in_progress"]
    completed_attempts = [a for a in attempts if a["status"] in ("submitted", "auto_submitted_violation")]

    return render_template(
        "quiz_submissions.html",
        course=course,
        quiz=quiz,
        pending_approvals=pending_approvals,
        active_attempts=active_attempts,
        completed_attempts=completed_attempts,
        attempts=attempts,
        active_tab="quizzes"
    )


@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/approve-student/<int:attempt_id>", methods=["POST"])
@quiz_staff_required
def approve_remote_student(course_id, quiz_id, attempt_id):
    """
    Teacher & TA Remote Access Approval Gate:
    Authorizes a student taking the exam via mobile app or public internet.
    """
    actor_id = session.get("user_id")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _do_approve(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            UPDATE quiz_attempts
            SET status = 'in_progress', remote_approved = 1, approved_by = ?, approved_at = ?
            WHERE id = ? AND quiz_id = ? AND status = 'pending_approval'
        """, (actor_id, now_str, attempt_id, quiz_id))
        conn.commit()

    execute_db_write_with_retry(_do_approve)
    flash("✅ Student remote access approved! Exam unlocked for student.", "success")
    return redirect(url_for("quizzes.quiz_submissions", course_id=course_id, quiz_id=quiz_id))


@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/reopen-student/<int:attempt_id>", methods=["POST"])
@quiz_staff_required
def reopen_student_exam(course_id, quiz_id, attempt_id):
    """
    Teacher & TA Exam Reopen Workflow:
    Unlocks auto-submitted or locked-out attempts, resets strikes, and grants extra time.
    Previously autosaved answers remain 100% intact so the student resumes seamlessly.
    """
    extra_minutes = int(request.form.get("extra_minutes", 10))

    def _do_reopen(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            UPDATE quiz_attempts
            SET status = 'in_progress', strikes = 0,
                extra_time_minutes = COALESCE(extra_time_minutes, 0) + ?
            WHERE id = ? AND quiz_id = ?
        """, (extra_minutes, attempt_id, quiz_id))
        conn.commit()

    execute_db_write_with_retry(_do_reopen)
    flash(f"🔓 Exam Reopened! Strikes reset and +{extra_minutes} minutes extra time granted. Student may resume immediately.", "info")
    return redirect(url_for("quizzes.quiz_submissions", course_id=course_id, quiz_id=quiz_id))
