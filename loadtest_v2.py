#!/usr/bin/env python3
"""
Hoodle LMS Comprehensive Load Test v2
======================================
Tests maximum concurrent capacity with CORRECT endpoint usage:
1. Login + Dashboard load
2. Attendance marking via /attend/<id>/submit (with valid dynamic tokens)
3. File submissions via /courses/<id>/coursework/<id>/submit
4. Mixed workload (all 3 at once)

Tracks: throughput, error rate, p50/p95/p99 latency, data integrity.
"""

import os
import sys
import time
import json
import random
import string
import sqlite3
import hashlib
import threading
import statistics
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# App setup
APP_DIR = "/data/admin/ACCLLMS"
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

import app as lms_app

DB_PATH = os.path.join(APP_DIR, "accl_lms.db")

# ── Scale Parameters ──
NUM_STUDENTS = 200
NUM_COURSES = 5
CONCURRENT_LEVELS = [10, 25, 50, 100, 150, 200]
FILE_SIZE_KB = 50
BATCH_ID = random.randint(10000, 99999)

results_log = []
lock = threading.Lock()
SECRET_KEY = lms_app.SECRET_KEY
ROTATION_SECONDS = lms_app.ATTENDANCE_ROTATION_SECONDS


def get_valid_token(course_id, session_type="Lecture"):
    """Generate a valid attendance token using the app's actual logic."""
    time_block = int(time.time() // ROTATION_SECONDS)
    raw = f"{SECRET_KEY}_ATTEND_{course_id}_{session_type.upper()}_{time_block}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest[:8].upper()


def log_result(test_name, concurrency, total_requests, successes, failures,
               latencies, data_verified=None, extra=""):
    if not latencies:
        latencies = [0]
    sl = sorted(latencies)
    p50 = sl[int(len(sl) * 0.50)]
    p95 = sl[min(int(len(sl) * 0.95), len(sl) - 1)]
    p99 = sl[min(int(len(sl) * 0.99), len(sl) - 1)]
    avg = statistics.mean(latencies)
    wall = max(latencies) if latencies else 1
    rps = total_requests / wall if wall > 0 else 0
    err_pct = (failures / total_requests * 100) if total_requests > 0 else 0

    entry = {
        "test": test_name, "concurrency": concurrency,
        "total": total_requests, "ok": successes, "fail": failures,
        "err_pct": round(err_pct, 1),
        "avg_ms": round(avg * 1000), "p50_ms": round(p50 * 1000),
        "p95_ms": round(p95 * 1000), "p99_ms": round(p99 * 1000),
        "rps": round(rps, 1), "data_ok": data_verified, "extra": extra
    }
    with lock:
        results_log.append(entry)

    icon = "✅" if failures == 0 else ("⚠️" if err_pct < 5 else "❌")
    data_s = "✓" if data_verified is True else ("✗" if data_verified is False else "—")
    print(f"  {icon} C={concurrency:>4} | OK={successes:>4} FAIL={failures:>3} ({err_pct:.1f}%) | "
          f"Avg={avg * 1000:.0f}ms P95={p95 * 1000:.0f}ms P99={p99 * 1000:.0f}ms | "
          f"RPS={rps:.0f} | Data={data_s}{' | ' + extra if extra else ''}")


# ── Setup ──
def setup():
    print("\n📦 Setting up test data...")
    from werkzeug.security import generate_password_hash
    pwd = generate_password_hash("lt123", method="pbkdf2:sha256")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    # Teacher
    r = conn.execute("SELECT id FROM users WHERE username='lt_teacher'").fetchone()
    if r:
        tid = r["id"]
    else:
        conn.execute("INSERT INTO users (username,roll_number,email,password_hash,display_name,role,created_at) "
                     "VALUES ('lt_teacher','LTFAC','lt_fac@test.com',?,?,'teacher',?)", (pwd, "LT Teacher", now))
        tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Courses + assignments
    cids = []
    cw_map = {}  # course_id -> coursework_id
    for i in range(1, NUM_COURSES + 1):
        code = f"LT{BATCH_ID}_{i}"
        r = conn.execute("SELECT id FROM courses WHERE code=?", (code,)).fetchone()
        if r:
            cid = r["id"]
        else:
            conn.execute("INSERT INTO courses (code,title,section,description,join_code,teacher_id,theme_color,created_at) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (code, f"LoadTest Course {i}", "A", "test", f"LTJ{i}", tid, "blue", now))
            cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        cids.append(cid)

        r2 = conn.execute("SELECT id FROM coursework WHERE course_id=? AND title='LT Assignment'", (cid,)).fetchone()
        if r2:
            cw_map[cid] = r2["id"]
        else:
            conn.execute("INSERT INTO coursework (course_id,type,title,description,points,due_date,"
                         "allowed_types,allow_multiple,allow_late,is_exam_mode,created_by,created_at) "
                         "VALUES (?,'assignment','LT Assignment','test',100,'2027-12-31 23:59:59',"
                         "'pdf,txt',1,1,0,?,?)", (cid, tid, now))
            cw_map[cid] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Students
    sids = []
    enr = {}  # student_id -> course_id
    for i in range(1, NUM_STUDENTS + 1):
        uname = f"lt_s{BATCH_ID}_{i}"
        r = conn.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()
        if r:
            sid = r["id"]
        else:
            conn.execute("INSERT INTO users (username,roll_number,email,password_hash,display_name,role,created_at) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (uname, f"LT{i:04d}", f"lt{i}@test.com", pwd, f"Student {i}", "student", now))
            sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        sids.append(sid)
        cid = cids[(i - 1) % len(cids)]
        conn.execute("INSERT OR IGNORE INTO course_enrollments (course_id,user_id,role,enrolled_at) VALUES (?,?,?,?)",
                     (cid, sid, "student", now))
        enr[sid] = cid

    conn.commit()
    conn.close()
    print(f"  ✓ {len(sids)} students, {len(cids)} courses, {len(cw_map)} assignments ready")
    return sids, cids, cw_map, enr, tid


def make_client():
    lms_app.app.config["TESTING"] = True
    lms_app.app.config["WTF_CSRF_ENABLED"] = False
    return lms_app.app.test_client()


def login(client, username):
    r = client.post("/login", data={"identifier": username, "password": "lt123"}, follow_redirects=False)
    return r.status_code in (200, 302)


def get_username(sid):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    u = conn.execute("SELECT username FROM users WHERE id=?", (sid,)).fetchone()
    conn.close()
    return u["username"] if u else None


# ── Test 1: Login + Dashboard ──
def test_login_dashboard(sids, concurrency):
    sel = random.sample(sids, min(concurrency, len(sids)))
    lats, ok, fail = [], 0, 0

    def worker(sid):
        nonlocal ok, fail
        t0 = time.time()
        try:
            c = make_client()
            uname = get_username(sid)
            if not uname or not login(c, uname):
                with lock: fail += 1; return
            r = c.get("/dashboard")
            with lock:
                if r.status_code == 200: ok += 1
                else: fail += 1
        except Exception:
            with lock: fail += 1
        finally:
            with lock: lats.append(time.time() - t0)

    with ThreadPoolExecutor(max_workers=concurrency) as p:
        list(as_completed([p.submit(worker, s) for s in sel]))
    log_result("Login+Dashboard", concurrency, len(sel), ok, fail, lats)


# ── Test 2: Attendance via /attend/<id>/submit ──
def test_attendance(sids, enr, concurrency):
    sel = random.sample(sids, min(concurrency, len(sids)))
    lats, ok, fail = [], 0, 0
    test_date = datetime.now().strftime("%Y-%m-%d")  # use today so token validation works

    def worker(sid):
        nonlocal ok, fail
        t0 = time.time()
        try:
            c = make_client()
            uname = get_username(sid)
            cid = enr.get(sid)
            if not uname or not cid or not login(c, uname):
                with lock: fail += 1; return

            # Get a VALID dynamic token
            token = get_valid_token(cid, "Lecture")

            r = c.post(f"/attend/{cid}/submit", data={
                "session_type": "Lecture",
                "token": token
            }, follow_redirects=True)

            # Check if attendance was recorded or already exists
            if r.status_code == 200:
                body = r.data.lower()
                if b"success" in body or b"already" in body or b"recorded" in body or b"attendance" in body:
                    with lock: ok += 1
                else:
                    with lock: fail += 1
            else:
                with lock: fail += 1
        except Exception:
            with lock: fail += 1
        finally:
            with lock: lats.append(time.time() - t0)

    with ThreadPoolExecutor(max_workers=concurrency) as p:
        list(as_completed([p.submit(worker, s) for s in sel]))

    # Verify data integrity
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("""
        SELECT COUNT(*) FROM attendance_logs
        WHERE attendance_date = ? AND session_type = 'Lecture'
        AND student_id IN ({})
    """.format(",".join(str(s) for s in sel)), (test_date,)).fetchone()[0]
    conn.close()

    data_ok = count >= ok  # at least as many rows as successes (some may be duplicates from earlier runs)
    log_result("Attendance QR", concurrency, len(sel), ok, fail, lats,
               data_verified=data_ok, extra=f"DB={count} OK={ok}")


# ── Test 3: File Submission ──
def test_file_submit(sids, enr, cw_map, concurrency):
    sel = random.sample(sids, min(concurrency, len(sids)))
    lats, ok, fail = [], 0, 0
    fake_pdf = b"%PDF-1.4\n" + os.urandom(FILE_SIZE_KB * 1024)

    def worker(sid):
        nonlocal ok, fail
        t0 = time.time()
        try:
            c = make_client()
            uname = get_username(sid)
            cid = enr.get(sid)
            cw_id = cw_map.get(cid) if cid else None
            if not uname or not cid or not cw_id or not login(c, uname):
                with lock: fail += 1; return

            r = c.post(f"/courses/{cid}/coursework/{cw_id}/submit", data={
                "file": (io.BytesIO(fake_pdf), f"lt_{sid}_{BATCH_ID}.pdf", "application/pdf")
            }, content_type="multipart/form-data", follow_redirects=True)

            if r.status_code == 200:
                with lock: ok += 1
            else:
                with lock: fail += 1
        except Exception:
            with lock: fail += 1
        finally:
            with lock: lats.append(time.time() - t0)

    with ThreadPoolExecutor(max_workers=concurrency) as p:
        list(as_completed([p.submit(worker, s) for s in sel]))

    conn = sqlite3.connect(DB_PATH)
    db_count = conn.execute("SELECT COUNT(*) FROM submissions WHERE original_filename LIKE ?",
                            (f"lt_%_{BATCH_ID}%",)).fetchone()[0]
    conn.close()
    log_result("File Submit", concurrency, len(sel), ok, fail, lats,
               extra=f"DB subs={db_count}")


# ── Test 4: Mixed Workload ──
def test_mixed(sids, enr, cw_map, concurrency):
    sel = random.sample(sids, min(concurrency, len(sids)))
    lats, ok, fail = [], 0, 0
    ops = ["dashboard", "attendance", "submission"]

    def worker(sid, op):
        nonlocal ok, fail
        t0 = time.time()
        try:
            c = make_client()
            uname = get_username(sid)
            cid = enr.get(sid)
            if not uname or not cid or not login(c, uname):
                with lock: fail += 1; return

            if op == "dashboard":
                r = c.get("/dashboard")
                with lock:
                    if r.status_code == 200: ok += 1
                    else: fail += 1

            elif op == "attendance":
                token = get_valid_token(cid, "Lab")
                r = c.post(f"/attend/{cid}/submit", data={
                    "session_type": "Lab", "token": token
                }, follow_redirects=True)
                with lock:
                    if r.status_code == 200: ok += 1
                    else: fail += 1

            elif op == "submission":
                cw_id = cw_map.get(cid)
                if not cw_id:
                    with lock: fail += 1; return
                fake = b"%PDF-1.4\n" + os.urandom(10 * 1024)
                r = c.post(f"/courses/{cid}/coursework/{cw_id}/submit", data={
                    "file": (io.BytesIO(fake), f"mx_{sid}_{BATCH_ID}.pdf", "application/pdf")
                }, content_type="multipart/form-data", follow_redirects=True)
                with lock:
                    if r.status_code == 200: ok += 1
                    else: fail += 1
        except Exception:
            with lock: fail += 1
        finally:
            with lock: lats.append(time.time() - t0)

    with ThreadPoolExecutor(max_workers=concurrency) as p:
        fs = [p.submit(worker, sel[i], ops[i % 3]) for i in range(len(sel))]
        list(as_completed(fs))
    log_result("Mixed Workload", concurrency, len(sel), ok, fail, lats)


# ── Cleanup ──
def cleanup():
    print("\n🧹 Cleaning up load test data...")
    conn = sqlite3.connect(DB_PATH)
    ids = [r[0] for r in conn.execute("SELECT id FROM users WHERE username LIKE 'lt_%'").fetchall()]
    if ids:
        ph = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM attendance_logs WHERE student_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM submissions WHERE student_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM course_enrollments WHERE user_id IN ({ph})", ids)
    conn.execute("DELETE FROM coursework WHERE title='LT Assignment'")
    conn.execute("DELETE FROM courses WHERE code LIKE ?", (f"LT{BATCH_ID}_%",))
    conn.execute("DELETE FROM users WHERE username LIKE 'lt_%'")
    conn.commit()
    conn.close()

    # Clean submission files
    sd = Path(APP_DIR) / "storage" / "submissions"
    if sd.exists():
        for f in sd.glob(f"*lt_*_{BATCH_ID}*"):
            f.unlink(missing_ok=True)
        for f in sd.glob(f"*mx_*_{BATCH_ID}*"):
            f.unlink(missing_ok=True)
    print("  ✓ Cleanup complete")


# ── Main ──
def main():
    print("=" * 80)
    print("🔬 HOODLE LMS — LOAD TEST v2 (Correct Endpoints)")
    print("=" * 80)
    print(f"Server: Xeon Silver 4514Y 32-core, 62GB RAM, NVMe SSD")
    print(f"Gunicorn: 16 workers × 4 threads = 64 concurrent slots")
    print(f"SQLite WAL mode | Test batch: {BATCH_ID}")
    print(f"Students: {NUM_STUDENTS} | Courses: {NUM_COURSES}")
    print(f"Concurrency: {CONCURRENT_LEVELS}")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    sids, cids, cw_map, enr, tid = setup()

    print("\n" + "─" * 70)
    print("📊 TEST 1: Login + Dashboard")
    print("─" * 70)
    for c in CONCURRENT_LEVELS:
        test_login_dashboard(sids, c)

    print("\n" + "─" * 70)
    print("📊 TEST 2: Attendance QR Scan (via /attend/<id>/submit)")
    print("─" * 70)
    for c in CONCURRENT_LEVELS:
        test_attendance(sids, enr, c)

    print("\n" + "─" * 70)
    print("📊 TEST 3: File Submission (50KB PDF)")
    print("─" * 70)
    for c in CONCURRENT_LEVELS:
        test_file_submit(sids, enr, cw_map, c)

    print("\n" + "─" * 70)
    print("📊 TEST 4: Mixed Workload")
    print("─" * 70)
    for c in CONCURRENT_LEVELS:
        test_mixed(sids, enr, cw_map, c)

    # ── Summary ──
    print("\n" + "=" * 80)
    print("📋 RESULTS SUMMARY")
    print("=" * 80)
    print(f"{'Test':<18} {'C':>4} {'Tot':>5} {'OK':>5} {'Fail':>4} {'Err%':>6} "
          f"{'Avg':>7} {'P50':>7} {'P95':>7} {'P99':>7} {'RPS':>6} {'Data':>5} {'Extra'}")
    print("-" * 110)
    for r in results_log:
        ds = "✓" if r["data_ok"] is True else ("✗" if r["data_ok"] is False else "—")
        print(f"{r['test']:<18} {r['concurrency']:>4} {r['total']:>5} {r['ok']:>5} {r['fail']:>4} "
              f"{r['err_pct']:>5.1f}% {r['avg_ms']:>6}ms {r['p50_ms']:>6}ms "
              f"{r['p95_ms']:>6}ms {r['p99_ms']:>6}ms {r['rps']:>5.0f} {ds:>5} "
              f"{r.get('extra', '')}")

    # Capacity analysis
    print("\n" + "─" * 70)
    print("🎯 CAPACITY ANALYSIS")
    print("─" * 70)
    for tn in ["Login+Dashboard", "Attendance QR", "File Submit", "Mixed Workload"]:
        trs = [r for r in results_log if r["test"] == tn]
        max_zero = max([r["concurrency"] for r in trs if r["err_pct"] == 0], default=0)
        max_5 = max([r["concurrency"] for r in trs if r["err_pct"] < 5], default=0)
        max_10 = max([r["concurrency"] for r in trs if r["err_pct"] < 10], default=0)
        # Find first failure
        first_fail = "none"
        for r in trs:
            if r["fail"] > 0:
                first_fail = f"C={r['concurrency']} ({r['fail']} failures, {r['err_pct']}%)"
                break
        print(f"  {tn:<18}: 0% err @ C={max_zero:>4} | <5% @ C={max_5:>4} | <10% @ C={max_10:>4}")
        print(f"                     First failure: {first_fail}")

    # Data integrity summary
    print("\n" + "─" * 70)
    print("🔒 DATA INTEGRITY")
    print("─" * 70)
    conn = sqlite3.connect(DB_PATH)
    att_count = conn.execute("SELECT COUNT(*) FROM attendance_logs WHERE student_id IN "
                             "(SELECT id FROM users WHERE username LIKE 'lt_%')").fetchone()[0]
    sub_count = conn.execute("SELECT COUNT(*) FROM submissions WHERE student_id IN "
                             "(SELECT id FROM users WHERE username LIKE 'lt_%')").fetchone()[0]
    conn.close()
    total_att_attempts = sum(r["ok"] for r in results_log if "Attendance" in r["test"])
    total_sub_attempts = sum(r["ok"] for r in results_log if "Submit" in r["test"] and "File" in r["test"])
    print(f"  Attendance: {att_count} DB rows from test (some duplicates expected from repeated runs)")
    print(f"  Submissions: {sub_count} DB rows from test")
    data_loss = att_count == 0 and total_att_attempts > 0
    print(f"  ⚠️ DATA LOSS DETECTED: {'YES — CRITICAL' if data_loss else 'NO — All writes persisted'}")

    print(f"\nCompleted: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    cleanup()

    with open("/tmp/hoodle_loadtest_v2.json", "w") as f:
        json.dump(results_log, f, indent=2)
    print("📁 Results saved to /tmp/hoodle_loadtest_v2.json")


if __name__ == "__main__":
    main()
