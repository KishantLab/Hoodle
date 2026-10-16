import os
import subprocess
import time
import math
from pathlib import Path

DEFAULT_LOCKER_QUOTA = 200 * 1024 * 1024  # 200 MB default
MAX_TEACHER_QUOTA = 500 * 1024 * 1024     # 500 MB max for teachers
GPU_MEMORY_LIMIT_MB = 1024                # 1 GB VRAM strict limit

def get_locker_usage(locker_path: str, quota_bytes: int = DEFAULT_LOCKER_QUOTA):
    p = Path(locker_path)
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    for root, dirs, files in os.walk(p):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total_bytes += os.path.getsize(fp)
            except OSError:
                pass
    return total_bytes, quota_bytes, total_bytes > quota_bytes

def select_best_gpu_node():
    """
    Intelligent backend load scheduler (completely blackbox to students):
    1. Checks if Slurm batch jobs are running on gpu1 or gpu2.
    2. Routes to the node with lowest load without impacting Slurm.
    """
    try:
        r1 = subprocess.run(["ssh", "-o", "ConnectTimeout=2", "gpu1", "squeue -h -w gpu1 | wc -l"], capture_output=True, text=True, timeout=3)
        slurm_gpu1 = int(r1.stdout.strip()) if r1.returncode == 0 and r1.stdout.strip().isdigit() else 0
    except Exception:
        slurm_gpu1 = 0

    try:
        r2 = subprocess.run(["ssh", "-o", "ConnectTimeout=2", "gpu2", "squeue -h -w gpu2 | wc -l"], capture_output=True, text=True, timeout=3)
        slurm_gpu2 = int(r2.stdout.strip()) if r2.returncode == 0 and r2.stdout.strip().isdigit() else 0
    except Exception:
        slurm_gpu2 = 0

    if slurm_gpu1 > 0 and slurm_gpu2 == 0:
        return "gpu2"
    elif slurm_gpu2 > 0 and slurm_gpu1 == 0:
        return "gpu1"

    return "gpu1"

def build_bwrap_cmd(locker_path: str, inner_cmd: str, is_cuda: bool = False, course_id: int = None):
    prefix = ""
    if is_cuda:
        prefix = f"export CUDA_MPS_PINNED_DEVICE_MEM_LIMIT=0={GPU_MEMORY_LIMIT_MB}M && "

    # Mount course python packages if present
    extra_env = ""
    if course_id:
        pkg_dir = f"/data/admin/ACCLLMS/storage/course_packages/course_{course_id}"
        if os.path.exists(pkg_dir):
            extra_env = f"export PYTHONPATH=/course_packages:$PYTHONPATH && "

    prefix = extra_env + prefix

    cmd = [
        "bwrap",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/etc", "/etc",
        "--uid", "2500",
        "--gid", "65534",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--bind", locker_path, "/workspace",
        "--chdir", "/workspace",
        "--unshare-all",
        "--unshare-net",
        "--die-with-parent"
    ]

    if course_id:
        pkg_dir = f"/data/admin/ACCLLMS/storage/course_packages/course_{course_id}"
        if os.path.exists(pkg_dir):
            cmd.extend(["--ro-bind", pkg_dir, "/course_packages"])
    if is_cuda:
        cmd.extend([
            "--ro-bind", "/apps/cuda", "/apps/cuda",
            "--dev-bind", "/dev", "/dev"
        ])
    else:
        cmd.extend([
            "--dev", "/dev"
        ])
    cmd.extend(["bash", "-c", prefix + inner_cmd])
    return cmd

def compare_outputs(actual: str, expected: str, tolerance: float = 0.001) -> bool:
    """
    Compares program stdout with expected stdout:
    1. Exact or normalized string match.
    2. Numerical / float tolerance matching.
    """
    act = actual.strip()
    exp = expected.strip()
    if act == exp or exp in act:
        return True

    # Token-by-token numeric comparison
    act_tokens = act.split()
    exp_tokens = exp.split()
    if len(act_tokens) == len(exp_tokens) and len(act_tokens) > 0:
        all_numeric = True
        for a, e in zip(act_tokens, exp_tokens):
            try:
                fa = float(a)
                fe = float(e)
                if abs(fa - fe) > tolerance:
                    return False
            except ValueError:
                if a != e:
                    return False
        return True

    return False

def check_code_quality_and_structure(source_code: str, code_checks: dict) -> list:
    """
    Performs static and structural code analysis (CUDA verification, memory leaks, banned libraries).
    """
    results = []
    if not code_checks:
        return results

    # 1. CUDA GPU Kernel Verification
    if code_checks.get("require_cuda_kernel"):
        pts = code_checks.get("cuda_kernel_points", 15)
        has_global = "__global__" in source_code
        has_launch = "<<<" in source_code and ">>>" in source_code

        if has_global and has_launch:
            results.append({
                "type": "code_check",
                "name": "⚡ CUDA GPU Kernel Launch Verification",
                "status": "PASSED",
                "points_earned": pts,
                "points_possible": pts,
                "details": "Verified: Student declared a `__global__` kernel and executed on GPU with `<<<blocks, threads>>>` syntax."
            })
        else:
            results.append({
                "type": "code_check",
                "name": "⚡ CUDA GPU Kernel Launch Verification",
                "status": "FAILED",
                "points_earned": 0,
                "points_possible": pts,
                "details": "Architecture Violation: No CUDA GPU kernel launch (`__global__` or `<<<...>>>`) detected in code. CPU-only computations are disallowed."
            })

    # 2. GPU Memory Cleanup (cudaFree)
    if code_checks.get("check_memory_cleanup"):
        pts = code_checks.get("cleanup_points", 10)
        has_alloc = "cudaMalloc" in source_code
        has_free = "cudaFree" in source_code

        if has_alloc and not has_free:
            results.append({
                "type": "code_check",
                "name": "🛡️ GPU Device Memory Cleanup",
                "status": "FAILED",
                "points_earned": 0,
                "points_possible": pts,
                "details": "Resource Leak: `cudaMalloc` allocation detected without matching `cudaFree()` deallocation."
            })
        elif has_alloc and has_free:
            results.append({
                "type": "code_check",
                "name": "🛡️ GPU Device Memory Cleanup",
                "status": "PASSED",
                "points_earned": pts,
                "points_possible": pts,
                "details": "Verified: Proper GPU device memory deallocation (`cudaFree`) implemented."
            })

    # 3. Disallowed Libraries / Functions
    banned = code_checks.get("disallowed_keywords", [])
    if banned:
        violation = [b for b in banned if b.strip() and b.strip() in source_code]
        if violation:
            results.append({
                "type": "code_check",
                "name": "🚫 Forbidden Library / API Check",
                "status": "FAILED",
                "points_earned": 0,
                "points_possible": 0,
                "details": f"Policy Violation: Disallowed library/function call detected: {', '.join(violation)}"
            })

    return results

def execute_in_sandbox(locker_path: str, filename: str, stdin_input: str = "", timeout_sec: int = 15, quota_bytes: int = DEFAULT_LOCKER_QUOTA):
    used, quota, exceeded = get_locker_usage(locker_path, quota_bytes)
    if exceeded:
        return {
            "success": False,
            "stage": "quota",
            "error": f"Storage Quota Exceeded: Using {used / (1024*1024):.2f} MB of your {quota / (1024*1024):.0f} MB limit. Please remove unused files.",
            "stdout": "",
            "stderr": "",
            "exit_code": -1,
            "duration_ms": 0
        }

    ext = Path(filename).suffix.lower()
    binary_name = "program.out"
    is_cuda = (ext in (".cu", ".cuh"))

    compile_cmd = None
    run_cmd = f"./{binary_name}"

    if is_cuda:
        compile_cmd = f"/apps/cuda/cuda-12.1/bin/nvcc -O3 -arch=sm_86 '{filename}' -o '{binary_name}'"
    elif ext == ".c":
        compile_cmd = f"gcc -O2 -Wall '{filename}' -o '{binary_name}'"
    elif ext in (".cpp", ".cc", ".cxx"):
        compile_cmd = f"g++ -O2 -std=c++17 -Wall '{filename}' -o '{binary_name}'"
    elif ext == ".py":
        compile_cmd = None
        run_cmd = f"python3 -u '{filename}'"
    elif ext == ".java":
        classname = Path(filename).stem
        compile_cmd = f"javac '{filename}'"
        run_cmd = f"java '{classname}'"
    else:
        return {
            "success": False,
            "stage": "unsupported",
            "error": f"Unsupported file extension '{ext}'. Supported languages: .cu, .cpp, .c, .py, .java",
            "stdout": "",
            "stderr": "",
            "exit_code": -1,
            "duration_ms": 0
        }

    node = select_best_gpu_node()

    compile_log = ""
    compile_time_ms = 0
    if compile_cmd:
        t0 = time.time()
        bwrap_comp = build_bwrap_cmd(locker_path, compile_cmd, is_cuda=is_cuda)
        bwrap_str = " ".join([f"'{arg}'" if " " in arg or ";" in arg else arg for arg in bwrap_comp])
        full_cmd = ["ssh", node, bwrap_str]

        try:
            res = subprocess.run(full_cmd, capture_output=True, text=True, timeout=40)
            compile_time_ms = int((time.time() - t0) * 1000)
            compile_log = (res.stdout + "\n" + res.stderr).strip()
            if res.returncode != 0:
                return {
                    "success": False,
                    "stage": "compilation",
                    "error": "Compilation Failed",
                    "compile_log": compile_log,
                    "stdout": "",
                    "stderr": compile_log,
                    "exit_code": res.returncode,
                    "compile_time_ms": compile_time_ms,
                    "duration_ms": 0
                }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "stage": "compilation_timeout",
                "error": "Compilation Timed Out (exceeded 40s)",
                "compile_log": "Compiler process terminated due to timeout.",
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
                "duration_ms": 0
            }

    bwrap_run = build_bwrap_cmd(locker_path, run_cmd, is_cuda=is_cuda)
    bwrap_str = " ".join([f"'{arg}'" if " " in arg or ";" in arg else arg for arg in bwrap_run])
    full_cmd = ["ssh", node, bwrap_str]

    t0 = time.time()
    try:
        res = subprocess.run(
            full_cmd,
            input=stdin_input,
            capture_output=True,
            text=True,
            timeout=timeout_sec
        )
        run_duration_ms = int((time.time() - t0) * 1000)
        return {
            "success": (res.returncode == 0),
            "stage": "execution",
            "compile_log": compile_log,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "exit_code": res.returncode,
            "compile_time_ms": compile_time_ms,
            "duration_ms": run_duration_ms
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "stage": "execution_timeout",
            "error": f"Execution Timed Out (exceeded {timeout_sec}s limit)",
            "compile_log": compile_log,
            "stdout": "",
            "stderr": f"Process terminated: Wall-clock execution exceeded {timeout_sec}s limit.",
            "exit_code": 124,
            "duration_ms": timeout_sec * 1000
        }

def evaluate_autograder(locker_path: str, filename: str, test_cases: list, code_checks: dict = None, quota_bytes: int = DEFAULT_LOCKER_QUOTA):
    src_file = Path(locker_path) / filename
    source_code = ""
    if src_file.exists():
        try:
            source_code = src_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # 1. Run Static & Structural Code Analysis Checks
    code_results = check_code_quality_and_structure(source_code, code_checks)
    code_pts_earned = sum(r["points_earned"] for r in code_results)
    code_pts_possible = sum(r["points_possible"] for r in code_results)

    ext = Path(filename).suffix.lower()
    is_cuda = (ext in (".cu", ".cuh"))
    binary_name = "test_autograded.out"
    node = select_best_gpu_node()

    compile_cmd = None
    if is_cuda:
        compile_cmd = f"/apps/cuda/cuda-12.1/bin/nvcc -O3 -arch=sm_86 '{filename}' -o '{binary_name}'"
    elif ext == ".c":
        compile_cmd = f"gcc -O2 -Wall '{filename}' -o '{binary_name}'"
    elif ext in (".cpp", ".cc"):
        compile_cmd = f"g++ -O2 -std=c++17 -Wall '{filename}' -o '{binary_name}'"
    elif ext == ".py":
        compile_cmd = None
        binary_name = filename
    elif ext == ".java":
        classname = Path(filename).stem
        compile_cmd = f"javac '{filename}'"
        binary_name = classname

    if compile_cmd:
        bwrap_comp = build_bwrap_cmd(locker_path, compile_cmd, is_cuda=is_cuda)
        bwrap_str = " ".join([f"'{arg}'" if " " in arg or ";" in arg else arg for arg in bwrap_comp])
        full_cmd = ["ssh", node, bwrap_str]

        res = subprocess.run(full_cmd, capture_output=True, text=True, timeout=40)
        if res.returncode != 0:
            return {
                "overall_status": "Compilation Error",
                "total_points_earned": 0,
                "total_points_possible": sum(t.get("points", 10) for t in test_cases) + code_pts_possible,
                "compile_error": (res.stdout + "\n" + res.stderr).strip(),
                "code_checks": code_results,
                "test_results": []
            }

    test_results = []
    earned_pts = code_pts_earned
    total_pts = sum(t.get("points", 10) for t in test_cases) + code_pts_possible
    tol = code_checks.get("float_tolerance", 0.001) if code_checks else 0.001

    for tc in test_cases:
        timeout = tc.get("timeout_sec", 5)
        run_cmd = f"./{binary_name}" if ext != ".py" and ext != ".java" else (f"python3 -u '{filename}'" if ext == ".py" else f"java '{binary_name}'")
        bwrap_run = build_bwrap_cmd(locker_path, run_cmd, is_cuda=is_cuda)
        bwrap_str = " ".join([f"'{arg}'" if " " in arg or ";" in arg else arg for arg in bwrap_run])
        full_cmd = ["ssh", node, bwrap_str]

        t0 = time.time()
        try:
            r = subprocess.run(
                full_cmd,
                input=tc.get("stdin", ""),
                capture_output=True,
                text=True,
                timeout=timeout
            )
            elapsed_ms = int((time.time() - t0) * 1000)
            actual_out = r.stdout.strip()
            expected_out = tc.get("expected_stdout", "").strip()

            is_correct = (r.returncode == 0) and compare_outputs(actual_out, expected_out, tolerance=tol)
            pts = tc.get("points", 10) if is_correct else 0
            earned_pts += pts

            test_results.append({
                "id": tc.get("id"),
                "name": tc.get("name", f"Test Case {tc.get('id')}"),
                "is_hidden": tc.get("is_hidden", False),
                "status": "PASSED" if is_correct else ("Wrong Answer" if r.returncode == 0 else "Runtime Error"),
                "points_earned": pts,
                "points_possible": tc.get("points", 10),
                "time_ms": elapsed_ms,
                "actual_stdout": actual_out if not tc.get("is_hidden") else "[Hidden Test Output]",
                "expected_stdout": expected_out if not tc.get("is_hidden") else "[Hidden Output]",
                "stderr": r.stderr.strip()
            })
        except subprocess.TimeoutExpired:
            test_results.append({
                "id": tc.get("id"),
                "name": tc.get("name", f"Test Case {tc.get('id')}"),
                "is_hidden": tc.get("is_hidden", False),
                "status": "Time Limit Exceeded",
                "points_earned": 0,
                "points_possible": tc.get("points", 10),
                "time_ms": timeout * 1000,
                "actual_stdout": "",
                "expected_stdout": "[Hidden Output]" if tc.get("is_hidden") else tc.get("expected_stdout", ""),
                "stderr": f"Execution exceeded {timeout}s limit"
            })

    return {
        "overall_status": "Passed (Full Marks)" if earned_pts == total_pts else f"Score: {earned_pts}/{total_pts} pts",
        "total_points_earned": earned_pts,
        "total_points_possible": total_pts,
        "compile_error": None,
        "code_checks": code_results,
        "test_results": test_results
    }
