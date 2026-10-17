"""
task_queue.py - Asynchronous Evaluation Task Queue for Hoodle LMS
Enables non-blocking background code evaluation, autograding, and plagiarism scans.
Powered by Redis with 100% resilient ThreadPoolExecutor fallback if Redis is down.
"""

import os
import json
import time
import uuid
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, Optional

logger = logging.getLogger("hoodle.task_queue")

QUEUE_NAME = "hoodle:tasks:queue"
TASK_PREFIX = "hoodle:task:"
TASK_TTL = 3600  # 1 hour

_local_tasks: Dict[str, Dict[str, Any]] = {}
_fallback_executor = ThreadPoolExecutor(max_workers=4)


def _get_redis():
    try:
        import redis_cache
        return redis_cache.get_redis_client()
    except Exception:
        return None


def enqueue_task(task_type: str, payload: dict) -> str:
    """
    Enqueues an asynchronous task.
    Returns:
        task_id: UUID string identifying the queued task.
    """
    task_id = str(uuid.uuid4())
    task_data = {
        "task_id": task_id,
        "type": task_type,
        "status": "QUEUED",
        "created_at": time.time(),
        "payload": payload,
        "result": None,
        "error": None
    }

    client = _get_redis()
    if client:
        try:
            client.setex(f"{TASK_PREFIX}{task_id}", TASK_TTL, json.dumps(task_data))
            client.rpush(QUEUE_NAME, task_id)
            logger.info("Enqueued task %s (%s) to Redis queue.", task_id, task_type)
            return task_id
        except Exception as e:
            logger.warning("Redis enqueue error: %s. Falling back to local thread pool.", e)

    # Local fallback
    _local_tasks[task_id] = task_data
    _fallback_executor.submit(_process_task_locally, task_id, task_type, payload)
    return task_id


def get_task_status(task_id: str) -> Optional[Dict[str, Any]]:
    """Returns task status and result dict or None if not found."""
    client = _get_redis()
    if client:
        try:
            raw = client.get(f"{TASK_PREFIX}{task_id}")
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning("Redis get_task_status error: %s", e)

    return _local_tasks.get(task_id)


def update_task_status(task_id: str, status: str, result: Any = None, error: str = None):
    """Updates task execution status and result."""
    client = _get_redis()
    if client:
        try:
            raw = client.get(f"{TASK_PREFIX}{task_id}")
            if raw:
                task_data = json.loads(raw)
                task_data["status"] = status
                if result is not None:
                    task_data["result"] = result
                if error is not None:
                    task_data["error"] = error
                if status in ("COMPLETED", "FAILED"):
                    task_data["completed_at"] = time.time()
                client.setex(f"{TASK_PREFIX}{task_id}", TASK_TTL, json.dumps(task_data))
                return
        except Exception as e:
            logger.warning("Redis update_task_status error: %s", e)

    if task_id in _local_tasks:
        _local_tasks[task_id]["status"] = status
        if result is not None:
            _local_tasks[task_id]["result"] = result
        if error is not None:
            _local_tasks[task_id]["error"] = error
        if status in ("COMPLETED", "FAILED"):
            _local_tasks[task_id]["completed_at"] = time.time()


def execute_job(task_type: str, payload: dict) -> Any:
    """Executes the specific job logic."""
    if task_type == "autograder":
        from runner import evaluate_autograder
        locker_path = payload.get("locker_path")
        filename = payload.get("filename", "main.cu")
        test_cases = payload.get("test_cases", [])
        code_checks = payload.get("code_checks", {})
        return evaluate_autograder(locker_path, filename, test_cases, code_checks)

    elif task_type == "exam_submission":
        from runner import evaluate_autograder
        from db_adapter import get_db, execute_db_write_with_retry
        submission_id = payload.get("submission_id")
        assignment_id = payload.get("assignment_id")
        locker_path = payload.get("locker_path")
        filename = payload.get("filename", "main.cu")
        test_cases = payload.get("test_cases", [])
        code_checks = payload.get("code_checks", {})

        report = evaluate_autograder(locker_path, filename, test_cases, code_checks)
        score = report.get("score", 0.0)

        def _save_score(conn):
            conn.execute("""
                UPDATE coding_submissions
                SET autograde_score = ?
                WHERE id = ?
            """, (score, submission_id))
            conn.commit()

        execute_db_write_with_retry(_save_score)
        return {"report": report, "score": score}

    elif task_type == "plagiarism_scan":
        from plagiarism_detector import calculate_similarity_matrix
        submissions = payload.get("submissions", [])
        return calculate_similarity_matrix(submissions)

    else:
        raise ValueError(f"Unknown task type: {task_type}")


def _process_task_locally(task_id: str, task_type: str, payload: dict):
    update_task_status(task_id, "PROCESSING")
    try:
        result = execute_job(task_type, payload)
        update_task_status(task_id, "COMPLETED", result=result)
    except Exception as e:
        logger.exception("Error executing local task %s: %s", task_id, e)
        update_task_status(task_id, "FAILED", error=str(e))
