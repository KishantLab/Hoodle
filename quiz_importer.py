"""
quiz_importer.py - Automated Quiz Import Engine for Hoodle LMS
Parses question sheets and assessments from:
- JSON (.json)
- PDF (.pdf) via Poppler pdftotext
- Aiken & Extended Plain Text (.txt)
- CSV (.csv)

Supports all 6 Hoodle Assessment Formats:
1. mcq: Single Choice
2. msq: Multiple Choice / Multiple Select (partial credit)
3. fitb: Fill in the Blanks with variations
4. mcq_justification: Choice + Student Reasoning for TA review
5. code_predict: Predict Exact Console Output
6. code_blank: Complete Code with ___BLANK___ token
"""

import os
import re
import csv
import json
import subprocess
import tempfile
from typing import Dict, List, Any, Tuple


# =====================================================================
# 1. SAMPLE TEMPLATE GENERATION
# =====================================================================

def get_sample_json_template() -> Dict[str, Any]:
    """Returns a comprehensive sample quiz structure covering all 6 question formats."""
    return {
        "title": "Systems & Accelerated Computing Assessment",
        "description": "Comprehensive quiz demonstrating all 6 Hoodle assessment question formats.",
        "duration_minutes": 45,
        "pass_marks": 10.0,
        "exam_lockdown": True,
        "shuffle_questions": True,
        "remote_approval_required": True,
        "questions": [
            {
                "type": "mcq",
                "prompt": "Which GPU memory space has the lowest access latency for threads within the same thread block?",
                "points": 2.0,
                "options": ["Global Memory", "Shared Memory", "Constant Memory", "Texture Memory"],
                "correct_answer": "B",
                "explanation": "Shared memory is on-chip and offers bandwidth comparable to L1 cache."
            },
            {
                "type": "msq",
                "prompt": "Which of the following statements about CUDA kernel execution are correct? (Select all that apply)",
                "points": 3.0,
                "options": [
                    "Kernel launches are asynchronous/non-blocking on the host by default",
                    "All threads in a thread block share private local registers",
                    "Warp divergence causes serialized execution of divergent execution paths",
                    "cudaDeviceSynchronize() blocks host execution until device finishes tasks"
                ],
                "correct_answer": "A,C,D",
                "explanation": "Registers are private to each thread; they are not shared."
            },
            {
                "type": "fitb",
                "prompt": "In POSIX operating systems, the system call used to duplicate an existing process is ___.",
                "points": 2.0,
                "correct_answer": "fork",
                "acceptable_variations": ["fork()", "fork();"],
                "explanation": "fork() creates an exact clone child process."
            },
            {
                "type": "mcq_justification",
                "prompt": "Is checking (ptr != NULL) sufficient to guarantee that dereferencing ptr will not cause a segmentation fault in C?",
                "points": 3.0,
                "options": ["Yes, it is completely sufficient", "No, it is not sufficient"],
                "correct_answer": "B",
                "explanation": "Dangling pointers and unmapped memory can still segfault even when non-null. Justification will be reviewed by TA."
            },
            {
                "type": "code_predict",
                "prompt": "What is the exact console output of this C program?",
                "language": "c",
                "code_snippet": "#include <stdio.h>\nint main() {\n    int a = 5, b = 2;\n    printf(\"%d\\n\", a * b + 3);\n    return 0;\n}",
                "points": 3.0,
                "correct_answer": "13",
                "acceptable_variations": ["13\n"],
                "explanation": "Multiplication takes precedence: 5 * 2 + 3 = 13."
            },
            {
                "type": "code_blank",
                "prompt": "Complete the CUDA unified memory allocation call below by providing the missing function token:",
                "language": "cuda",
                "code_snippet": "float *data;\n// Allocate 1024 floats in unified managed memory\n___BLANK___((void**)&data, 1024 * sizeof(float));",
                "points": 3.0,
                "correct_answer": "cudaMallocManaged",
                "acceptable_variations": ["cudaMallocManaged()"],
                "explanation": "cudaMallocManaged allocates unified memory accessible from CPU and GPU."
            }
        ]
    }


def get_sample_txt_template() -> str:
    """Returns sample plain text template covering all formats in easy readable form."""
    return """// Hoodle Assessment Template (Supports all 6 formats)
// Format 1: MCQ (Single Choice)
[TYPE: MCQ] [POINTS: 2.0]
Which GPU memory space has the lowest access latency for threads within a block?
A. Global Memory
B. Shared Memory
C. Constant Memory
D. Texture Memory
ANSWER: B
EXPLANATION: Shared memory is on-chip and offers latency comparable to L1 cache.

// Format 2: MSQ (Multiple Choice / Multiple Select)
[TYPE: MSQ] [POINTS: 3.0]
Which of the following statements about CUDA kernel execution are correct?
A. Kernel launches are non-blocking on host by default
B. All threads in a block share local registers
C. Warp divergence causes serialized execution
D. cudaDeviceSynchronize() blocks host until device finishes
ANSWER: A, C, D

// Format 3: FITB (Fill in the Blanks)
[TYPE: FITB] [POINTS: 2.0]
In POSIX operating systems, the system call used to duplicate an existing process is ___.
ANSWER: fork
VARIATIONS: fork(), fork();

// Format 4: MCQ with Justification
[TYPE: MCQ_JUSTIFICATION] [POINTS: 3.0]
Is checking (ptr != NULL) sufficient to guarantee dereferencing ptr will not segfault?
A. Yes, completely sufficient
B. No, it is not sufficient
ANSWER: B

// Format 5: Code Snippet - Predict Output
[TYPE: CODE_PREDICT] [LANG: c] [POINTS: 3.0]
What is the exact console output of this C program?
```c
#include <stdio.h>
int main() {
    int a = 5, b = 2;
    printf("%d\\n", a * b + 3);
    return 0;
}
```
ANSWER: 13

// Format 6: Code Snippet - Missing Blank
[TYPE: CODE_BLANK] [LANG: cuda] [POINTS: 3.0]
Complete the CUDA unified memory allocation call by providing the missing function token:
```cuda
float *data;
___BLANK___((void**)&data, 1024 * sizeof(float));
```
ANSWER: cudaMallocManaged
"""


# =====================================================================
# 2. JSON QUIZ PARSER
# =====================================================================

def parse_json_quiz(content_str: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Parses a JSON string. Supports both:
    1. Full quiz schema: { "title": "...", "duration_minutes": 30, "questions": [...] }
    2. Raw question array: [ { "prompt": "...", ... } ]
    """
    data = json.loads(content_str)
    meta = {}
    raw_questions = []

    if isinstance(data, dict):
        meta = {
            "title": data.get("title") or data.get("quiz_title") or "Imported Quiz",
            "description": data.get("description", ""),
            "duration_minutes": int(data.get("duration_minutes", 30)),
            "pass_marks": float(data.get("pass_marks", 40.0)),
            "exam_lockdown": 1 if data.get("exam_lockdown", True) else 0,
            "shuffle_questions": 1 if data.get("shuffle_questions", True) else 0,
            "remote_approval_required": 1 if data.get("remote_approval_required", True) else 0,
        }
        raw_questions = data.get("questions", [])
    elif isinstance(data, list):
        meta = {
            "title": "Imported Quiz",
            "description": "Imported from JSON",
            "duration_minutes": 30,
            "pass_marks": 40.0,
            "exam_lockdown": 1,
            "shuffle_questions": 1,
            "remote_approval_required": 1,
        }
        raw_questions = data

    normalized_questions = []
    for q in raw_questions:
        norm_q = normalize_question_dict(q)
        if norm_q:
            normalized_questions.append(norm_q)

    return meta, normalized_questions


def normalize_question_dict(q: Dict[str, Any]) -> Dict[str, Any]:
    """Normalizes any question dictionary into Hoodle's standard representation."""
    prompt = (q.get("prompt") or q.get("question") or q.get("text") or "").strip()
    if not prompt:
        return None

    raw_type = (q.get("type") or q.get("question_type") or "mcq").strip().lower()
    # Map common aliases
    type_map = {
        "mcq": "mcq",
        "single": "mcq",
        "single_choice": "mcq",
        "msq": "msq",
        "multiple": "msq",
        "multiple_select": "msq",
        "multi_choice": "msq",
        "fitb": "fitb",
        "fill_in_the_blank": "fitb",
        "blank": "fitb",
        "mcq_justification": "mcq_justification",
        "justification": "mcq_justification",
        "code_predict": "code_predict",
        "predict_output": "code_predict",
        "code_output": "code_predict",
        "code_blank": "code_blank",
        "code_fill": "code_blank",
    }
    q_type = type_map.get(raw_type, "mcq")

    points = float(q.get("points") or q.get("marks") or 1.0)
    options = q.get("options") or []
    if isinstance(options, str):
        options = [o.strip() for o in options.splitlines() if o.strip()]

    correct_answer = str(q.get("correct_answer") or q.get("answer") or "").strip()

    # If correct answer is given as full option text, convert to A, B, C, D
    if q_type in ("mcq", "mcq_justification") and correct_answer:
        for idx, opt in enumerate(options):
            if opt.strip().lower() == correct_answer.lower():
                correct_answer = chr(65 + idx)
                break

    acceptable_variations = q.get("acceptable_variations") or q.get("variations") or []
    if isinstance(acceptable_variations, str):
        acceptable_variations = [v.strip() for v in acceptable_variations.splitlines() if v.strip()]

    code_snippet = (q.get("code_snippet") or q.get("code") or "").strip()
    code_language = (q.get("code_language") or q.get("language") or "python").strip().lower()
    explanation = (q.get("explanation") or q.get("solution") or "").strip()

    return {
        "question_type": q_type,
        "prompt": prompt,
        "points": points,
        "options": options,
        "correct_answer": correct_answer,
        "acceptable_variations": acceptable_variations,
        "code_snippet": code_snippet,
        "code_language": code_language,
        "explanation": explanation
    }


# =====================================================================
# 3. PDF QUIZ PARSER (via Poppler pdftotext & Heuristics)
# =====================================================================

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


def parse_pdf_quiz(pdf_bytes: bytes) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Extracts text from PDF and identifies all question formats:
    - MCQs / MSQs with options (A)-(D)
    - Code Snippets (fenced or syntax-indicated)
    - Fill in Blanks (containing ___ or [blank])
    - Points indicated in brackets like [2 marks] or (3 pts)
    - Answer keys inline (Answer: B) or summary key at the end
    """
    raw_text = extract_text_from_pdf(pdf_bytes)
    return parse_text_quiz(raw_text)


# =====================================================================
# 4. PLAIN TEXT / AIKEN / EXTENDED FORMAT PARSER
# =====================================================================

def parse_text_quiz(raw_text: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Parses plain text, Aiken format, or custom formatted question sheets.
    """
    meta = {
        "title": "Imported Assessment",
        "description": "Auto-extracted from document",
        "duration_minutes": 30,
        "pass_marks": 40.0,
        "exam_lockdown": 1,
        "shuffle_questions": 1,
        "remote_approval_required": 1,
    }

    # Extract Title if present in first lines
    first_lines = [l.strip() for l in raw_text.splitlines()[:5] if l.strip() and not l.strip().startswith("//")]
    if first_lines and not re.match(r'^(Q\d|Question|\d+[\.\)])', first_lines[0], re.IGNORECASE):
        meta["title"] = first_lines[0][:80]

    # Normalize line breaks
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    # Split into question blocks
    # A new question begins with:
    # 1. [TYPE: ...] tag
    # 2. Q1., Q.1, Question 1:, 1., 1)
    q_split_pattern = re.compile(
        r'(?:\n|^)(?=(?:\[TYPE:[^\]]+\]|\b(?:Q|Question)\s*\d+[\.\:\)]|\b\d+[\.\)]\s+[A-Z]))',
        re.IGNORECASE
    )

    chunks = q_split_pattern.split(text)
    questions = []

    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk or len(chunk) < 5:
            continue
        q = parse_single_question_chunk(chunk)
        if q:
            questions.append(q)

    # If no questions found via strict split, try paragraph split
    if not questions:
        para_chunks = text.split("\n\n")
        for chunk in para_chunks:
            chunk = chunk.strip()
            if not chunk or len(chunk) < 8:
                continue
            q = parse_single_question_chunk(chunk)
            if q:
                questions.append(q)

    return meta, questions


def parse_single_question_chunk(chunk: str) -> Dict[str, Any]:
    """Parses one raw text question block into a normalized question dict."""
    lines = chunk.splitlines()
    first_line = lines[0].strip()

    # Detect Explicit [TYPE: ...] tag
    q_type = "mcq"
    points = 1.0
    code_lang = "python"

    type_match = re.search(r'\[TYPE:\s*([A-Z_]+)\]', chunk, re.IGNORECASE)
    if type_match:
        t_str = type_match.group(1).lower()
        if "msq" in t_str or "multi" in t_str:
            q_type = "msq"
        elif "fitb" in t_str or "blank" in t_str:
            q_type = "fitb"
        elif "just" in t_str:
            q_type = "mcq_justification"
        elif "predict" in t_str:
            q_type = "code_predict"
        elif "code_blank" in t_str:
            q_type = "code_blank"
        elif "mcq" in t_str:
            q_type = "mcq"

    pts_match = re.search(r'\[(?:POINTS|MARKS):\s*([\d\.]+)\]|\(([\d\.]+)\s*(?:pts?|marks?)\)|\[([\d\.]+)\s*(?:pts?|marks?)\]', chunk, re.IGNORECASE)
    if pts_match:
        val = pts_match.group(1) or pts_match.group(2) or pts_match.group(3)
        try:
            points = float(val)
        except Exception:
            pass

    lang_match = re.search(r'\[LANG:\s*([A-Za-z0-9\+#]+)\]|```([a-zA-Z0-9\+#]+)', chunk, re.IGNORECASE)
    if lang_match:
        code_lang = (lang_match.group(1) or lang_match.group(2) or "python").lower()

    # Extract Code Snippet if present (``` ... ```)
    code_snippet = ""
    code_block_match = re.search(r'```(?:[a-zA-Z0-9\+#]*)\n(.*?)```', chunk, re.DOTALL)
    if code_block_match:
        code_snippet = code_block_match.group(1).strip()
        # Clean the block out of prompt for cleaner text
        chunk = chunk[:code_block_match.start()] + "\n" + chunk[code_block_match.end():]
        if "___BLANK___" in code_snippet and q_type != "code_predict":
            q_type = "code_blank"
        elif q_type == "mcq" and not re.search(r'^[A-D][\.\)]', chunk, re.MULTILINE):
            q_type = "code_predict"

    # Extract Answer Key
    correct_ans = ""
    ans_match = re.search(r'(?:ANSWER|ANS|CORRECT|KEY):\s*([^\n]+)', chunk, re.IGNORECASE)
    if ans_match:
        correct_ans = ans_match.group(1).strip()

    # Extract Variations
    variations = []
    var_match = re.search(r'(?:VARIATIONS|ACCEPTABLE):\s*([^\n]+)', chunk, re.IGNORECASE)
    if var_match:
        raw_vars = var_match.group(1).strip()
        variations = [v.strip() for v in re.split(r'[,;]\s*', raw_vars) if v.strip()]

    # Extract Explanation
    explanation = ""
    exp_match = re.search(r'(?:EXPLANATION|SOLUTION|NOTE):\s*([^\n]+)', chunk, re.IGNORECASE)
    if exp_match:
        explanation = exp_match.group(1).strip()

    # Extract Options (A., B., C., D. or A), B), C), D) or (A), (B), (C), (D))
    options = []
    cleaned_prompt_lines = []
    lines = chunk.splitlines()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Skip header meta tags
        if stripped.startswith("[TYPE:") or stripped.startswith("//") or stripped.startswith("#"):
            continue
        if re.match(r'^(?:ANSWER|ANS|CORRECT|KEY|VARIATIONS|EXPLANATION|SOLUTION):', stripped, re.IGNORECASE):
            continue

        # Check for option line: e.g. "A. Paris" or "(A) Paris" or "A) Paris"
        opt_match = re.match(r'^(?:(?:\(([A-Fa-f])\))|(?:([A-Fa-f])[\.\)]))\s+(.*)$', stripped)
        if opt_match:
            opt_letter = (opt_match.group(1) or opt_match.group(2)).upper()
            opt_text = opt_match.group(3).strip()
            options.append(opt_text)
        else:
            cleaned_prompt_lines.append(line)

    prompt = "\n".join(cleaned_prompt_lines).strip()
    # Strip leading question markers from prompt: "Q1. ", "1. ", "Question 1: "
    prompt = re.sub(r'^(?:Q\d+[\.\:\)]|Question\s*\d+[\.\:\)]|\d+[\.\)]\s*)', '', prompt, flags=re.IGNORECASE).strip()

    if not prompt and not code_snippet:
        return None

    # Auto-infer question type if not explicitly set
    if q_type == "mcq":
        if "," in correct_ans:
            q_type = "msq"
        elif "___" in prompt or "___" in code_snippet:
            q_type = "code_blank" if code_snippet else "fitb"
        elif code_snippet and not options:
            q_type = "code_predict"

    # Default points and answers
    if not correct_ans:
        if q_type in ("mcq", "mcq_justification"):
            correct_ans = "A"
        elif q_type == "msq":
            correct_ans = "A"

    return {
        "question_type": q_type,
        "prompt": prompt,
        "points": points,
        "options": options,
        "correct_answer": correct_ans,
        "acceptable_variations": variations,
        "code_snippet": code_snippet,
        "code_language": code_lang,
        "explanation": explanation
    }


# =====================================================================
# 5. CSV FORMAT PARSER
# =====================================================================

def parse_csv_quiz(content_str: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Parses a CSV file with columns: Type, Prompt, Options, Answer, Points, Code, Lang, Explanation."""
    meta = {
        "title": "Imported CSV Assessment",
        "description": "Imported from CSV spreadsheet",
        "duration_minutes": 30,
        "pass_marks": 40.0,
        "exam_lockdown": 1,
        "shuffle_questions": 1,
        "remote_approval_required": 1,
    }

    reader = csv.reader(content_str.splitlines())
    rows = list(reader)
    if not rows:
        return meta, []

    header = [h.strip().lower() for h in rows[0]]
    questions = []

    # Map column positions
    col_map = {
        "prompt": -1, "type": -1, "answer": -1, "points": -1,
        "opt_a": -1, "opt_b": -1, "opt_c": -1, "opt_d": -1,
        "code": -1, "lang": -1, "explanation": -1, "variations": -1
    }

    for idx, col in enumerate(header):
        if "prompt" in col or "question" in col:
            col_map["prompt"] = idx
        elif "type" in col:
            col_map["type"] = idx
        elif "answer" in col or "correct" in col:
            col_map["answer"] = idx
        elif "point" in col or "mark" in col:
            col_map["points"] = idx
        elif "opt" in col and "a" in col or col == "a":
            col_map["opt_a"] = idx
        elif "opt" in col and "b" in col or col == "b":
            col_map["opt_b"] = idx
        elif "opt" in col and "c" in col or col == "c":
            col_map["opt_c"] = idx
        elif "opt" in col and "d" in col or col == "d":
            col_map["opt_d"] = idx
        elif "code" in col:
            col_map["code"] = idx
        elif "lang" in col:
            col_map["lang"] = idx
        elif "exp" in col:
            col_map["explanation"] = idx

    for r in rows[1:]:
        if not r or len(r) == 0:
            continue
        prompt = r[col_map["prompt"]].strip() if col_map["prompt"] != -1 and col_map["prompt"] < len(r) else ""
        if not prompt:
            continue

        q_type = r[col_map["type"]].strip().lower() if col_map["type"] != -1 and col_map["type"] < len(r) else "mcq"
        ans = r[col_map["answer"]].strip() if col_map["answer"] != -1 and col_map["answer"] < len(r) else "A"
        pts = float(r[col_map["points"]].strip()) if col_map["points"] != -1 and col_map["points"] < len(r) and r[col_map["points"]].strip() else 1.0

        options = []
        for opt_key in ("opt_a", "opt_b", "opt_c", "opt_d"):
            c_idx = col_map[opt_key]
            if c_idx != -1 and c_idx < len(r) and r[c_idx].strip():
                options.append(r[c_idx].strip())

        code = r[col_map["code"]].strip() if col_map["code"] != -1 and col_map["code"] < len(r) else ""
        lang = r[col_map["lang"]].strip() if col_map["lang"] != -1 and col_map["lang"] < len(r) else "python"
        exp = r[col_map["explanation"]].strip() if col_map["explanation"] != -1 and col_map["explanation"] < len(r) else ""

        questions.append({
            "question_type": q_type or "mcq",
            "prompt": prompt,
            "points": pts,
            "options": options,
            "correct_answer": ans,
            "acceptable_variations": [],
            "code_snippet": code,
            "code_language": lang,
            "explanation": exp
        })

    return meta, questions


# =====================================================================
# 6. UNIFIED FILE PARSER DISPATCHER
# =====================================================================

def parse_uploaded_quiz_file(file_storage) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Auto-detects file format from filename and contents, and returns (meta, questions).
    Supports: .json, .pdf, .txt, .csv
    """
    filename = (file_storage.filename or "").lower()
    file_bytes = file_storage.read()
    file_storage.seek(0)

    if filename.endswith(".json"):
        text = file_bytes.decode("utf-8", errors="ignore")
        return parse_json_quiz(text)
    elif filename.endswith(".pdf"):
        return parse_pdf_quiz(file_bytes)
    elif filename.endswith(".csv"):
        text = file_bytes.decode("utf-8", errors="ignore")
        return parse_csv_quiz(text)
    else:
        # Default to plain text / Aiken format
        text = file_bytes.decode("utf-8", errors="ignore")
        return parse_text_quiz(text)
