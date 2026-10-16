"""
terminal_server.py - Interactive Sandboxed PTY Session Manager for Hoodle Cloud IDE
Spawns bash sessions strictly inside Bubblewrap (bwrap) as the unprivileged NIS user hoodle_ide (UID 2500).
Restricted exclusively to /workspace (the student's isolated locker directory).
Provides bidirectional stream over HTTP chunked streaming & SSE / polling without requiring root.
"""

import os
import pty
import select
import termios
import struct
import fcntl
import signal
import time
import threading
from pathlib import Path
from typing import Dict, Any

ACTIVE_TERMINALS: Dict[str, Dict[str, Any]] = {}
TERMINAL_LOCK = threading.Lock()


def get_terminal_session(session_id: str):
    with TERMINAL_LOCK:
        return ACTIVE_TERMINALS.get(session_id)


def create_terminal_session(session_id: str, locker_path: str, course_id: int = None) -> bool:
    """Spawns an interactive bash process inside bwrap for session_id."""
    with TERMINAL_LOCK:
        if session_id in ACTIVE_TERMINALS:
            term = ACTIVE_TERMINALS[session_id]
            if term.get("alive"):
                return True

    master_fd, slave_fd = pty.openpty()

    # Build the bwrap jail command running as unprivileged hoodle_ide (UID 2500)
    cmd = [
        "bwrap",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/etc", "/etc",
        "--ro-bind", "/apps", "/apps",
        "--dev-bind", "/dev", "/dev",
        "--uid", "2500",
        "--gid", "65534",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--bind", locker_path, "/workspace",
        "--chdir", "/workspace",
        "--setenv", "HOME", "/tmp",
        "--setenv", "USER", "hoodle_ide",
        "--setenv", "PATH", "/apps/cuda/cuda-12.1/bin:/usr/local/bin:/usr/bin:/bin",
        "--setenv", "PS1", "\\[\\033[01;32m\\]hoodle_ide@cluster\\[\\033[00m\\]:\\[\\033[01;34m\\]/workspace\\[\\033[00m\\]$ ",
        "--unshare-all",
        "--unshare-net",
        "--die-with-parent"
    ]

    if course_id:
        pkg_dir = f"/data/admin/ACCLLMS/storage/course_packages/course_{course_id}"
        if os.path.exists(pkg_dir):
            cmd.extend([
                "--ro-bind", pkg_dir, "/course_packages",
                "--setenv", "PYTHONPATH", "/course_packages"
            ])

    cmd.extend(["/bin/bash", "--norc", "--noprofile"])

    import subprocess
    pid = subprocess.Popen(
        cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        preexec_fn=os.setsid,
        close_fds=True
    ).pid

    os.close(slave_fd)

    # Set non-blocking I/O on master_fd
    fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    with TERMINAL_LOCK:
        ACTIVE_TERMINALS[session_id] = {
            "master_fd": master_fd,
            "pid": pid,
            "created_at": time.time(),
            "last_active": time.time(),
            "alive": True,
            "buffer": bytearray()
        }

    return True


def write_to_terminal(session_id: str, data: str) -> bool:
    term = get_terminal_session(session_id)
    if not term or not term.get("alive"):
        return False
    try:
        os.write(term["master_fd"], data.encode("utf-8"))
        term["last_active"] = time.time()
        return True
    except OSError:
        return False


def read_from_terminal(session_id: str, max_bytes: int = 4096) -> str:
    term = get_terminal_session(session_id)
    if not term or not term.get("alive"):
        return ""
    try:
        # Check if master_fd has data ready
        r, _, _ = select.select([term["master_fd"]], [], [], 0.05)
        if r:
            chunk = os.read(term["master_fd"], max_bytes)
            term["last_active"] = time.time()
            return chunk.decode("utf-8", errors="replace")
    except OSError:
        pass
    return ""


def resize_terminal(session_id: str, rows: int, cols: int):
    term = get_terminal_session(session_id)
    if not term or not term.get("alive"):
        return
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(term["master_fd"], termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


def close_terminal_session(session_id: str):
    with TERMINAL_LOCK:
        term = ACTIVE_TERMINALS.pop(session_id, None)
    if term:
        try:
            os.close(term["master_fd"])
        except OSError:
            pass
        try:
            os.killpg(os.getpgid(term["pid"]), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
