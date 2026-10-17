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
    flash, jsonify, session, abort, Response
)

from db_adapter import get_db, execute_db_write_with_retry
from quiz_importer import (
    parse_uploaded_quiz_file, get_sample_json_template, get_sample_txt_template
)

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


def normalize_option_answer(ans, options=None):
    if not ans:
        return ""
    ans_str = str(ans).strip()
    ans_clean = ans_str.upper()

    # If single letter like 'A', 'B', 'C', 'D'
    if len(ans_clean) == 1 and 'A' <= ans_clean <= 'Z':
        return ans_clean

    # Format like "B. Something" or "B) Something"
    m = re.match(r'^([A-Za-z])[\.\)\:\-]\s*', ans_str)
    if m:
        return m.group(1).upper()

    # If options list is available, match by text or prefix
    if options and isinstance(options, list):
        for idx, opt in enumerate(options):
            opt_letter = chr(65 + idx)
            opt_clean = str(opt).strip().lower()
            if ans_str.lower() == opt_clean:
                return opt_letter
            m_opt = re.match(r'^([A-Za-z])[\.\)\:\-]\s*(.*)', str(opt).strip())
            if m_opt and ans_str.lower() == m_opt.group(2).strip().lower():
                return opt_letter

    return ans_str


def grade_question(q_type, student_ans, correct_ans, acceptable_vars_json='[]', points=1.0, options=None):
    """
    Flexible auto-grading engine for all 6 formats:
    - mcq: Case-insensitive match on option letter (A, B, C...) or option text.
    - msq: Set comparison with partial credit for correct choices without wrong selections.
    - fitb, code_predict, code_blank: Normalized whitespace, lines, case-insensitivity, stripped quotes, and variations.
    - mcq_justification: Objective option auto-graded; justification stored for TA review.
    """
    student_ans = (student_ans or '').strip()
    correct_ans = (correct_ans or '').strip()
    pts = float(points)

    if q_type in ('mcq', 'mcq_justification'):
        if not student_ans or not correct_ans:
            return (0, 0.0)
        norm_s = normalize_option_answer(student_ans, options).upper()
        norm_c = normalize_option_answer(correct_ans, options).upper()
        is_corr = (norm_s == norm_c) or (student_ans.lower() == correct_ans.lower())
        return (1 if is_corr else 0, pts if is_corr else 0.0)

    elif q_type == 'msq':
        def parse_msq_set(val):
            if not val:
                return set()
            parts = [p.strip() for p in str(val).split(',') if p.strip()]
            norm_parts = set()
            for p in parts:
                letter = normalize_option_answer(p, options)
                norm_parts.add(letter.upper())
            return norm_parts

        s_set = parse_msq_set(student_ans)
        c_set = parse_msq_set(correct_ans)
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
        def norm_code(s):
            if not s:
                return ""
            lines = [line.strip() for line in str(s).strip().splitlines() if line.strip()]
            res = "\n".join(lines).strip()
            if (res.startswith('"') and res.endswith('"')) or (res.startswith("'") and res.endswith("'")):
                res = res[1:-1].strip()
            return res

        s_clean = norm_code(student_ans)
        c_clean = norm_code(correct_ans)

        acceptable = [c_clean]
        if c_clean.endswith(';'):
            acceptable.append(c_clean[:-1].strip())
        if c_clean.endswith('()'):
            acceptable.append(c_clean[:-2].strip())
        if c_clean.endswith('();'):
            acceptable.append(c_clean[:-3].strip())

        try:
            vars_list = json.loads(acceptable_vars_json or '[]')
            for v in vars_list:
                v_c = norm_code(v)
                if v_c:
                    acceptable.append(v_c)
                    if v_c.endswith(';'):
                        acceptable.append(v_c[:-1].strip())
                    if v_c.endswith('()'):
                        acceptable.append(v_c[:-2].strip())
                    if v_c.endswith('();'):
                        acceptable.append(v_c[:-3].strip())
        except Exception:
            pass

        def strip_punc(t):
            return t.rstrip(';').rstrip().rstrip('()').rstrip()

        is_corr = False
        s_lower = s_clean.lower()
        if any(s_lower == a.lower() for a in acceptable if a):
            is_corr = True
        elif any(strip_punc(s_lower) == strip_punc(a.lower()) for a in acceptable if strip_punc(a.lower())):
            is_corr = True

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
               (SELECT COALESCE(SUM(qq.points), 0.0) FROM quiz_questions qq WHERE qq.quiz_id = q.id) as computed_total_marks,
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
        if item.get("question_count", 0) > 0:
            item["total_marks"] = item["computed_total_marks"]
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


@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/settings/update", methods=["POST"])
@quiz_staff_required
def update_quiz_settings(course_id, quiz_id):
    """
    Allows instructors and TAs to update quiz time, pass marks, total marks,
    lockdown settings, and schedule after creation.
    """
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    duration = int(request.form.get("duration_minutes", 30))
    pass_marks = float(request.form.get("pass_marks", 40.0))
    exam_lockdown = 1 if request.form.get("exam_lockdown") else 0
    shuffle_q = 1 if request.form.get("shuffle_questions") else 0
    remote_approval = 1 if request.form.get("remote_approval_required") else 0
    start_time = request.form.get("start_time", "").strip() or None
    end_time = request.form.get("end_time", "").strip() or None
    total_marks_raw = request.form.get("total_marks")

    if not title:
        flash("Quiz title is required.", "danger")
        return redirect(url_for("quizzes.edit_quiz", course_id=course_id, quiz_id=quiz_id))

    def _do_update_settings(conn):
        conn.execute("BEGIN IMMEDIATE")
        tot = conn.execute("SELECT COALESCE(SUM(points), 0.0) as sp FROM quiz_questions WHERE quiz_id = ?", (quiz_id,)).fetchone()["sp"] or 0.0

        conn.execute("""
            UPDATE quizzes
            SET title = ?, description = ?, duration_minutes = ?,
                pass_marks = ?, total_marks = ?, exam_lockdown = ?,
                shuffle_questions = ?, remote_approval_required = ?,
                start_time = ?, end_time = ?
            WHERE id = ? AND course_id = ?
        """, (
            title, description, duration, pass_marks, float(tot),
            exam_lockdown, shuffle_q, remote_approval, start_time, end_time,
            quiz_id, course_id
        ))
        conn.commit()

    execute_db_write_with_retry(_do_update_settings)
    flash("✅ Quiz settings, duration, and pass marks updated successfully!", "success")
    return redirect(url_for("quizzes.edit_quiz", course_id=course_id, quiz_id=quiz_id))


@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/delete", methods=["POST"])
@quiz_staff_required
def delete_quiz(course_id, quiz_id):
    """
    Allows instructors and TAs to permanently delete a quiz and all associated
    questions, attempts, and submitted answers.
    """
    def _do_delete_quiz(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM quiz_answers WHERE attempt_id IN (SELECT id FROM quiz_attempts WHERE quiz_id = ?)", (quiz_id,))
        conn.execute("DELETE FROM quiz_attempts WHERE quiz_id = ?", (quiz_id,))
        conn.execute("DELETE FROM quiz_questions WHERE quiz_id = ?", (quiz_id,))
        conn.execute("DELETE FROM quizzes WHERE id = ? AND course_id = ?", (quiz_id, course_id))
        conn.commit()

    execute_db_write_with_retry(_do_delete_quiz)
    flash("🗑️ Quiz and all associated attempts deleted successfully.", "info")
    return redirect(url_for("quizzes.course_quizzes", course_id=course_id))


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

    # Synchronize total_marks with the sum of all question points
    calculated_total = sum(float(q["points"] or 1.0) for q in questions)
    if questions and float(quiz["total_marks"] or 0.0) != float(calculated_total):
        conn.close()
        def _sync_tot(c):
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE quizzes SET total_marks = ? WHERE id = ?", (float(calculated_total), quiz_id))
            c.commit()
        execute_db_write_with_retry(_sync_tot)
        conn = get_db(read_only=True)
        quiz = conn.execute("SELECT * FROM quizzes WHERE id = ? AND course_id = ?", (quiz_id, course_id)).fetchone()

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


@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/questions/<int:question_id>/update-points", methods=["POST"])
@quiz_staff_required
def update_question_points(course_id, quiz_id, question_id):
    """Allows teachers to update points on a specific question and recalculates total marks."""
    points = float(request.form.get("points", 1.0))
    def _do_upd_pts(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE quiz_questions SET points = ? WHERE id = ? AND quiz_id = ?", (points, question_id, quiz_id))
        tot = conn.execute("SELECT SUM(points) as sp FROM quiz_questions WHERE quiz_id = ?", (quiz_id,)).fetchone()["sp"] or 0.0
        conn.execute("UPDATE quizzes SET total_marks = ? WHERE id = ?", (tot, quiz_id))
        conn.commit()
    execute_db_write_with_retry(_do_upd_pts)
    flash(f"✅ Question points updated to {points} pts.", "success")
    return redirect(url_for("quizzes.edit_quiz", course_id=course_id, quiz_id=quiz_id))


@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/questions/<int:question_id>/edit", methods=["POST"])
@quiz_staff_required
def edit_quiz_question(course_id, quiz_id, question_id):
    """
    Allows instructors and TAs to edit question prompt, options, code, answer, points,
    and the required justification / explanation note.
    """
    q_type = request.form.get("question_type", "mcq").strip()
    prompt = request.form.get("prompt", "").strip()
    points = float(request.form.get("points", 1.0))
    code_snippet = request.form.get("code_snippet", "").strip()
    code_language = request.form.get("code_language", "python").strip()
    explanation = request.form.get("explanation", "").strip()

    if not prompt:
        flash("Question prompt cannot be empty.", "danger")
        return redirect(url_for("quizzes.edit_quiz", course_id=course_id, quiz_id=quiz_id))

    options_list = []
    correct_ans = ""
    variations_list = []

    if q_type in ("mcq", "mcq_justification"):
        options_raw = request.form.getlist("options[]")
        options_list = [o.strip() for o in options_raw if o.strip()]
        correct_idx = request.form.get("correct_option")
        if correct_idx is not None and str(correct_idx).isdigit() and int(correct_idx) < len(options_list):
            correct_ans = chr(65 + int(correct_idx))
        else:
            fallback = request.form.get("correct_option_letter", "A").strip().upper()
            correct_ans = fallback if fallback else "A"

    elif q_type == "msq":
        options_raw = request.form.getlist("options[]")
        options_list = [o.strip() for o in options_raw if o.strip()]
        correct_checks = request.form.getlist("correct_msq[]")
        correct_letters = sorted([chr(65 + int(i)) for i in correct_checks if str(i).isdigit() and int(i) < len(options_list)])
        if correct_letters:
            correct_ans = ",".join(correct_letters)
        else:
            correct_ans = request.form.get("correct_msq_str", "").strip().upper()

    elif q_type in ("fitb", "code_predict", "code_blank"):
        correct_ans = request.form.get("correct_answer", "").strip()
        vars_raw = request.form.get("acceptable_variations", "").strip()
        if vars_raw:
            variations_list = [v.strip() for v in vars_raw.splitlines() if v.strip()]

    def _do_edit_q(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            UPDATE quiz_questions
            SET question_type = ?, prompt = ?, code_snippet = ?,
                code_language = ?, options_json = ?, correct_answer = ?,
                acceptable_variations_json = ?, points = ?, explanation = ?
            WHERE id = ? AND quiz_id = ?
        """, (
            q_type, prompt, code_snippet,
            code_language, json.dumps(options_list), correct_ans,
            json.dumps(variations_list), points, explanation,
            question_id, quiz_id
        ))
        tot = conn.execute("SELECT SUM(points) as sp FROM quiz_questions WHERE quiz_id = ?", (quiz_id,)).fetchone()["sp"] or 0.0
        conn.execute("UPDATE quizzes SET total_marks = ? WHERE id = ?", (tot, quiz_id))
        conn.commit()

    execute_db_write_with_retry(_do_edit_q)
    flash("✅ Question and justification updated successfully!", "success")
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
    if course and dict(course).get("quiz_network_policy") == "intranet_only":
        client_ip = request.headers.get("X-Forwarded-For", request.headers.get("X-Real-IP", request.remote_addr or "")).split(",")[0].strip()
        is_intranet = client_ip.startswith("10.") or client_ip.startswith("192.168.") or client_ip in ("127.0.0.1", "::1")
        if not is_intranet:
            conn.close()
            flash("🔒 Campus Wi-Fi Required: This quiz is restricted by your instructor to the IIT Bhilai Campus Intranet Wi-Fi (10.10.*.*). Please connect to campus Wi-Fi to attempt this quiz.", "danger")
            return redirect(url_for("quizzes.course_quizzes", course_id=course_id))

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
    Guarantees zero data loss: extracts all submitted form fields (radios, MSQ checkboxes,
    text answers, and justifications) and persists them to quiz_answers before auto-grading.
    """
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]
    attempt_id = request.form.get("attempt_id")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if course and dict(course).get("quiz_network_policy") == "intranet_only":
        client_ip = request.headers.get("X-Forwarded-For", request.headers.get("X-Real-IP", request.remote_addr or "")).split(",")[0].strip()
        is_intranet = client_ip.startswith("10.") or client_ip.startswith("192.168.") or client_ip in ("127.0.0.1", "::1")
        if not is_intranet:
            conn.close()
            flash("🔒 Campus Wi-Fi Required: Submissions for this quiz can only be sent from the IIT Bhilai Campus Intranet Wi-Fi (10.10.*.*).", "danger")
            return redirect(url_for("quizzes.course_quizzes", course_id=course_id))

    att = conn.execute("SELECT * FROM quiz_attempts WHERE id = ? AND student_id = ?", (attempt_id, user_id)).fetchone()
    if not att:
        conn.close()
        flash("Quiz attempt not found.", "danger")
        return redirect(url_for("quizzes.course_quizzes", course_id=course_id))

    if att["status"] not in ("in_progress", "pending_approval"):
        conn.close()
        flash("Quiz attempt already submitted.", "info")
        return redirect(url_for("quizzes.take_quiz", course_id=course_id, quiz_id=quiz_id))

    # 1. Fetch questions to save answers submitted via standard POST form
    questions = conn.execute("SELECT * FROM quiz_questions WHERE quiz_id = ?", (quiz_id,)).fetchall()

    for q in questions:
        qid = q["id"]
        q_type = q["question_type"]
        student_ans = ""
        if q_type == "msq":
            msq_vals = request.form.getlist(f"msq_{qid}[]")
            if msq_vals:
                student_ans = ",".join(sorted([v.strip() for v in msq_vals if v.strip()]))
        else:
            student_ans = request.form.get(f"ans_{qid}", "").strip()

        justification = request.form.get(f"just_{qid}", "").strip()

        # Save / upsert answer into quiz_answers
        conn.execute("""
            INSERT INTO quiz_answers (attempt_id, question_id, student_answer, justification, saved_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(attempt_id, question_id) DO UPDATE SET
                student_answer = CASE WHEN excluded.student_answer != '' THEN excluded.student_answer ELSE quiz_answers.student_answer END,
                justification = CASE WHEN excluded.justification != '' THEN excluded.justification ELSE quiz_answers.justification END,
                saved_at = excluded.saved_at
        """, (attempt_id, qid, student_ans, justification, now_str))

    # 2. Mark submitted
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
    questions = conn.execute("SELECT * FROM quiz_questions WHERE quiz_id = ? ORDER BY question_number, id", (quiz_id,)).fetchall()
    answers = conn.execute("SELECT * FROM quiz_answers WHERE attempt_id = ?", (attempt_id,)).fetchall()

    ans_map = {a["question_id"]: dict(a) for a in answers}
    total_score = 0.0

    for q in questions:
        q_id = q["id"]
        pts_possible = float(q["points"] or 1.0)
        options = []
        try:
            options = json.loads(q["options_json"] or "[]")
        except Exception:
            options = []

        stud_ans = ans_map.get(q_id, {}).get("student_answer", "") if ans_map.get(q_id) else ""

        is_corr, awarded = grade_question(
            q["question_type"],
            stud_ans,
            q["correct_answer"],
            q["acceptable_variations_json"],
            pts_possible,
            options=options
        )
        total_score += awarded

        # Update points awarded in answer table or insert if missing
        if q_id in ans_map:
            conn.execute("""
                UPDATE quiz_answers
                SET is_correct = ?, points_awarded = ?
                WHERE attempt_id = ? AND question_id = ?
            """, (is_corr, awarded, attempt_id, q_id))
        else:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""
                INSERT INTO quiz_answers (attempt_id, question_id, student_answer, justification, is_correct, points_awarded, saved_at)
                VALUES (?, ?, '', '', ?, ?, ?)
            """, (attempt_id, q_id, is_corr, awarded, now_str))

    tot_possible = float(quiz["total_marks"] or 0.0)
    pass_val = float(quiz["pass_marks"] or 40.0)
    # If pass marks was entered as percentage > total possible (e.g. 40% on a 16-point quiz)
    if pass_val > tot_possible and tot_possible > 0:
        pass_threshold = (pass_val / 100.0) * tot_possible
    else:
        pass_threshold = pass_val

    is_passed = 1 if total_score >= pass_threshold else 0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn.execute("""
        UPDATE quiz_attempts
        SET score = ?, is_passed = ?, total_possible = ?, submitted_at = COALESCE(submitted_at, ?)
        WHERE id = ?
    """, (round(total_score, 2), is_passed, tot_possible, now_str, attempt_id))
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


@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/autograde-all", methods=["POST"])
@quiz_staff_required
def autograde_all_submissions(course_id, quiz_id):
    """
    Teacher & TA Batch Autograde Workflow:
    Evaluates all submitted attempts against question answer keys and computes scores.
    """
    conn = get_db(read_only=True)
    attempts = conn.execute("""
        SELECT id FROM quiz_attempts
        WHERE quiz_id = ? AND status IN ('submitted', 'auto_submitted_violation')
    """, (quiz_id,)).fetchall()
    conn.close()

    count = 0
    for a in attempts:
        perform_quiz_grading(quiz_id, a["id"])
        count += 1

    flash(f"🤖 Successfully auto-graded {count} student submission(s)!", "success")
    return redirect(url_for("quizzes.quiz_submissions", course_id=course_id, quiz_id=quiz_id))


@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/release-grades", methods=["POST"])
@quiz_staff_required
def toggle_release_grades(course_id, quiz_id):
    """
    Teacher & TA Grade Release Gate:
    Controls visibility of numerical scores and pass/fail badges to students,
    and synchronizes them into the official Course Grades table.
    """
    release = int(request.form.get("release", 1))

    def _do_release(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE quizzes SET grades_released = ? WHERE id = ? AND course_id = ?", (release, quiz_id, course_id))
        conn.commit()

    execute_db_write_with_retry(_do_release)
    if release:
        flash("📢 Grades released! Student scorecards and course Grades tables are now updated.", "success")
    else:
        flash("🔒 Grades hidden from students. Assessment is now under instructor review.", "info")
    return redirect(url_for("quizzes.quiz_submissions", course_id=course_id, quiz_id=quiz_id))


@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/submissions/<int:attempt_id>")
@quiz_staff_required
def inspect_submission(course_id, quiz_id, attempt_id):
    """
    Teacher & TA Detailed Submission Inspection & Justification Review:
    Displays each question, student-selected answer, correct answer, student-typed justification,
    and allows granular per-question point adjustments.
    """
    conn = get_db(read_only=True)
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    quiz = conn.execute("SELECT * FROM quizzes WHERE id = ? AND course_id = ?", (quiz_id, course_id)).fetchone()
    if not course or not quiz:
        conn.close()
        abort(404)

    attempt = conn.execute("""
        SELECT qa.*, u.roll_number, u.display_name, u.email
        FROM quiz_attempts qa
        JOIN users u ON qa.student_id = u.id
        WHERE qa.id = ? AND qa.quiz_id = ?
    """, (attempt_id, quiz_id)).fetchone()
    if not attempt:
        conn.close()
        abort(404)

    questions = conn.execute("""
        SELECT * FROM quiz_questions WHERE quiz_id = ? ORDER BY question_number ASC, id ASC
    """, (quiz_id,)).fetchall()

    answers = conn.execute("""
        SELECT * FROM quiz_answers WHERE attempt_id = ?
    """, (attempt_id,)).fetchall()
    conn.close()

    ans_map = {a["question_id"]: dict(a) for a in answers}

    parsed_q = []
    for q in questions:
        q_dict = dict(q)
        try:
            q_dict["options"] = json.loads(q["options_json"] or "[]")
        except Exception:
            q_dict["options"] = []
        try:
            q_dict["variations"] = json.loads(q["acceptable_variations_json"] or "[]")
        except Exception:
            q_dict["variations"] = []

        a_data = ans_map.get(q["id"], {})
        q_dict["student_answer"] = a_data.get("student_answer", "")
        q_dict["justification"] = a_data.get("justification", "")
        q_dict["is_correct"] = a_data.get("is_correct", 0)
        q_dict["points_awarded"] = a_data.get("points_awarded", 0.0)
        parsed_q.append(q_dict)

    strike_log = []
    try:
        strike_log = json.loads(attempt["strike_log_json"] or "[]")
    except Exception:
        strike_log = []

    return render_template(
        "quiz_submission_inspect.html",
        course=course,
        quiz=quiz,
        attempt=attempt,
        questions=parsed_q,
        strike_log=strike_log,
        active_tab="quizzes"
    )


@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/submissions/<int:attempt_id>/update-score", methods=["POST"])
@quiz_staff_required
def update_submission_score(course_id, quiz_id, attempt_id):
    """
    Teacher & TA Question Points Override & Score Adjustment:
    Saves adjusted points for each question based on student justification and recalculates total.
    """
    conn = get_db(read_only=True)
    quiz = conn.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    questions = conn.execute("SELECT id, points FROM quiz_questions WHERE quiz_id = ?", (quiz_id,)).fetchall()
    conn.close()

    total_score = 0.0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _do_update_pts(c):
        nonlocal total_score
        c.execute("BEGIN IMMEDIATE")
        for q in questions:
            qid = q["id"]
            field_name = f"pts_{qid}"
            if field_name in request.form:
                try:
                    pts = float(request.form.get(field_name, 0.0))
                except (ValueError, TypeError):
                    pts = 0.0
                pts = max(0.0, pts)
                is_corr = 1 if pts > 0 else 0
                c.execute("""
                    INSERT INTO quiz_answers (attempt_id, question_id, student_answer, justification, is_correct, points_awarded, saved_at)
                    VALUES (?, ?, '', '', ?, ?, ?)
                    ON CONFLICT(attempt_id, question_id) DO UPDATE SET
                        points_awarded = excluded.points_awarded,
                        is_correct = excluded.is_correct
                """, (attempt_id, qid, is_corr, pts, now_str))
                total_score += pts

        # Recalculate pass status
        tot_possible = float(quiz["total_marks"] or 0.0)
        pass_val = float(quiz["pass_marks"] or 40.0)
        if pass_val > tot_possible and tot_possible > 0:
            pass_threshold = (pass_val / 100.0) * tot_possible
        else:
            pass_threshold = pass_val
        is_passed = 1 if total_score >= pass_threshold else 0

        c.execute("""
            UPDATE quiz_attempts
            SET score = ?, is_passed = ?
            WHERE id = ?
        """, (round(total_score, 2), is_passed, attempt_id))
        c.commit()

    execute_db_write_with_retry(_do_update_pts)
    flash(f"✅ Points successfully saved! Student score updated to {round(total_score, 2)} / {quiz['total_marks']} pts.", "success")
    return redirect(url_for("quizzes.inspect_submission", course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id))


# ==========================================================
# 5. SMART BULK QUIZ IMPORT ENGINE (PDF, JSON, TXT, CSV)
# ==========================================================

@quizzes_bp.route("/courses/<int:course_id>/quizzes/template/download")
@quiz_staff_required
def download_quiz_template(course_id):
    """Provides downloadable sample assessment templates in JSON or plain text format."""
    fmt = request.args.get("format", "json").lower()
    if fmt == "txt":
        content = get_sample_txt_template()
        filename = "hoodle_quiz_template.txt"
        mimetype = "text/plain"
    else:
        sample_dict = get_sample_json_template()
        content = json.dumps(sample_dict, indent=2)
        filename = "hoodle_quiz_template.json"
        mimetype = "application/json"

    return Response(
        content,
        mimetype=mimetype,
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/import/preview", methods=["POST"])
@quiz_staff_required
def import_quiz_preview(course_id, quiz_id):
    """
    Step 1 of Import Wizard:
    Parses an uploaded PDF, JSON, CSV, or Aiken TXT file and returns the
    extracted questions as JSON for client-side review and editing before committing.
    """
    if "quiz_file" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded."}), 400
    file = request.files["quiz_file"]
    if not file.filename:
        return jsonify({"success": False, "error": "Empty filename."}), 400

    try:
        meta, questions = parse_uploaded_quiz_file(file)
        return jsonify({
            "success": True,
            "filename": file.filename,
            "meta": meta,
            "questions": questions,
            "count": len(questions)
        })
    except Exception as e:
        return jsonify({"success": False, "error": f"Failed to parse file: {str(e)}"}), 400


@quizzes_bp.route("/courses/<int:course_id>/quizzes/<int:quiz_id>/import/commit", methods=["POST"])
@quiz_staff_required
def import_quiz_commit(course_id, quiz_id):
    """
    Step 2 of Import Wizard:
    Takes the confirmed & reviewed questions from instructor and persists them
    into quiz_questions table.
    """
    data = request.get_json(silent=True) or {}
    questions = data.get("questions") or []

    if not questions:
        return jsonify({"success": False, "error": "No questions selected for import."}), 400

    def _do_bulk_insert(conn):
        conn.execute("BEGIN IMMEDIATE")
        max_num = conn.execute("SELECT MAX(question_number) as mn FROM quiz_questions WHERE quiz_id = ?", (quiz_id,)).fetchone()["mn"] or 0
        
        inserted_count = 0
        for idx, q in enumerate(questions):
            q_type = q.get("question_type", "mcq")
            prompt = q.get("prompt", "").strip()
            if not prompt:
                continue
            points = float(q.get("points", 1.0))
            code_snippet = q.get("code_snippet", "")
            code_language = q.get("code_language", "python")
            options = q.get("options") or []
            correct_ans = q.get("correct_answer", "")
            variations = q.get("acceptable_variations") or []
            explanation = q.get("explanation", "")

            conn.execute("""
                INSERT INTO quiz_questions (
                    quiz_id, question_number, question_type, prompt, code_snippet,
                    code_language, options_json, correct_answer, acceptable_variations_json,
                    points, explanation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                quiz_id, max_num + idx + 1, q_type, prompt, code_snippet,
                code_language, json.dumps(options), str(correct_ans),
                json.dumps(variations), points, explanation
            ))
            inserted_count += 1

        # Recalculate total quiz marks
        tot = conn.execute("SELECT SUM(points) as sp FROM quiz_questions WHERE quiz_id = ?", (quiz_id,)).fetchone()["sp"] or 0.0
        conn.execute("UPDATE quizzes SET total_marks = ? WHERE id = ?", (tot, quiz_id))
        conn.commit()
        return inserted_count

    count = execute_db_write_with_retry(_do_bulk_insert)
    return jsonify({"success": True, "imported_count": count})


@quizzes_bp.route("/courses/<int:course_id>/quizzes/import/new", methods=["POST"])
@quiz_staff_required
def import_new_quiz_file(course_id):
    """
    Direct Quiz Creation from File:
    Uploads a PDF, JSON, or TXT file, automatically creates the quiz,
    populates all questions, and redirects to the quiz editor!
    """
    if "quiz_file" not in request.files:
        flash("No file provided for quiz import.", "danger")
        return redirect(url_for("quizzes.course_quizzes", course_id=course_id))

    file = request.files["quiz_file"]
    if not file.filename:
        flash("Selected file was empty.", "warning")
        return redirect(url_for("quizzes.course_quizzes", course_id=course_id))

    try:
        meta, questions = parse_uploaded_quiz_file(file)
    except Exception as e:
        flash(f"Error parsing assessment file: {str(e)}", "danger")
        return redirect(url_for("quizzes.course_quizzes", course_id=course_id))

    if not questions:
        flash("No valid questions could be detected in the uploaded file.", "warning")
        return redirect(url_for("quizzes.course_quizzes", course_id=course_id))

    title = meta.get("title") or file.filename.rsplit(".", 1)[0].replace("_", " ").title()
    desc = meta.get("description", "")
    duration = meta.get("duration_minutes", 30)
    pass_marks = meta.get("pass_marks", 40.0)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _do_create_and_import(conn):
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("""
            INSERT INTO quizzes (
                course_id, topic_id, title, description, duration_minutes,
                pass_marks, total_marks, is_published, exam_lockdown,
                shuffle_questions, remote_approval_required, start_time, end_time,
                created_by, created_at
            ) VALUES (?, NULL, ?, ?, ?, ?, 0.0, 1, 1, 1, 1, NULL, NULL, ?, ?)
        """, (course_id, title, desc, duration, pass_marks, session["user_id"], now_str))
        qid = getattr(cur, "lastrowid", None)

        total_pts = 0.0
        for idx, q in enumerate(questions):
            pts = float(q.get("points", 1.0))
            total_pts += pts
            conn.execute("""
                INSERT INTO quiz_questions (
                    quiz_id, question_number, question_type, prompt, code_snippet,
                    code_language, options_json, correct_answer, acceptable_variations_json,
                    points, explanation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                qid, idx + 1, q.get("question_type", "mcq"), q.get("prompt", ""),
                q.get("code_snippet", ""), q.get("code_language", "python"),
                json.dumps(q.get("options") or []), str(q.get("correct_answer", "")),
                json.dumps(q.get("acceptable_variations") or []), pts,
                q.get("explanation", "")
            ))

        conn.execute("UPDATE quizzes SET total_marks = ? WHERE id = ?", (total_pts, qid))
        conn.commit()
        return qid

    new_qid = execute_db_write_with_retry(_do_create_and_import)
    flash(f"🎉 Created quiz '{title}' and successfully imported {len(questions)} questions!", "success")
    return redirect(url_for("quizzes.edit_quiz", course_id=course_id, quiz_id=new_qid))
