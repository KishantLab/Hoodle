#!/usr/bin/env python3
"""
Migration script for ACCL Lab Exam Portal on 10.10.14.104
Preserves all 44 CSL100 Lab Exam 1 submissions and moves to new schema.
"""

import os
import shutil
import sqlite3
from pathlib import Path
from datetime import datetime
from werkzeug.security import generate_password_hash

BASE_DIR = Path("/data/admin/lab_exam")
DB_PATH = BASE_DIR / "submissions.db"
OLD_SUB_DIR = BASE_DIR / "submissions"
NEW_COURSE_FOLDER = OLD_SUB_DIR / "CSL100_Lab_Exam_1"

def migrate():
    print("=== Starting CSL100 Exam Migration ===")
    
    # 1. Backup DB
    bak_db = BASE_DIR / f"submissions_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    if DB_PATH.exists():
        shutil.copy2(str(DB_PATH), str(bak_db))
        print(f"Backed up DB to {bak_db}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Read existing submissions
    cursor.execute("SELECT * FROM submissions WHERE is_latest = 1")
    old_records = [dict(row) for row in cursor.fetchall()]
    print(f"Found {len(old_records)} active student submissions from previous schema.")

    # Create new course directory
    NEW_COURSE_FOLDER.mkdir(parents=True, exist_ok=True)

    # 2. Re-create / adjust tables
    # Rename old submissions table
    cursor.execute("ALTER TABLE submissions RENAME TO old_submissions")

    # Create users table
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

    # Create default user kishan / password123
    pwd_hash = generate_password_hash("password123")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT OR IGNORE INTO users (id, username, password_hash, display_name, role, created_at)
        VALUES (1, 'kishan', ?, 'Prof. Kishan', 'admin', ?)
    """, (pwd_hash, now_str))

    # Create exams table
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

    # Insert CSL100 Lab Exam 1
    cursor.execute("""
        INSERT OR REPLACE INTO exams (
            id, slug, course_code, course_name, exam_title, teacher_name,
            created_by_user_id, instructions, folder_name, allowed_types,
            allow_multiple, is_active, logo_filename, created_at
        ) VALUES (
            1, 'csl100-lab-exam-1', 'CSL100', 'Computer Programming Lab',
            'Lab Exam 1', 'Prof. Kishan', 1,
            'Submit your lab exam code in a single .zip file. Multiple submissions allowed; latest file is considered.',
            'CSL100_Lab_Exam_1', 'zip', 1, 1, 'iitbhilai_logo.png', ?
        )
    """, (now_str,))

    # Create new submissions table
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

    # 3. Migrate each student submission
    migrated_count = 0
    for r in old_records:
        roll = r["roll_number"]
        old_file = Path(r["file_path"])
        student_dir = OLD_SUB_DIR / roll
        latest_file = student_dir / f"{roll}_latest.zip"

        source_file = None
        if latest_file.exists():
            source_file = latest_file
        elif old_file.exists():
            source_file = old_file

        if source_file and source_file.exists():
            dest_file = NEW_COURSE_FOLDER / f"{roll}.zip"
            shutil.copy2(str(source_file), str(dest_file))
            size = dest_file.stat().st_size

            cursor.execute("""
                INSERT OR REPLACE INTO submissions (
                    exam_id, roll_number, original_filename, stored_filename,
                    file_path, file_size, sha256, ip_address, submitted_at,
                    version, is_latest
                ) VALUES (
                    1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1
                )
            """, (
                roll, r["original_filename"], f"{roll}.zip",
                str(dest_file), size, r["sha256"], r.get("ip_address", "127.0.0.1"),
                r["submitted_at"], r.get("version", 1)
            ))
            migrated_count += 1

    conn.commit()
    conn.close()
    print(f"Successfully migrated {migrated_count} student files into {NEW_COURSE_FOLDER}")

if __name__ == "__main__":
    migrate()
