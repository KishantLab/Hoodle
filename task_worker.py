#!/usr/bin/env python3
"""
task_worker.py - Background Worker Daemon for Hoodle LMS
Polls the Redis task queue and executes asynchronous autograding,
exam evaluations, and plagiarism analysis.
"""

import os
import sys
import time
import json
import logging
import signal

# Ensure ACCLLMS root is in sys.path
sys.path.insert(0, "/data/admin/ACCLLMS")

from task_queue import (
    QUEUE_NAME, TASK_PREFIX, TASK_TTL,
    get_task_status, update_task_status, execute_job, _get_redis
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [TaskWorker] %(message)s"
)
logger = logging.getLogger("hoodle.task_worker")

_running = True


def _handle_exit(signum, frame):
    global _running
    logger.info("Worker received exit signal (%s). Shutting down cleanly...", signum)
    _running = False


signal.signal(signal.SIGINT, _handle_exit)
signal.signal(signal.SIGTERM, _handle_exit)


def run_worker():
    logger.info("Hoodle Task Worker started. Listening on Redis queue '%s'...", QUEUE_NAME)

    while _running:
        client = _get_redis()
        if not client:
            logger.warning("Redis client not available. Retrying in 5 seconds...")
            time.sleep(5)
            continue

        try:
            # Non-blocking pop from queue
            task_id_bytes = client.lpop(QUEUE_NAME)
            if not task_id_bytes:
                time.sleep(1)
                continue

            task_id = task_id_bytes.decode("utf-8") if isinstance(task_id_bytes, bytes) else str(task_id_bytes)

            task_info = get_task_status(task_id)
            if not task_info:
                logger.warning("Task %s not found in store. Skipping.", task_id)
                continue

            task_type = task_info.get("type")
            payload = task_info.get("payload", {})
            logger.info("Started processing task %s (type: %s)...", task_id, task_type)

            update_task_status(task_id, "PROCESSING")
            start_t = time.time()

            try:
                result = execute_job(task_type, payload)
                elapsed = time.time() - start_t
                update_task_status(task_id, "COMPLETED", result=result)
                logger.info("Finished task %s in %.2fs (status: COMPLETED)", task_id, elapsed)
            except Exception as ex:
                elapsed = time.time() - start_t
                logger.exception("Task %s failed after %.2fs: %s", task_id, elapsed, ex)
                update_task_status(task_id, "FAILED", error=str(ex))

        except Exception as e:
            if _running:
                logger.warning("Worker loop exception: %s. Pausing 2s...", e)
                time.sleep(2)

    logger.info("Hoodle Task Worker shut down successfully.")


if __name__ == "__main__":
    run_worker()
