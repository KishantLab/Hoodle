"""
redis_cache.py - High-Throughput In-Memory Caching for Hoodle LMS
Used for sub-millisecond dynamic rotating QR attendance verification,
rate limiting, session verification, and background task queues.

Features 100% silent fallback: if Redis is unavailable, all operations
seamlessly degrade without raising errors or interrupting user workflows.
"""

import os
import time
import logging
from typing import Optional, Any

logger = logging.getLogger("hoodle.redis")

def _get_redis_host():
    env_h = os.environ.get("REDIS_HOST")
    if env_h:
        return env_h
    try:
        with open("/etc/hoodle/hoodle.env", "r") as f:
            for line in f:
                if line.strip().startswith("REDIS_HOST="):
                    return line.strip().split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.05)
        if s.connect_ex(("127.0.0.1", REDIS_PORT)) == 0:
            s.close()
            return "127.0.0.1"
        s.close()
    except Exception:
        pass
    return "192.168.99.2"

REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_DB = int(os.environ.get("REDIS_DB", "0"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", None)

_redis_client = None
_redis_available = None
_last_check_ts = 0


def get_redis_client():
    """Returns a connected Redis client or None if Redis is unreachable."""
    global _redis_client, _redis_available, _last_check_ts
    now = time.time()

    # Retry reconnection every 10 seconds if previously unavailable
    if _redis_available is False and (now - _last_check_ts < 10):
        return None

    if _redis_client is not None:
        return _redis_client

    try:
        import redis
        target_host = _get_redis_host()
        pool = redis.ConnectionPool(
            host=target_host,
            port=REDIS_PORT,
            db=REDIS_DB,
            password=REDIS_PASSWORD,
            socket_timeout=0.5,
            socket_connect_timeout=0.5,
            max_connections=100
        )
        client = redis.Redis(connection_pool=pool)
        if client.ping():
            _redis_client = client
            _redis_available = True
            logger.info("Connected to Redis in-memory cache successfully.")
            return _redis_client
    except Exception as e:
        _last_check_ts = now
        _redis_available = False
        logger.warning("Redis not available: %s. Continuing with database fallback.", e)
        return None


def cache_qr_token(session_key: Any, token: str, ttl_seconds: int = 90) -> bool:
    """
    Caches the dynamic QR token for an attendance session in a Redis set.
    Supports storing both current and previous rotation tokens to allow grace windows.
    """
    client = get_redis_client()
    if not client or not token:
        return False
    try:
        key = f"hoodle:att:qr:{session_key}"
        token_str = str(token).strip().upper()
        pipeline = client.pipeline()
        pipeline.sadd(key, token_str)
        pipeline.expire(key, max(ttl_seconds, 60))
        pipeline.execute()
        return True
    except Exception as e:
        logger.warning("Redis cache_qr_token error: %s", e)
        return False


def verify_qr_token(session_key: Any, token: str) -> Optional[bool]:
    """
    Verifies dynamic QR token against in-memory cache (<0.2ms).
    Returns:
        True: Token matches active cached token in set.
        False: Key exists but token does NOT match.
        None: Cache miss or Redis unavailable (caller should check fallback).
    """
    client = get_redis_client()
    if not client or not token:
        return None
    try:
        key = f"hoodle:att:qr:{session_key}"
        token_str = str(token).strip().upper()
        if not client.exists(key):
            return None  # Cache miss or expired
        return bool(client.sismember(key, token_str))
    except Exception as e:
        logger.warning("Redis verify_qr_token error: %s", e)
        return None


def invalidate_qr_tokens(session_key: Any) -> bool:
    """Invalidates cached QR tokens when an attendance session ends."""
    client = get_redis_client()
    if not client:
        return False
    try:
        key = f"hoodle:att:qr:{session_key}"
        client.delete(key)
        return True
    except Exception:
        return False


def cache_set(key: str, val: Any, ttl_seconds: int = 60) -> bool:
    """Generic in-memory cache set."""
    client = get_redis_client()
    if not client:
        return False
    try:
        import json
        client.setex(f"hoodle:{key}", ttl_seconds, json.dumps(val))
        return True
    except Exception:
        return False


def cache_get(key: str) -> Optional[Any]:
    """Generic in-memory cache get."""
    client = get_redis_client()
    if not client:
        return None
    try:
        import json
        raw = client.get(f"hoodle:{key}")
        if raw is not None:
            return json.loads(raw)
        return None
    except Exception:
        return None
