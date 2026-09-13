"""
db_adapter.py - Dual-Engine Database Adapter for Hoodle LMS
Supports both PostgreSQL (multi-node cluster with ThreadedConnectionPool)
and SQLite (local single-node with zero regression / 1-click instant rollback).
"""

import os
import re
import time
import sqlite3
import random
import logging

logger = logging.getLogger("hoodle.db")

# Detect backend configuration from environment
# DATABASE_BACKEND can be 'postgres' or 'sqlite' (defaults to 'sqlite' for zero breaking changes)
DATABASE_BACKEND = os.environ.get("DATABASE_BACKEND", "").lower().strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if not DATABASE_BACKEND:
    if DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://"):
        DATABASE_BACKEND = "postgres"
    else:
        DATABASE_BACKEND = "sqlite"

# SQLite configuration
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "accl_lms.db"))

# PostgreSQL configuration and connection pool
pg_pool = None

if DATABASE_BACKEND == "postgres":
    try:
        import psycopg2
        import psycopg2.pool
        import psycopg2.extras

        # Allow standard postgres:// URL by normalizing to postgresql://
        normalized_url = DATABASE_URL
        if normalized_url.startswith("postgres://"):
            normalized_url = "postgresql://" + normalized_url[len("postgres://"):]

        # Initialize thread-safe connection pool (min 4, max 64 connections per node)
        pg_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=64,
            dsn=normalized_url
        )
        logger.info("PostgreSQL connection pool initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize PostgreSQL pool: {e}. Falling back to SQLite.")
        DATABASE_BACKEND = "sqlite"


class DummyCursor:
    """No-op cursor for SQLite-specific PRAGMA commands in PostgreSQL mode."""
    def __init__(self):
        self.lastrowid = None
        self.rowcount = 0
        self.description = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, size=None):
        return []

    def close(self):
        pass


def translate_sql(sql):
    """
    Translates SQLite query syntax into PostgreSQL dialect:
    1. Replaces '?' placeholders with '%s' (ignoring literals in single quotes).
    2. Translates 'INSERT OR IGNORE INTO' to 'ON CONFLICT DO NOTHING'.
    3. Translates 'INSERT OR REPLACE INTO system_settings' to upsert on conflict key.
    """
    out = []
    in_quote = False
    for ch in sql:
        if ch == "'":
            in_quote = not in_quote
            out.append(ch)
        elif ch == "?" and not in_quote:
            out.append("%s")
        else:
            out.append(ch)
    result = "".join(out)

    # Convert INSERT OR IGNORE INTO table ... -> INSERT INTO table ... ON CONFLICT DO NOTHING
    if re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", result, re.IGNORECASE):
        result = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", result, flags=re.IGNORECASE)
        result += " ON CONFLICT DO NOTHING"

    # Convert INSERT OR REPLACE INTO system_settings ... -> INSERT INTO system_settings ... ON CONFLICT (key) DO UPDATE
    if "system_settings" in result and re.search(r"INSERT\s+OR\s+REPLACE\s+INTO", result, re.IGNORECASE):
        result = re.sub(r"INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", result, flags=re.IGNORECASE)
        result += " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at"

    # Convert INTEGER PRIMARY KEY AUTOINCREMENT -> SERIAL PRIMARY KEY for PostgreSQL DDL
    if re.search(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", result, re.IGNORECASE):
        result = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "SERIAL PRIMARY KEY", result, flags=re.IGNORECASE)

    return result


class PgCursorWrapper:
    def __init__(self, raw_cursor, conn_wrapper):
        self._cur = raw_cursor
        self._conn = conn_wrapper
        self.lastrowid = None

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    def execute(self, sql, params=None):
        sql_clean = sql.strip()
        if sql_clean.upper().startswith("PRAGMA") or sql_clean.upper().startswith("BEGIN"):
            return DummyCursor()

        translated_sql = translate_sql(sql)
        is_insert = translated_sql.strip().upper().startswith("INSERT INTO")

        if params is not None:
            self._cur.execute(translated_sql, params)
        else:
            self._cur.execute(translated_sql)

        if is_insert:
            try:
                self._cur.execute("SELECT lastval()")
                res = self._cur.fetchone()
                if res:
                    self.lastrowid = res[0]
            except Exception:
                self.lastrowid = None

        return self

    def executemany(self, sql, params_seq):
        sql_clean = sql.strip()
        if sql_clean.upper().startswith("PRAGMA") or sql_clean.upper().startswith("BEGIN"):
            return DummyCursor()
        translated_sql = translate_sql(sql)
        self._cur.executemany(translated_sql, params_seq)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def fetchmany(self, size=None):
        if size is None:
            return self._cur.fetchmany()
        return self._cur.fetchmany(size)

    def close(self):
        self._cur.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class PgConnectionWrapper:
    def __init__(self, raw_conn, pool):
        self._conn = raw_conn
        self._pool = pool
        self._closed = False
        self.row_factory = None  # Compatible attribute

    def cursor(self):
        import psycopg2.extras
        raw_cur = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        return PgCursorWrapper(raw_cur, self)

    def execute(self, sql, params=None):
        sql_clean = sql.strip()
        if sql_clean.upper().startswith("PRAGMA") or sql_clean.upper().startswith("BEGIN"):
            return DummyCursor()
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        if not self._closed and self._conn:
            self._conn.commit()

    def rollback(self):
        if not self._closed and self._conn:
            self._conn.rollback()

    def close(self):
        if not self._closed and self._conn and self._pool:
            try:
                self._pool.putconn(self._conn)
            except Exception:
                pass
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()


def get_db(read_only=False):
    """
    Returns an active database connection:
    - If DATABASE_BACKEND == 'postgres': checks out a thread-safe connection from PostgreSQL pool.
    - If DATABASE_BACKEND == 'sqlite': returns an optimized SQLite connection (existing behavior).
    """
    global DATABASE_BACKEND, pg_pool

    if DATABASE_BACKEND == "postgres" and pg_pool is not None:
        raw_conn = pg_pool.getconn()
        return PgConnectionWrapper(raw_conn, pg_pool)

    # Fallback to standard SQLite connection
    conn = sqlite3.connect(DB_PATH, timeout=45.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 45000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA mmap_size = 268435456")
    conn.execute("PRAGMA cache_size = -64000")
    conn.execute("PRAGMA temp_store = MEMORY")
    if read_only:
        conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def execute_db_write_with_retry(write_func, max_retries=6, base_delay=0.02):
    """
    Executes a write operation with automatic retry on concurrency conflicts:
    - In SQLite mode: retries on sqlite3.OperationalError (database locked / busy).
    - In PostgreSQL mode: retries on psycopg2 OperationalError / SerializationFailure.
    """
    import inspect
    last_err = None
    takes_conn = False
    try:
        sig = inspect.signature(write_func)
        takes_conn = len(sig.parameters) > 0
    except Exception:
        pass

    for attempt in range(max_retries):
        conn = None
        try:
            if takes_conn:
                conn = get_db()
                result = write_func(conn)
                conn.commit()
                conn.close()
                return result
            else:
                return write_func()
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                    conn.close()
                except Exception:
                    pass
            last_err = e
            err_msg = str(e).lower()
            if "locked" in err_msg or "busy" in err_msg or "deadlock" in err_msg or "serialization" in err_msg:
                sleep_time = (base_delay * (2 ** attempt)) + random.uniform(0.005, 0.025)
                time.sleep(sleep_time)
                continue
            raise
    raise last_err
