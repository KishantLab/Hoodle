"""
pip_manager.py - Course-Level Python Pip Package Manager for Hoodle LMS
Allows teachers/TAs to install custom python libraries into course-isolated directories
without requiring system root/sudo privileges.
Supports asynchronous background installation with live polling, system library discovery,
popular packages catalog, and package uninstallation.
"""

import os
import sys
import time
import uuid
import shutil
import threading
import subprocess
import importlib.metadata
from pathlib import Path
from typing import Dict, List, Any, Optional

BASE_COURSE_PACKAGES = Path("/data/admin/ACCLLMS/storage/course_packages")
BASE_COURSE_PACKAGES.mkdir(parents=True, exist_ok=True)

# In-memory store for background installation jobs
# job_id -> {course_id, package, status: 'running'|'completed'|'failed', start_time, end_time, log: [], error: str}
INSTALL_JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()

# Curated list of popular academic & computational libraries
POPULAR_PACKAGES = [
    {
        "name": "numpy",
        "category": "Scientific Computing",
        "description": "Powerful N-dimensional array manipulation, linear algebra, and mathematical operations.",
        "preinstalled": True,
        "default_ver": "1.21.5 (system)"
    },
    {
        "name": "scipy",
        "category": "Scientific Computing",
        "description": "Algorithms for optimization, integration, interpolation, eigenvalue problems, and statistics.",
        "preinstalled": False,
        "default_ver": ""
    },
    {
        "name": "pandas",
        "category": "Data Analysis",
        "description": "High-performance data manipulation and analysis with DataFrame structures.",
        "preinstalled": False,
        "default_ver": ""
    },
    {
        "name": "matplotlib",
        "category": "Visualization",
        "description": "Comprehensive library for creating static, animated, and interactive visualizations.",
        "preinstalled": False,
        "default_ver": ""
    },
    {
        "name": "seaborn",
        "category": "Visualization",
        "description": "Statistical data visualization built on top of Matplotlib with attractive default themes.",
        "preinstalled": False,
        "default_ver": ""
    },
    {
        "name": "sympy",
        "category": "Mathematics",
        "description": "Full-featured computer algebra system (CAS) for symbolic mathematics and calculus.",
        "preinstalled": False,
        "default_ver": ""
    },
    {
        "name": "scikit-learn",
        "category": "Machine Learning",
        "description": "Simple and efficient tools for predictive data analysis and classic machine learning.",
        "preinstalled": False,
        "default_ver": ""
    },
    {
        "name": "Pillow",
        "category": "Computer Vision",
        "description": "Python Imaging Library (PIL Fork) for opening, manipulating, and saving many image formats.",
        "preinstalled": True,
        "default_ver": "9.0.1 (system)"
    },
    {
        "name": "requests",
        "category": "Networking",
        "description": "Elegant and simple HTTP library for human beings to make REST API requests.",
        "preinstalled": True,
        "default_ver": "2.25.1 (system)"
    },
    {
        "name": "pytest",
        "category": "Testing",
        "description": "Framework that makes it easy to write small, readable tests, and scales to complex functional testing.",
        "preinstalled": False,
        "default_ver": ""
    },
    {
        "name": "networkx",
        "category": "Graph Theory",
        "description": "Study of the structure, dynamics, and functions of complex networks and graph data.",
        "preinstalled": False,
        "default_ver": ""
    },
    {
        "name": "nltk",
        "category": "NLP",
        "description": "Natural Language Toolkit for text processing, tokenization, stemming, and linguistic analysis.",
        "preinstalled": False,
        "default_ver": ""
    }
]

STANDARD_MODULES = [
    {"name": "math", "description": "Mathematical functions (sin, cos, sqrt, log, ceil, floor, pi, e)"},
    {"name": "random", "description": "Pseudo-random number generators, shuffling, and sampling"},
    {"name": "json", "description": "JSON encoder and decoder for structured data"},
    {"name": "csv", "description": "CSV file reading and writing utilities"},
    {"name": "re", "description": "Regular expression matching operations"},
    {"name": "datetime", "description": "Basic date and time types, formatting, and arithmetic"},
    {"name": "collections", "description": "Specialized container datatypes (Counter, defaultdict, deque, namedtuple)"},
    {"name": "itertools", "description": "Functions creating iterators for efficient looping (combinations, permutations)"},
    {"name": "functools", "description": "Higher-order functions and operations on callable objects (reduce, lru_cache)"},
    {"name": "statistics", "description": "Mathematical statistics functions (mean, median, variance, stdev)"},
    {"name": "sqlite3", "description": "DB-API 2.0 interface for SQLite databases"},
    {"name": "os", "description": "Miscellaneous operating system interfaces and path operations"},
    {"name": "sys", "description": "System-specific parameters, exit handling, and command-line arguments"},
    {"name": "hashlib", "description": "Secure hash and message digest algorithms (sha256, md5)"},
    {"name": "time", "description": "Time access, measurement, and sleep conversions"},
    {"name": "copy", "description": "Shallow and deep copy operations for objects"},
    {"name": "heapq", "description": "Heap queue algorithm (priority queues)"},
    {"name": "bisect", "description": "Array bisection algorithm for sorted lists"},
    {"name": "typing", "description": "Support for type hints (List, Dict, Tuple, Optional, Any)"}
]


def get_course_package_dir(course_id: int) -> Path:
    p = BASE_COURSE_PACKAGES / f"course_{course_id}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def list_course_packages(course_id: int) -> List[Dict[str, str]]:
    """Lists all python packages installed specifically in this course directory."""
    p_dir = get_course_package_dir(course_id)
    packages = []
    
    # Check .dist-info directories
    for item in sorted(p_dir.iterdir()):
        if item.name.endswith(".dist-info") and item.is_dir():
            name_ver = item.name[:-len(".dist-info")]
            parts = name_ver.split("-")
            name = parts[0]
            ver = parts[1] if len(parts) > 1 else "installed"
            packages.append({"name": name, "version": ver})
    return packages


# Cache system packages in memory (cached for life of process)
_SYSTEM_PACKAGES_CACHE: Optional[List[Dict[str, str]]] = None

def list_system_packages() -> List[Dict[str, str]]:
    """Lists all system-wide python packages pre-installed on the host."""
    global _SYSTEM_PACKAGES_CACHE
    if _SYSTEM_PACKAGES_CACHE is not None:
        return _SYSTEM_PACKAGES_CACHE
    
    try:
        dists = []
        for d in importlib.metadata.distributions():
            try:
                name = d.metadata["Name"]
                ver = d.version
                dists.append({"name": name, "version": ver})
            except Exception:
                continue
        dists = sorted(dists, key=lambda x: x["name"].lower())
        _SYSTEM_PACKAGES_CACHE = dists
        return dists
    except Exception:
        return []


def get_popular_packages_catalog(course_id: int) -> List[Dict[str, Any]]:
    """Returns curated popular packages annotated with installation status for this course."""
    course_pkgs = {p["name"].lower(): p["version"] for p in list_course_packages(course_id)}
    system_pkgs = {p["name"].lower(): p["version"] for p in list_system_packages()}
    
    result = []
    for p in POPULAR_PACKAGES:
        pkg_lower = p["name"].lower()
        item = dict(p)
        if pkg_lower in course_pkgs:
            item["status"] = "Course Installed"
            item["active_version"] = course_pkgs[pkg_lower]
            item["is_installed"] = True
        elif pkg_lower in system_pkgs:
            item["status"] = "Pre-installed (System)"
            item["active_version"] = system_pkgs[pkg_lower]
            item["is_installed"] = True
        else:
            item["status"] = "Available to Install"
            item["active_version"] = ""
            item["is_installed"] = False
        result.append(item)
    return result


def _run_pip_install_thread(job_id: str, course_id: int, clean_pkg: str):
    """Background worker executing pip install with live rolling log buffer."""
    target_dir = str(get_course_package_dir(course_id))
    
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--target", target_dir,
        "--no-warn-script-location",
        "--prefer-binary",
        clean_pkg
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        with JOBS_LOCK:
            if job_id in INSTALL_JOBS:
                INSTALL_JOBS[job_id]["pid"] = proc.pid

        # Read output line by line
        for line in iter(proc.stdout.readline, ""):
            with JOBS_LOCK:
                if job_id in INSTALL_JOBS:
                    INSTALL_JOBS[job_id]["log"].append(line)
                    # Keep last 100 lines to avoid memory bloat
                    if len(INSTALL_JOBS[job_id]["log"]) > 100:
                        INSTALL_JOBS[job_id]["log"].pop(0)

        proc.stdout.close()
        return_code = proc.wait(timeout=600)

        with JOBS_LOCK:
            if job_id in INSTALL_JOBS:
                INSTALL_JOBS[job_id]["end_time"] = time.time()
                if return_code == 0:
                    INSTALL_JOBS[job_id]["status"] = "completed"
                    INSTALL_JOBS[job_id]["message"] = f"Successfully installed '{clean_pkg}' for this course."
                else:
                    INSTALL_JOBS[job_id]["status"] = "failed"
                    INSTALL_JOBS[job_id]["error"] = f"Installation exited with code {return_code}."
    except Exception as e:
        with JOBS_LOCK:
            if job_id in INSTALL_JOBS:
                INSTALL_JOBS[job_id]["end_time"] = time.time()
                INSTALL_JOBS[job_id]["status"] = "failed"
                INSTALL_JOBS[job_id]["error"] = str(e)


def start_install_job(course_id: int, package_name: str) -> Dict[str, Any]:
    """Starts an asynchronous pip installation job in the background."""
    clean_pkg = package_name.strip()
    bad_chars = {";", "&", "|", "`", "$", "(", ")", chr(10), chr(13), " "}
    if not clean_pkg or any(c in bad_chars for c in clean_pkg):
        return {"success": False, "error": "Invalid package name format. Only standard package names (e.g. 'pandas' or 'scipy==1.10.0') are accepted."}

    # Check if there is already a running job for this course and package
    with JOBS_LOCK:
        for j_id, j in INSTALL_JOBS.items():
            if j.get("course_id") == course_id and j.get("package") == clean_pkg and j.get("status") == "running":
                return {
                    "success": True,
                    "status": "running",
                    "job_id": j_id,
                    "package": clean_pkg,
                    "message": f"Installation of '{clean_pkg}' is already running."
                }

        job_id = uuid.uuid4().hex[:12]
        INSTALL_JOBS[job_id] = {
            "job_id": job_id,
            "course_id": course_id,
            "package": clean_pkg,
            "status": "running",
            "start_time": time.time(),
            "end_time": None,
            "log": [f"Starting pip install for '{clean_pkg}' into course_{course_id}...\n"],
            "error": None,
            "message": None
        }

    t = threading.Thread(
        target=_run_pip_install_thread,
        args=(job_id, course_id, clean_pkg),
        daemon=True
    )
    t.start()

    return {
        "success": True,
        "status": "running",
        "job_id": job_id,
        "package": clean_pkg,
        "message": f"Started installation of '{clean_pkg}'."
    }


def get_job_status(job_id: str) -> Dict[str, Any]:
    """Retrieves current progress and status of a pip install job."""
    with JOBS_LOCK:
        job = INSTALL_JOBS.get(job_id)
        if not job:
            return {"success": False, "error": "Job not found."}
        
        now = time.time()
        start = job.get("start_time", now)
        end = job.get("end_time") or now
        elapsed = int(end - start)

        return {
            "success": True,
            "job_id": job_id,
            "course_id": job.get("course_id"),
            "package": job.get("package"),
            "status": job.get("status"),
            "elapsed_seconds": elapsed,
            "log": "".join(job.get("log", [])),
            "message": job.get("message"),
            "error": job.get("error")
        }


def uninstall_course_package(course_id: int, package_name: str) -> Dict[str, Any]:
    """Removes an installed custom course package directory."""
    clean_pkg = package_name.strip().lower()
    if not clean_pkg or any(c in clean_pkg for c in ["/", "\\", "..", ";", " "]):
        return {"success": False, "error": "Invalid package name."}

    p_dir = get_course_package_dir(course_id)
    removed = []

    for item in p_dir.iterdir():
        # Match dist-info
        if item.name.lower().startswith(f"{clean_pkg}-") and item.name.endswith(".dist-info"):
            try:
                shutil.rmtree(item)
                removed.append(item.name)
            except Exception as e:
                return {"success": False, "error": f"Failed to delete {item.name}: {str(e)}"}
        # Match module folder
        elif item.name.lower() == clean_pkg and item.is_dir():
            try:
                shutil.rmtree(item)
                removed.append(item.name)
            except Exception as e:
                return {"success": False, "error": f"Failed to delete {item.name}: {str(e)}"}
        # Match single-file module
        elif item.name.lower() == f"{clean_pkg}.py" and item.is_file():
            try:
                item.unlink()
                removed.append(item.name)
            except Exception as e:
                return {"success": False, "error": f"Failed to delete {item.name}: {str(e)}"}

    if removed:
        return {"success": True, "message": f"Successfully uninstalled '{clean_pkg}' ({', '.join(removed)})."}
    else:
        return {"success": False, "error": f"Package '{clean_pkg}' was not found in course packages."}


def install_course_package(course_id: int, package_name: str) -> Dict[str, Any]:
    """Synchronous fallback wrapper with generous timeout."""
    job = start_install_job(course_id, package_name)
    if not job.get("success"):
        return job

    job_id = job["job_id"]
    for _ in range(300):  # Wait up to 300s
        time.sleep(1)
        stat = get_job_status(job_id)
        if stat["status"] == "completed":
            return {"success": True, "message": stat.get("message"), "output": stat.get("log")}
        elif stat["status"] == "failed":
            return {"success": False, "error": stat.get("error") or "Installation failed.", "output": stat.get("log")}
    return {"success": False, "error": "Installation timed out (exceeded limit)."}
