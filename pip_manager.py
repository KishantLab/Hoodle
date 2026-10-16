"""
pip_manager.py - Course-Level Python Pip Package Manager for Hoodle LMS
Allows teachers/TAs to install custom python libraries into course-isolated directories
without requiring system root/sudo privileges.
"""

import os
import sys
import subprocess
from pathlib import Path
from typing import Dict, List, Any

BASE_COURSE_PACKAGES = Path("/data/admin/ACCLLMS/storage/course_packages")
BASE_COURSE_PACKAGES.mkdir(parents=True, exist_ok=True)


def get_course_package_dir(course_id: int) -> Path:
    p = BASE_COURSE_PACKAGES / f"course_{course_id}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def list_course_packages(course_id: int) -> List[Dict[str, str]]:
    """Lists all python packages installed in the course."""
    p_dir = get_course_package_dir(course_id)
    packages = []
    
    # Check .dist-info or standard directories
    for item in sorted(p_dir.iterdir()):
        if item.name.endswith(".dist-info") and item.is_dir():
            name_ver = item.name[:-len(".dist-info")]
            parts = name_ver.split("-")
            name = parts[0]
            ver = parts[1] if len(parts) > 1 else "installed"
            packages.append({"name": name, "version": ver})
    return packages


def install_course_package(course_id: int, package_name: str) -> Dict[str, Any]:
    """
    Installs a pip package into the isolated course directory using --target.
    Blocks dangerous / malicious packages or flag injection.
    """
    clean_pkg = package_name.strip()
    if not clean_pkg or any(c in clean_pkg for c in [";", "&", "|", "`", "$", "(", ")", "\n", "\r"]):
        return {"success": False, "error": "Invalid package name format."}

    target_dir = str(get_course_package_dir(course_id))
    
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--target", target_dir,
        "--no-warn-script-location",
        "--upgrade",
        clean_pkg
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if res.returncode == 0:
            return {
                "success": True,
                "message": f"Successfully installed '{clean_pkg}' for this course.",
                "output": res.stdout[-400:] if len(res.stdout) > 400 else res.stdout
            }
        else:
            return {
                "success": False,
                "error": "Installation failed.",
                "output": res.stderr[-500:] if len(res.stderr) > 500 else res.stderr
            }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Installation timed out (exceeded 120s limit)."}
