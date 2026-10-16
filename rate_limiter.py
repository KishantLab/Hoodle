"""
rate_limiter.py - Distributed Rate Limiter for Hoodle LMS Authentication
Protects /login, /forgot-password, and /reset-password from brute-force password guessing,
credential stuffing, and OTP enumeration.
Synchronized across the entire multi-node cluster (master, gpu1, gpu2) via PostgreSQL.
"""

import time
import math
import logging
from datetime import datetime, timedelta
from typing import Tuple, Optional
from flask import request

logger = logging.getLogger("hoodle.security.rate_limiter")

# Rate limit configuration thresholds
CONFIG = {
    # Login limits
    "login_ip": {
        "max_attempts": 10,       # 10 failed login attempts from same IP
        "window_seconds": 600,    # within 10 minutes
        "lockout_seconds": 900,   # 15 minutes lockout
    },
    "login_user": {
        "max_attempts": 5,        # 5 failed attempts for same username/roll_number
        "window_seconds": 600,    # within 10 minutes
        "lockout_seconds": 900,   # 15 minutes lockout
    },
    # Forgot password limits
    "forgot_ip": {
        "max_attempts": 5,        # 5 forgot password requests from same IP
        "window_seconds": 900,    # within 15 minutes
        "lockout_seconds": 900,   # 15 minutes lockout
    },
    "forgot_user": {
        "max_attempts": 3,        # 3 forgot password requests for same account
        "window_seconds": 900,    # within 15 minutes
        "lockout_seconds": 900,   # 15 minutes lockout
    },
    # Reset OTP verification limits
    "reset_otp": {
        "max_attempts": 5,        # 5 incorrect OTP attempts
        "window_seconds": 900,    # within 15 minutes
        "lockout_seconds": 900,   # 15 minutes lockout
    }
}


def get_real_client_ip() -> str:
    """
    Extracts the true client IP address, properly handling reverse proxies
    (Nginx / HAProxy / Cloudflare) and campus network gateways.
    """
    # 1. Check X-Forwarded-For header
    xff = request.headers.get("X-Forwarded-For", "").strip()
    if xff:
        # Take the leftmost IP (the original client IP)
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[0]

    # 2. Check X-Real-IP
    x_real_ip = request.headers.get("X-Real-IP", "").strip()
    if x_real_ip:
        return x_real_ip

    # 3. Fallback to Flask remote_addr
    return request.remote_addr or "127.0.0.1"


def init_rate_limiter_db(conn):
    """Initializes the auth_rate_limits table if not already created."""
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auth_rate_limits (
                id SERIAL PRIMARY KEY,
                scope VARCHAR(64) NOT NULL,
                key_value VARCHAR(255) NOT NULL,
                attempts INT DEFAULT 1,
                first_attempt TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_attempt TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                locked_until TIMESTAMP NULL,
                UNIQUE(scope, key_value)
            );
        """)
        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rate_limits_scope_key ON auth_rate_limits(scope, key_value);")
        except Exception:
            pass
        conn.commit()
    except Exception as e:
        logger.warning(f"Notice during init_rate_limiter_db: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


def check_rate_limit(scope: str, key_value: str, conn) -> Tuple[bool, int]:
    """
    Checks if an action is currently blocked by rate limiting.
    Returns (is_allowed: bool, retry_after_seconds: int).
    """
    if not key_value:
        return True, 0

    key_norm = str(key_value).strip().lower()
    cfg = CONFIG.get(scope, {
        "max_attempts": 5,
        "window_seconds": 600,
        "lockout_seconds": 900
    })

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT attempts, first_attempt, last_attempt, locked_until
            FROM auth_rate_limits
            WHERE scope = ? AND key_value = ?
        """, (scope, key_norm))
        row = cur.fetchone()

        if not row:
            return True, 0

        # Handle tuple or sqlite3.Row/dict
        attempts = row[0] if isinstance(row, (tuple, list)) else row["attempts"]
        first_attempt = row[1] if isinstance(row, (tuple, list)) else row["first_attempt"]
        locked_until = row[3] if isinstance(row, (tuple, list)) else row["locked_until"]

        now = datetime.now()

        # Parse timestamps if string
        if isinstance(locked_until, str):
            try:
                locked_until = datetime.strptime(locked_until.split(".")[0], "%Y-%m-%d %H:%M:%S")
            except Exception:
                locked_until = None

        if isinstance(first_attempt, str):
            try:
                first_attempt = datetime.strptime(first_attempt.split(".")[0], "%Y-%m-%d %H:%M:%S")
            except Exception:
                first_attempt = now

        # If locked_until is active and in future
        if locked_until and locked_until > now:
            remaining = math.ceil((locked_until - now).total_seconds())
            return False, max(1, remaining)

        # If sliding window expired, user is free
        if (now - first_attempt).total_seconds() > cfg["window_seconds"]:
            # Window expired, reset record
            cur.execute("""
                DELETE FROM auth_rate_limits
                WHERE scope = ? AND key_value = ?
            """, (scope, key_norm))
            conn.commit()
            return True, 0

        # Check if attempts exceeded max
        if attempts >= cfg["max_attempts"]:
            lock_until_dt = now + timedelta(seconds=cfg["lockout_seconds"])
            cur.execute("""
                UPDATE auth_rate_limits
                SET locked_until = ?
                WHERE scope = ? AND key_value = ?
            """, (lock_until_dt.strftime("%Y-%m-%d %H:%M:%S"), scope, key_norm))
            conn.commit()
            return False, cfg["lockout_seconds"]

        return True, 0
    except Exception as e:
        logger.error(f"Error checking rate limit: {e}")
        return True, 0


def record_failed_attempt(scope: str, key_value: str, conn) -> Tuple[bool, int, int]:
    """
    Records a failed attempt.
    Returns (is_locked: bool, remaining_attempts: int, retry_after_seconds: int).
    """
    if not key_value:
        return False, 5, 0

    key_norm = str(key_value).strip().lower()
    cfg = CONFIG.get(scope, {
        "max_attempts": 5,
        "window_seconds": 600,
        "lockout_seconds": 900
    })

    try:
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.cursor()

        cur.execute("""
            SELECT id, attempts, first_attempt, locked_until
            FROM auth_rate_limits
            WHERE scope = ? AND key_value = ?
        """, (scope, key_norm))
        row = cur.fetchone()

        if not row:
            # First failed attempt
            cur.execute("""
                INSERT INTO auth_rate_limits (scope, key_value, attempts, first_attempt, last_attempt, locked_until)
                VALUES (?, ?, 1, ?, ?, NULL)
            """, (scope, key_norm, now_str, now_str))
            conn.commit()
            remaining = max(0, cfg["max_attempts"] - 1)
            return False, remaining, 0

        rec_id = row[0] if isinstance(row, (tuple, list)) else row["id"]
        attempts = row[1] if isinstance(row, (tuple, list)) else row["attempts"]
        first_attempt = row[2] if isinstance(row, (tuple, list)) else row["first_attempt"]

        if isinstance(first_attempt, str):
            try:
                first_attempt = datetime.strptime(first_attempt.split(".")[0], "%Y-%m-%d %H:%M:%S")
            except Exception:
                first_attempt = now

        # If window expired, restart counter
        if (now - first_attempt).total_seconds() > cfg["window_seconds"]:
            cur.execute("""
                UPDATE auth_rate_limits
                SET attempts = 1, first_attempt = ?, last_attempt = ?, locked_until = NULL
                WHERE id = ?
            """, (now_str, now_str, rec_id))
            conn.commit()
            return False, cfg["max_attempts"] - 1, 0

        # Increment attempts
        new_attempts = attempts + 1
        if new_attempts >= cfg["max_attempts"]:
            # Trigger lockout
            lock_until_dt = now + timedelta(seconds=cfg["lockout_seconds"])
            lock_str = lock_until_dt.strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("""
                UPDATE auth_rate_limits
                SET attempts = ?, last_attempt = ?, locked_until = ?
                WHERE id = ?
            """, (new_attempts, now_str, lock_str, rec_id))
            conn.commit()
            return True, 0, cfg["lockout_seconds"]
        else:
            cur.execute("""
                UPDATE auth_rate_limits
                SET attempts = ?, last_attempt = ?
                WHERE id = ?
            """, (new_attempts, now_str, rec_id))
            conn.commit()
            remaining = max(0, cfg["max_attempts"] - new_attempts)
            return False, remaining, 0

    except Exception as e:
        logger.error(f"Error recording failed attempt: {e}")
        return False, 1, 0


def clear_rate_limit(scope: str, key_value: str, conn):
    """Clears the rate limit record (e.g. upon successful authentication)."""
    if not key_value:
        return
    try:
        key_norm = str(key_value).strip().lower()
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM auth_rate_limits
            WHERE scope = ? AND key_value = ?
        """, (scope, key_norm))
        conn.commit()
    except Exception as e:
        logger.warning(f"Error clearing rate limit: {e}")


def cleanup_expired_limits(conn):
    """Housekeeping job to remove stale rate limit records older than 24 hours."""
    try:
        cutoff = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM auth_rate_limits
            WHERE last_attempt < ? AND (locked_until IS NULL OR locked_until < ?)
        """, (cutoff, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    except Exception as e:
        logger.warning(f"Error cleaning up expired rate limits: {e}")
