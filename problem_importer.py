"""
problem_importer.py - Automated Problem & Test Case Import Engine
Parses coding problem assignments, test cases, and code checks from:
- JSON (.json)
- PDF (.pdf) via Poppler pdftotext
- Plain Text / Markdown (.txt, .md)
"""

import os
import re
import json
import subprocess
import tempfile
from typing import Dict, List, Any, Tuple


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extracts layout-preserved text from PDF bytes using pdftotext."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        cmd = ["/usr/bin/pdftotext", "-layout", tmp_path, "-"]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return result.stdout.decode("utf-8", errors="ignore")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def get_sample_problem_txt_template() -> str:
    """Returns sample coding problem template in plain text / markdown format."""
    return """CSL605 Lab 2: Matrix Multiplication with CUDA Tiling
Description:
Implement 2D Matrix Multiplication C = A * B using CUDA Shared Memory Tiling.
Requirements: Must launch a __global__ CUDA kernel with <<<...>>> and call cudaFree() on all allocated buffers.

Starter Code:
```cuda
#include <stdio.h>
#include <cuda_runtime.h>

__global__ void matrixMul(float *A, float *B, float *C, int N) {
    // Implement tiled multiplication
}

int main() {
    return 0;
}
```

Test Case 1:
Input:
4 4 4
1 2 3 4
Output:
PASSED
Points: 25

Test Case 2:
Hidden: true
Input:
1024 1024 1024
Output:
PASSED
Points: 50
"""


def get_sample_problem_json_template() -> Dict[str, Any]:
    """Returns sample coding problem template in JSON."""
    return {
        "title": "CSL605 Lab 2: Matrix Multiplication with CUDA Tiling",
        "language": "cuda",
        "description": "### Problem Description\nImplement 2D Matrix Multiplication C = A * B using CUDA Shared Memory Tiling.",
        "starter_code": "#include <stdio.h>\n#include <cuda_runtime.h>\n\nint main() {\n    return 0;\n}\n",
        "code_checks": {
            "require_cuda_kernel": True,
            "cuda_kernel_points": 15,
            "check_memory_cleanup": True,
            "cleanup_points": 10,
            "float_tolerance": 0.001,
            "disallowed_keywords": ["cublas"]
        },
        "test_cases": [
            {
                "id": 1,
                "name": "Square Matrix 4x4",
                "stdin": "4 4 4\n1 2 3 4 ...\n",
                "expected_stdout": "PASSED",
                "points": 25,
                "timeout_sec": 5,
                "is_hidden": False
            },
            {
                "id": 2,
                "name": "Hidden Stress Test 1024x1024",
                "stdin": "1024 1024 1024\n...\n",
                "expected_stdout": "PASSED",
                "points": 50,
                "timeout_sec": 10,
                "is_hidden": True
            }
        ]
    }


def parse_json_problem(content_str: str) -> Dict[str, Any]:
    """Parses coding problem and test cases from JSON."""
    data = json.loads(content_str, strict=False)
    
    title = data.get("title") or data.get("name") or "Imported Coding Assignment"
    description = data.get("description") or data.get("problem_statement") or ""
    starter_code = data.get("starter_code") or data.get("template") or ""
    language = data.get("language") or "cuda"
    
    code_checks = data.get("code_checks") or {
        "require_cuda_kernel": True,
        "cuda_kernel_points": 15,
        "check_memory_cleanup": True,
        "cleanup_points": 10,
        "float_tolerance": 0.001,
        "disallowed_keywords": ["cublas"]
    }
    
    raw_cases = data.get("test_cases") or data.get("tests") or []
    test_cases = []
    for idx, tc in enumerate(raw_cases):
        test_cases.append({
            "id": idx + 1,
            "name": tc.get("name") or f"Test Case #{idx + 1}",
            "stdin": str(tc.get("stdin") or tc.get("input") or ""),
            "expected_stdout": str(tc.get("expected_stdout") or tc.get("output") or tc.get("expected") or "").rstrip(),
            "points": int(tc.get("points") or 25),
            "timeout_sec": int(tc.get("timeout_sec") or 5),
            "is_hidden": bool(tc.get("is_hidden") or tc.get("hidden", False))
        })
        
    return {
        "title": title,
        "description": description,
        "starter_code": starter_code,
        "language": language,
        "code_checks": code_checks,
        "test_cases": test_cases
    }


def parse_text_or_pdf_problem(raw_text: str) -> Dict[str, Any]:
    """
    Parses problem statement, starter code, and test cases from text/pdf.
    """
    lines = raw_text.splitlines()
    clean_lines = [l.strip() for l in lines if l.strip()]
    
    title = "Imported Coding Assignment"
    if clean_lines:
        title = re.sub(r'^(Lab\s*\d+|Assignment\s*\d+|Problem\s*\d+)[:\-\s]*', '', clean_lines[0], flags=re.IGNORECASE).strip() or clean_lines[0]
        if len(title) > 90:
            title = clean_lines[0][:90]

    starter_code = ""
    code_match = re.search(r'```(?:cuda|cpp|c|python)?\s*\n(.*?)\n```', raw_text, re.DOTALL)
    if code_match:
        starter_code = code_match.group(1).strip()
    
    test_cases = []
    tc_blocks = re.split(r'(?i)(?:Test\s*(?:Case)?\s*#?\d+|Sample\s*(?:Test\s*)?#?\d+):?', raw_text)
    if len(tc_blocks) > 1:
        for idx, block in enumerate(tc_blocks[1:], start=1):
            block_lines = block.strip()
            in_match = re.search(r'(?i)(?:Stdin|Input|Sample Input)[:\s]*\n?(.*?)(?=(?:Stdout|Output|Expected|Points|Hidden|$))', block_lines, re.DOTALL)
            out_match = re.search(r'(?i)(?:Stdout|Output|Expected|Expected Output)[:\s]*\n?(.*?)(?=(?:Points|Hidden|Test|$))', block_lines, re.DOTALL)
            
            stdin_val = in_match.group(1).strip() if in_match else ""
            stdout_val = out_match.group(1).strip() if out_match else ""
            
            is_hidden = bool(re.search(r'(?i)\bhidden\b', block_lines))
            pts_match = re.search(r'(?i)(\d+)\s*(?:pts|points|marks)', block_lines)
            points = int(pts_match.group(1)) if pts_match else 25
            
            if stdin_val or stdout_val:
                test_cases.append({
                    "id": len(test_cases) + 1,
                    "name": f"Test Case #{len(test_cases) + 1}" + (" (Hidden)" if is_hidden else ""),
                    "stdin": stdin_val,
                    "expected_stdout": stdout_val,
                    "points": points,
                    "timeout_sec": 5,
                    "is_hidden": is_hidden
                })
                
    if not test_cases:
        io_matches = list(re.finditer(r'(?i)(?:Input|Sample Input)[:\s]*\n?(.*?)\n(?=(?:Output|Sample Output)[:\s]*\n?(.*?)(?:\n\n|\Z))', raw_text, re.DOTALL))
        for idx, m in enumerate(io_matches, start=1):
            stdin_val = m.group(1).strip()
            stdout_val = m.group(2).strip()
            if stdin_val or stdout_val:
                test_cases.append({
                    "id": idx,
                    "name": f"Test Case #{idx}",
                    "stdin": stdin_val,
                    "expected_stdout": stdout_val,
                    "points": 25,
                    "timeout_sec": 5,
                    "is_hidden": False
                })

    description = raw_text.strip()
    if starter_code and "```" in description:
        description = description.split("```")[0].strip()

    code_checks = {
        "require_cuda_kernel": bool(re.search(r'(?i)(?:cuda|gpu|kernel|<<<)', raw_text)),
        "cuda_kernel_points": 15,
        "check_memory_cleanup": bool(re.search(r'(?i)(?:cleanup|cudaFree|memory)', raw_text)),
        "cleanup_points": 10,
        "float_tolerance": 0.001,
        "disallowed_keywords": ["cublas"] if "cublas" in raw_text.lower() else []
    }

    return {
        "title": title,
        "description": description,
        "starter_code": starter_code,
        "language": "cuda" if code_checks["require_cuda_kernel"] else "c",
        "code_checks": code_checks,
        "test_cases": test_cases
    }


def parse_uploaded_problem_file(file_storage) -> Dict[str, Any]:
    """Auto-detects file format (json, pdf, txt, md) and returns extracted problem data."""
    filename = (file_storage.filename or "").lower()
    file_bytes = file_storage.read()
    file_storage.seek(0)

    if filename.endswith(".json"):
        text = file_bytes.decode("utf-8", errors="ignore")
        return parse_json_problem(text)
    elif filename.endswith(".pdf"):
        raw_text = extract_text_from_pdf(file_bytes)
        return parse_text_or_pdf_problem(raw_text)
    else:
        text = file_bytes.decode("utf-8", errors="ignore")
        return parse_text_or_pdf_problem(text)
