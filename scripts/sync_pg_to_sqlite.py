#!/usr/bin/env python3
"""
Reverse Sync Tool: PostgreSQL to SQLite
Used during 1-click rollback to guarantee 0% data loss for any records created while on PostgreSQL.
"""
import os
import sys
import sqlite3
import psycopg2
import psycopg2.extras

SQLITE_PATH = os.environ.get("SQLITE_PATH", "/data/admin/ACCLLMS/accl_lms.db")
PG_URL = os.environ.get("PG_URL", "postgresql://kishan_accl:HoodleLMS_Secure_2026@127.0.0.1:5432/accl_lms")

def sync_to_sqlite():
    print(f"[*] Connecting to PostgreSQL...")
    p_conn = psycopg2.connect(PG_URL)
    p_cursor = p_conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    print(f"[*] Connecting to SQLite at {SQLITE_PATH}...")
    s_conn = sqlite3.connect(SQLITE_PATH)
    s_cursor = s_conn.cursor()

    # Get tables
    s_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence' ORDER BY name;")
    tables = [r[0] for r in s_cursor.fetchall()]

    for tbl in tables:
        # Get column names
        s_cursor.execute(f'PRAGMA table_info("{tbl}");')
        cols = [c[1] for c in s_cursor.fetchall()]
        cols_str = ", ".join(f'"{c}"' for c in cols)
        qmarks = ", ".join(["?"] * len(cols))

        # Read from Postgres
        p_cursor.execute(f'SELECT {cols_str} FROM "{tbl}";')
        pg_rows = p_cursor.fetchall()

        # In SQLite, replace or insert
        s_cursor.execute(f'DELETE FROM "{tbl}";')
        if pg_rows:
            data = [tuple(r[c] for c in cols) for r in pg_rows]
            s_cursor.executemany(f'INSERT INTO "{tbl}" ({cols_str}) VALUES ({qmarks})', data)
        print(f"  -> Synced {len(pg_rows)} rows to SQLite '{tbl}'")

    s_conn.commit()
    s_conn.close()
    p_conn.close()
    print("[SUCCESS] Sync from PostgreSQL back to SQLite completed successfully!")
    return 0

if __name__ == "__main__":
    sys.exit(sync_to_sqlite())
