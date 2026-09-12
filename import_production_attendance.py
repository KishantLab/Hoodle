import sqlite3
from datetime import datetime
from werkzeug.security import generate_password_hash

raw_data = """9/9/2026	b26cs021@iitbhilai.ac.in	Batch 1	Lecture	PRESENT	b26cs021@iitbhilai.ac.in_LECTURE_2026-09-09
9/9/2026	b26cs034@iitbhilai.ac.in	Batch 1	Lecture	PRESENT	b26cs034@iitbhilai.ac.in_LECTURE_2026-09-09
9/9/2026	b26cs002@iitbhilai.ac.in	Batch 1	Lecture	PRESENT	b26cs002@iitbhilai.ac.in_LECTURE_2026-09-09
9/9/2026	b26cs047@iitbhilai.ac.in	Batch 1	Lecture	PRESENT	b26cs047@iitbhilai.ac.in_LECTURE_2026-09-09
9/9/2026	b26cs036@iitbhilai.ac.in	Batch 1	Lecture	PRESENT	b26cs036@iitbhilai.ac.in_LECTURE_2026-09-09
9/9/2026	b26cs038@iitbhilai.ac.in	Batch 1	Lecture	PRESENT	b26cs038@iitbhilai.ac.in_LECTURE_2026-09-09
9/9/2026	s26ma021@iitbhilai.ac.in	M.Sc Maths	Lecture	PRESENT	s26ma021@iitbhilai.ac.in_LECTURE_2026-09-09
9/9/2026	b26cs048@iitbhilai.ac.in	Batch 1	Lecture	PRESENT	b26cs048@iitbhilai.ac.in_LECTURE_2026-09-09
9/9/2026	b26cs025@iitbhilai.ac.in	Batch 1	Lecture	PRESENT	b26cs025@iitbhilai.ac.in_LECTURE_2026-09-09
9/9/2026	b26cs050@iitbhilai.ac.in	Batch 1	Lecture	PRESENT	b26cs050@iitbhilai.ac.in_LECTURE_2026-09-09
9/9/2026	b26cs004@iitbhilai.ac.in	Batch 1	Lecture	PRESENT	b26cs004@iitbhilai.ac.in_LECTURE_2026-09-09
9/9/2026	b26cs040@iitbhilai.ac.in	Batch 1	Lecture	PRESENT	b26cs040@iitbhilai.ac.in_LECTURE_2026-09-09"""

conn = sqlite3.connect('/data/admin/ACCLLMS/accl_lms.db')
conn.row_factory = sqlite3.Row

target_course_ids = [3, 2]
now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

for course_id in target_course_ids:
    c_row = conn.execute('SELECT id, code, title, teacher_id, section FROM courses WHERE id = ?', (course_id,)).fetchone()
    if not c_row:
        continue
    print(f"Processing Course {course_id}: {c_row['code']} - {c_row['title']}")

    sess = conn.execute('SELECT id FROM attendance_sessions WHERE course_id = ? AND session_date = ? AND session_type = ?', (course_id, '2026-09-09', 'Lecture')).fetchone()
    if not sess:
        conn.execute('''
            INSERT INTO attendance_sessions (course_id, title, session_type, session_date, start_time, end_time, is_active, created_by, created_at)
            VALUES (?, 'Lecture - 2026-09-09', 'Lecture', '2026-09-09', '09:00', '10:00', 0, ?, ?)
        ''', (course_id, c_row['teacher_id'], now_str))
        session_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    else:
        session_id = sess['id']

    for line in raw_data.strip().splitlines():
        parts = [p.strip() for p in line.split('\t')]
        date_str = '2026-09-09'
        email = parts[1].strip()
        section = parts[2].strip()
        session_type = parts[3].strip()
        status = parts[4].strip()

        username = email.split('@')[0].lower()
        roll_number = username.upper()

        u = conn.execute('SELECT id, username, roll_number, display_name FROM users WHERE LOWER(email) = LOWER(?) OR LOWER(roll_number) = LOWER(?) OR LOWER(username) = LOWER(?)', (email, roll_number, username)).fetchone()
        if not u:
            pwd_hash = generate_password_hash(username, method='pbkdf2:sha256')
            conn.execute('''
                INSERT INTO users (username, roll_number, email, password_hash, display_name, role, must_change_password, created_at)
                VALUES (?, ?, ?, ?, ?, 'student', 1, ?)
            ''', (username, roll_number, email.lower(), pwd_hash, roll_number, now_str))
            user_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            display_name = roll_number
        else:
            user_id = u['id']
            display_name = u['display_name']
            roll_number = u['roll_number'] or username.upper()

        enr = conn.execute('SELECT 1 FROM course_enrollments WHERE course_id = ? AND user_id = ?', (course_id, user_id)).fetchone()
        if not enr:
            conn.execute('''
                INSERT INTO course_enrollments (course_id, user_id, role, enrolled_at)
                VALUES (?, ?, 'student', ?)
            ''', (course_id, user_id, now_str))

        target_key = f"{user_id}_{session_type.upper()}_{date_str}"
        conn.execute('''
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
        ''', (course_id, session_id, user_id, roll_number, display_name, section, session_type, date_str, status, now_str, target_key))

conn.commit()

for course_id in target_course_ids:
    total_att = conn.execute('SELECT COUNT(*) as cnt FROM attendance_logs WHERE course_id = ?', (course_id,)).fetchone()['cnt']
    total_enr = conn.execute('SELECT COUNT(*) as cnt FROM course_enrollments WHERE course_id = ? AND role = "student"', (course_id,)).fetchone()['cnt']
    print(f"Result for Course {course_id}: {total_enr} enrolled students, {total_att} attendance logs recorded.")

conn.close()
