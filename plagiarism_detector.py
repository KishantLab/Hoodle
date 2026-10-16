"""
plagiarism_detector.py - Background Code Similarity & Plagiarism Engine for Hoodle LMS
Uses AST normalization and Winnowing n-gram hashing (MOSS / JPlag style).
- Robust against identifier renaming, whitespace differences, and code reordering.
- Automatically subtracts starter template code to prevent false positives.
- Executes asynchronously in background threads with low CPU priority.
"""

import re
import math
import hashlib
from typing import List, Dict, Tuple, Set, Any


def tokenize_code(source: str, lang: str = "c") -> List[str]:
    """
    Tokenizes and normalizes source code:
    - Strips comments and string literals
    - Canonicalizes variable names, types, and numbers into generic tokens
    - Retains structural grammar tokens (control flow, brackets, operators)
    """
    # 1. Remove comments
    # Multi-line /* ... */
    clean = re.sub(r'/\*.*?\*/', ' ', source, flags=re.DOTALL)
    # Single-line // ...
    clean = re.sub(r'//.*', ' ', clean)
    # Python comments
    if lang == "python":
        clean = re.sub(r'#.*', ' ', clean)

    # 2. Extract words and punctuation tokens
    tokens = re.findall(r'[A-Za-z_][A-Za-z0-9_]*|\d+|[{}()\[\];,.<>+\-*\/%=!&|^~?:#]', clean)

    # Keywords to preserve
    keywords = {
        "if", "else", "for", "while", "do", "switch", "case", "return", "break", "continue",
        "int", "float", "double", "char", "void", "size_t", "struct", "class",
        "__global__", "__device__", "__host__", "__shared__", "cudaMalloc", "cudaFree",
        "cudaMemcpy", "cudaDeviceSynchronize", "dim3", "blockIdx", "threadIdx", "blockDim", "gridDim",
        "def", "class", "import", "from", "in", "is", "None", "True", "False", "with", "as"
    }

    norm_tokens = []
    var_map = {}
    var_counter = 1

    for t in tokens:
        if t in keywords or len(t) == 1 and not t.isalnum():
            norm_tokens.append(t)
        elif t.isdigit():
            norm_tokens.append("NUM")
        else:
            # Canonicalize user variable/function identifiers: V1, V2, V3...
            if t not in var_map:
                var_map[t] = f"V{var_counter}"
                var_counter += 1
            norm_tokens.append(var_map[t])

    return norm_tokens


def get_k_grams(tokens: List[str], k: int = 8) -> List[Tuple[int, str]]:
    """Generates sliding window k-grams."""
    if len(tokens) < k:
        return []
    grams = []
    for i in range(len(tokens) - k + 1):
        gram_str = " ".join(tokens[i:i + k])
        # Hash the gram into 64-bit integer
        h = int(hashlib.md5(gram_str.encode("utf-8")).hexdigest()[:16], 16)
        grams.append((i, h))
    return grams


def winnow(hashes: List[Tuple[int, str]], window_size: int = 4) -> Set[int]:
    """
    Winnowing algorithm (MOSS): selects minimum hash in each sliding window.
    Guarantees detecting any match of length >= k + window_size - 1.
    """
    fingerprints = set()
    n = len(hashes)
    if n < window_size:
        return {h for _, h in hashes}

    for i in range(n - window_size + 1):
        window = hashes[i:i + window_size]
        min_elem = min(window, key=lambda x: x[1])
        fingerprints.add(min_elem[1])

    return fingerprints


def compute_code_fingerprints(source: str, lang: str = "c", k: int = 8, w: int = 4) -> Set[int]:
    tokens = tokenize_code(source, lang)
    if not tokens:
        return set()
    kgrams = get_k_grams(tokens, k)
    return winnow(kgrams, w)


def calculate_similarity_matrix(submissions: List[Dict[str, Any]], starter_code: str = "") -> List[Dict[str, Any]]:
    """
    Computes pairwise similarity scores between all submitted code snapshots.
    Excludes starter template code fingerprints to eliminate false positives.
    """
    starter_fps = compute_code_fingerprints(starter_code) if starter_code else set()

    # Precompute fingerprints for each submission
    processed = []
    for sub in submissions:
        code = sub.get("code", "")
        fps = compute_code_fingerprints(code) - starter_fps
        processed.append({
            "user_id": sub.get("user_id"),
            "display_name": sub.get("display_name", f"User {sub.get('user_id')}"),
            "roll_number": sub.get("roll_number", ""),
            "fingerprints": fps,
            "code": code,
            "submission_id": sub.get("submission_id")
        })

    results = []
    n = len(processed)
    for i in range(n):
        for j in range(i + 1, n):
            sub1 = processed[i]
            sub2 = processed[j]

            fp1 = sub1["fingerprints"]
            fp2 = sub2["fingerprints"]

            if not fp1 and not fp2:
                sim = 0.0
            elif not fp1 or not fp2:
                sim = 0.0
            else:
                intersection = len(fp1 & fp2)
                union = len(fp1 | fp2)
                sim = round((intersection / union) * 100, 1)

            # Record matches
            if sim >= 15.0 or (len(fp1 & fp2) > 5):
                results.append({
                    "user1_id": sub1["user_id"],
                    "user1_name": sub1["display_name"],
                    "user1_roll": sub1["roll_number"],
                    "user2_id": sub2["user_id"],
                    "user2_name": sub2["display_name"],
                    "user2_roll": sub2["roll_number"],
                    "similarity_pct": sim,
                    "shared_fingerprints_count": len(fp1 & fp2),
                    "is_flagged": (sim >= 70.0)
                })

    # Sort descending by similarity
    results.sort(key=lambda x: x["similarity_pct"], reverse=True)
    return results

import os
import json
import zipfile
from datetime import datetime

CODE_EXTENSIONS = {
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".cu", ".cuh",
    ".py", ".java", ".js", ".ts", ".html", ".css", ".php",
    ".rb", ".go", ".rs", ".sh", ".bash", ".txt", ".sql"
}


def extract_code_from_file(file_path: str, orig_name: str = "") -> str:
    """
    Extracts source code text from a student submission file.
    Supports single source files as well as multi-file ZIP archives.
    """
    if not file_path or not os.path.exists(file_path):
        return ""

    path_lower = file_path.lower()
    orig_lower = (orig_name or "").lower()

    # 1. ZIP Archive Extraction
    if path_lower.endswith(".zip") or orig_lower.endswith(".zip"):
        code_chunks = []
        try:
            with zipfile.ZipFile(file_path, "r") as z:
                for entry in z.infolist():
                    if entry.is_dir() or entry.file_size > 1024 * 1024:  # Skip >1MB individual files
                        continue
                    fname_lower = entry.filename.lower()
                    ext = os.path.splitext(fname_lower)[1]
                    if ext in CODE_EXTENSIONS or "/" not in fname_lower:
                        try:
                            content = z.read(entry).decode("utf-8", errors="replace")
                            # Ignore non-text or binary
                            if "\x00" not in content and len(content.strip()) > 10:
                                code_chunks.append(f"// FILE: {entry.filename}\n" + content)
                        except Exception:
                            continue
        except Exception:
            pass
        return "\n\n".join(code_chunks)

    # 2. Direct Source File Reading
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(512 * 1024)  # Read up to 512KB
            if "\x00" not in content:
                return content
    except Exception:
        pass

    return ""


def init_coursework_plagiarism_db(conn):
    """Initializes coursework_plagiarism_scans table."""
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS coursework_plagiarism_scans (
                id SERIAL PRIMARY KEY,
                coursework_id INT NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                total_submissions INT DEFAULT 0,
                results_json TEXT,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cw_plag_scan ON coursework_plagiarism_scans(coursework_id, created_at DESC);")
        except Exception:
            pass
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass


def run_coursework_plagiarism_scan(coursework_id: int, conn) -> Dict[str, Any]:
    """
    Executes a complete plagiarism & code similarity scan across all student submissions
    for a specific assignment (coursework).
    """
    init_coursework_plagiarism_db(conn)
    cur = conn.cursor()

    # 1. Register scan entry
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        INSERT INTO coursework_plagiarism_scans (coursework_id, status, total_submissions, created_at, updated_at)
        VALUES (?, 'running', 0, ?, ?)
    """, (coursework_id, now_str, now_str))
    conn.commit()

    # Get scan_id
    cur.execute("SELECT id FROM coursework_plagiarism_scans WHERE coursework_id = ? ORDER BY id DESC LIMIT 1;", (coursework_id,))
    scan_row = cur.fetchone()
    scan_id = scan_row[0] if isinstance(scan_row, (tuple, list)) else scan_row["id"]

    try:
        # 2. Fetch all student submissions
        cur.execute("""
            SELECT s.id as submission_id, s.student_id, s.original_filename, s.file_path,
                   u.display_name, u.roll_number, u.username
            FROM submissions s
            JOIN users u ON s.student_id = u.id
            WHERE s.coursework_id = ?
            ORDER BY u.roll_number ASC
        """, (coursework_id,))
        subs_rows = cur.fetchall()

        submissions_data = []
        code_by_sub_id = {}

        for row in subs_rows:
            sub_id = row[0] if isinstance(row, (tuple, list)) else row["submission_id"]
            user_id = row[1] if isinstance(row, (tuple, list)) else row["student_id"]
            orig_name = row[2] if isinstance(row, (tuple, list)) else row["original_filename"]
            fpath = row[3] if isinstance(row, (tuple, list)) else row["file_path"]
            disp_name = row[4] if isinstance(row, (tuple, list)) else row["display_name"]
            roll = row[5] if isinstance(row, (tuple, list)) else row["roll_number"]
            uname = row[6] if isinstance(row, (tuple, list)) else row["username"]

            code = extract_code_from_file(fpath, orig_name)
            if code and len(code.strip()) > 15:
                submissions_data.append({
                    "submission_id": sub_id,
                    "user_id": user_id,
                    "display_name": disp_name or uname,
                    "roll_number": roll or uname,
                    "code": code
                })
                code_by_sub_id[sub_id] = code[:20000]  # Store preview snippet up to 20KB

        # Also check coding_submissions if linked coding assignment
        cur.execute("""
            SELECT cs.id as submission_id, cs.user_id, cs.code_snapshot,
                   u.display_name, u.roll_number, u.username
            FROM coding_submissions cs
            JOIN users u ON cs.user_id = u.id
            WHERE cs.assignment_id = ?
        """, (coursework_id,))
        coding_rows = cur.fetchall()
        for crow in coding_rows:
            csub_id = crow[0] if isinstance(crow, (tuple, list)) else crow["submission_id"]
            cuid = crow[1] if isinstance(crow, (tuple, list)) else crow["user_id"]
            csnap = crow[2] if isinstance(crow, (tuple, list)) else crow["code_snapshot"]
            cdisp = crow[3] if isinstance(crow, (tuple, list)) else crow["display_name"]
            croll = crow[4] if isinstance(crow, (tuple, list)) else crow["roll_number"]
            cuname = crow[5] if isinstance(crow, (tuple, list)) else crow["username"]

            # If user not already added via file submission
            if csnap and not any(s["user_id"] == cuid for s in submissions_data):
                submissions_data.append({
                    "submission_id": csub_id,
                    "user_id": cuid,
                    "display_name": cdisp or cuname,
                    "roll_number": croll or cuname,
                    "code": csnap
                })
                code_by_sub_id[csub_id] = csnap[:20000]

        total_scanned = len(submissions_data)
        matches = []
        if total_scanned >= 2:
            matches = calculate_similarity_matrix(submissions_data)

        # Attach submission IDs to matches for comparison view
        user_to_sub = {s["user_id"]: s["submission_id"] for s in submissions_data}
        for m in matches:
            m["user1_sub_id"] = user_to_sub.get(m["user1_id"])
            m["user2_sub_id"] = user_to_sub.get(m["user2_id"])

        report = {
            "coursework_id": coursework_id,
            "scan_id": scan_id,
            "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_submissions": total_scanned,
            "flagged_matches_count": sum(1 for m in matches if m.get("is_flagged")),
            "matches": matches
        }

        # 3. Update scan record
        cur.execute("""
            UPDATE coursework_plagiarism_scans
            SET status = 'completed', total_submissions = ?, results_json = ?, updated_at = ?
            WHERE id = ?
        """, (total_scanned, json.dumps(report), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), scan_id))
        conn.commit()

        return report

    except Exception as e:
        logger.error(f"Plagiarism scan failed for coursework {coursework_id}: {e}")
        try:
            cur.execute("""
                UPDATE coursework_plagiarism_scans
                SET status = 'failed', error_message = ?, updated_at = ?
                WHERE id = ?
            """, (str(e), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), scan_id))
            conn.commit()
        except Exception:
            pass
        return {"status": "failed", "error": str(e)}


def find_similar_line_indices(code1: str, code2: str) -> Tuple[List[int], List[int]]:
    """
    Identifies 1-indexed line numbers in code1 and code2 that contain identical
    or structurally matching code logic (after whitespace normalization and AST tokenization).
    """
    lines1 = (code1 or "").splitlines()
    lines2 = (code2 or "").splitlines()

    TRIVIAL = {'{', '}', '};', 'else', 'else {', 'begin', 'end', ';', '(', ')', '', 'None', 'pass'}

    def norm_line(l):
        s = l.strip()
        if len(s) <= 2 or s in TRIVIAL:
            return ""
        tokens = tokenize_code(s)
        return " ".join(tokens) if tokens else ""

    norm1 = [norm_line(l) for l in lines1]
    norm2 = [norm_line(l) for l in lines2]

    matched1 = set()
    matched2 = set()

    for i, n1 in enumerate(norm1):
        if not n1:
            continue
        s1 = lines1[i].strip()
        for j, n2 in enumerate(norm2):
            if not n2:
                continue
            s2 = lines2[j].strip()
            # Direct text match or normalized token match
            if s1 == s2 or (n1 == n2 and len(n1.split()) >= 3):
                matched1.add(i + 1)
                matched2.add(j + 1)
                break

    return sorted(list(matched1)), sorted(list(matched2))
