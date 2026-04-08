#!/usr/bin/env python3
"""
analyze.py — Find coverage gaps in the Rust compiler.

Usage:
    python3 coverage/analyze.py [coverage.json] [--top N] [--min-lines N] [--format text|md]

Output:
    - Per-crate summary (sorted by uncovered lines)
    - Zero-coverage files
    - Low-coverage files (< threshold)
    - Top uncovered functions per crate
    - Prioritized action targets (highest-value gaps to fill)
"""

import json
import subprocess
import sys
import re
from collections import defaultdict
from pathlib import Path

# --- Config ---
JSON_PATH = "coverage/out-compiler/coverage.json"
TOP_N = 30
MIN_LINES = 5
LOW_COV_THRESH = 50
FORMAT = "text"

args = sys.argv[1:]
while args:
    a = args.pop(0)
    if a == "--top" and args:
        TOP_N = int(args.pop(0))
    elif a == "--min-lines" and args:
        MIN_LINES = int(args.pop(0))
    elif a == "--format" and args:
        FORMAT = args.pop(0)
    elif not a.startswith("--"):
        JSON_PATH = a


def extract_crate(filename: str) -> str:
    m = re.search(r"/compiler/([^/]+)/", filename)
    if m:
        return m.group(1)
    m = re.search(r"/library/([^/]+)/", filename)
    if m:
        return "std::" + m.group(1)
    parts = Path(filename).parts
    for i, p in enumerate(parts):
        if p in ("src", "lib"):
            return parts[i - 1] if i > 0 else "unknown"
    return Path(filename).parent.name or "unknown"


def demangle_batch(names):
    try:
        result = subprocess.run(
            ["rustfilt"],
            input="\n".join(names),
            capture_output=True,
            text=True,
            check=True,
        )
        demangled = result.stdout.splitlines()
        if len(demangled) == len(names):
            return demangled
    except Exception:
        pass
    return names


def value_score(uncovered_lines, total_lines, uncovered_funcs):
    """
    Score a file/crate by how valuable it is to cover.
    Higher = more impactful to write tests for.
    Formula: uncovered lines weighted by how uncovered the file is.
    Large files that are mostly uncovered score highest.
    """
    if total_lines == 0:
        return 0
    gap_ratio = uncovered_lines / total_lines
    return uncovered_lines * gap_ratio + uncovered_funcs * 10


# ── Load data ─────────────────────────────────────────────────────────────────

print(f"Loading {JSON_PATH} ...", flush=True)
with open(JSON_PATH) as f:
    data = json.load(f)

files_data = data["data"][0]["files"]
funcs_data = data["data"][0]["functions"]
print(f"  {len(files_data):,} files, {len(funcs_data):,} functions\n")

# ── File-level analysis ───────────────────────────────────────────────────────

crate_stats = defaultdict(lambda: {
    "total_lines": 0,
    "covered_lines": 0,
    "total_funcs": 0,
    "covered_funcs": 0,
    "files": [],
})

zero_cov_files = []
low_cov_files = []

for file_entry in files_data:
    fname = file_entry["filename"]
    s = file_entry["summary"]

    total_lines = s["lines"]["count"]
    covered_lines = s["lines"]["covered"]
    pct = s["lines"]["percent"]

    if total_lines < MIN_LINES:
        continue

    crate = extract_crate(fname)
    crate_stats[crate]["total_lines"] += total_lines
    crate_stats[crate]["covered_lines"] += covered_lines
    crate_stats[crate]["total_funcs"] += s["functions"]["count"]
    crate_stats[crate]["covered_funcs"] += s["functions"]["covered"]
    crate_stats[crate]["files"].append({
        "filename": fname,
        "pct": pct,
        "covered": covered_lines,
        "total": total_lines,
        "uncovered": total_lines - covered_lines,
    })

    short = fname.split("/compiler/")[-1] if "/compiler/" in fname else fname
    if pct == 0.0:
        zero_cov_files.append((total_lines, short, fname))
    elif pct < LOW_COV_THRESH:
        low_cov_files.append((total_lines - covered_lines, pct, short))

# ── Function-level analysis ───────────────────────────────────────────────────

crate_uncovered_funcs = defaultdict(list)

for func in funcs_data:
    if func.get("count", 1) == 0:
        for fname in func.get("filenames", []):
            if ".cargo/registry" in fname or "/library/" in fname:
                break
            if "/compiler/" not in fname:
                break
            crate = extract_crate(fname)
            short_file = fname.split("/compiler/")[-1]
            crate_uncovered_funcs[crate].append({
                "name": func["name"],
                "file": short_file,
            })
            break

print("Demangling function names ...", flush=True)
all_crates = list(crate_uncovered_funcs.keys())
all_names_flat = [fn["name"] for c in all_crates for fn in crate_uncovered_funcs[c]]
demangled_flat = demangle_batch(all_names_flat)
idx = 0
for c in all_crates:
    for fn in crate_uncovered_funcs[c]:
        fn["name"] = demangled_flat[idx]
        idx += 1

# ── Priority targets ──────────────────────────────────────────────────────────

compiler_crates = {
    k: v for k, v in crate_stats.items()
    if not k.startswith("std::") and k != "unknown"
}

# Score each crate
crate_scores = []
for crate, s in compiler_crates.items():
    tl = s["total_lines"]
    cl = s["covered_lines"]
    if tl == 0:
        continue
    uncov_funcs = len(crate_uncovered_funcs.get(crate, []))
    score = value_score(tl - cl, tl, uncov_funcs)
    crate_scores.append((score, crate, s, uncov_funcs))
crate_scores.sort(reverse=True)

# Score each zero-coverage file
zero_scored = []
for total, short, fname in zero_cov_files:
    crate = extract_crate(fname)
    uncov_funcs = sum(1 for fn in crate_uncovered_funcs.get(crate, []) if fn["file"] in fname)
    score = value_score(total, total, uncov_funcs)
    zero_scored.append((score, total, short))
zero_scored.sort(reverse=True)

# ── Output helpers ────────────────────────────────────────────────────────────

def text_out(lines):
    print("\n".join(lines))

def md_out(lines):
    print("\n".join(lines))

SEP = "=" * 70
HSEP = "---"

def section(title, char="="):
    if FORMAT == "md":
        return [f"## {title}", ""]
    return [SEP, title, SEP]

def table_row_text(crate, lpct, uncov, tl, fpct):
    return f"{crate:<40} {lpct:>6.1f}%  {uncov:>7,}  {tl:>7,}  {fpct:>5.1f}%"

def table_row_md(crate, lpct, uncov, tl, fpct):
    return f"| `{crate}` | {lpct:.1f}% | {uncov:,} | {tl:,} | {fpct:.1f}% |"

# ── Report ────────────────────────────────────────────────────────────────────

out = []

# Totals first in markdown (good for GitHub summary)
total_l = sum(s["total_lines"] for s in crate_stats.values())
total_cl = sum(s["covered_lines"] for s in crate_stats.values())
total_f = sum(s["total_funcs"] for s in crate_stats.values())
total_cf = sum(s["covered_funcs"] for s in crate_stats.values())
lpct_total = 100 * total_cl / total_l if total_l else 0
fpct_total = 100 * total_cf / total_f if total_f else 0

if FORMAT == "md":
    out += [
        "# Rust Compiler Coverage Report",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Line coverage | **{lpct_total:.1f}%** ({total_cl:,} / {total_l:,}) |",
        f"| Function coverage | **{fpct_total:.1f}%** ({total_cf:,} / {total_f:,}) |",
        f"| Zero-coverage files | {len(zero_cov_files)} |",
        f"| Low-coverage files (<{LOW_COV_THRESH}%) | {len(low_cov_files)} |",
        "",
    ]

# 1. Priority targets — the most valuable gaps to fill
out += section("TOP PRIORITY TARGETS  (highest-value coverage gaps)")
if FORMAT == "md":
    out += [
        "These are the compiler crates with the most uncovered code, weighted by gap size.",
        "Start here when writing new tests.",
        "",
        "| Crate | Lines% | Uncovered | Total | Fn% |",
        "|-------|--------|-----------|-------|-----|",
    ]
else:
    out += [f"{'Crate':<40} {'Lines%':>7}  {'Uncov':>7}  {'Total':>7}  {'Fn%':>6}", "-" * 70]

for score, crate, s, uncov_funcs in crate_scores[:15]:
    tl = s["total_lines"]
    cl = s["covered_lines"]
    lpct = 100.0 * cl / tl
    tf = s["total_funcs"]
    cf = s["covered_funcs"]
    fpct = 100.0 * cf / tf if tf else 0.0
    uncov = tl - cl
    if FORMAT == "md":
        out.append(table_row_md(crate, lpct, uncov, tl, fpct))
    else:
        out.append(table_row_text(crate, lpct, uncov, tl, fpct))

# 2. Per-crate summary (full)
out += [""]
out += section("CRATE COVERAGE SUMMARY  (sorted by uncovered lines, compiler crates only)")
if FORMAT == "md":
    out += [
        "| Crate | Lines% | Uncovered | Total | Fn% |",
        "|-------|--------|-----------|-------|-----|",
    ]
else:
    out += [f"{'Crate':<40} {'Lines%':>7}  {'Uncov':>7}  {'Total':>7}  {'Fn%':>6}", "-" * 70]

sorted_crates = sorted(
    compiler_crates.items(),
    key=lambda kv: kv[1]["total_lines"] - kv[1]["covered_lines"],
    reverse=True,
)
for crate, s in sorted_crates:
    tl = s["total_lines"]
    cl = s["covered_lines"]
    if tl == 0:
        continue
    lpct = 100.0 * cl / tl
    tf = s["total_funcs"]
    cf = s["covered_funcs"]
    fpct = 100.0 * cf / tf if tf else 0.0
    uncov = tl - cl
    if FORMAT == "md":
        out.append(table_row_md(crate, lpct, uncov, tl, fpct))
    else:
        out.append(table_row_text(crate, lpct, uncov, tl, fpct))

# 3. Zero-coverage files
out += [""]
out += section(f"ZERO-COVERAGE FILES  (≥{MIN_LINES} lines, by value score)")
for _, total, short in zero_scored[:TOP_N]:
    if FORMAT == "md":
        out.append(f"- `{short}` ({total} lines)")
    else:
        out.append(f"  {total:>5} lines  {short}")
if len(zero_cov_files) > TOP_N:
    out.append(f"  ... and {len(zero_cov_files) - TOP_N} more")

# 4. Low-coverage files
out += [""]
out += section(f"LOW-COVERAGE FILES  (<{LOW_COV_THRESH}%, sorted by uncovered lines)")
low_cov_files.sort(reverse=True)
for uncov, pct, short in low_cov_files[:TOP_N]:
    if FORMAT == "md":
        out.append(f"- `{short}` — {pct:.1f}% ({uncov} uncovered lines)")
    else:
        out.append(f"  {pct:>5.1f}%  {uncov:>5} uncov  {short}")
if len(low_cov_files) > TOP_N:
    out.append(f"  ... and {len(low_cov_files) - TOP_N} more")

# 5. Top uncovered functions per crate
out += [""]
out += section("UNCOVERED FUNCTIONS BY CRATE  (compiler crates, top 20 crates)")
compiler_uncov = dict(crate_uncovered_funcs)
sorted_uncov_crates = sorted(compiler_uncov.items(), key=lambda kv: len(kv[1]), reverse=True)

for crate, funcs in sorted_uncov_crates[:20]:
    if FORMAT == "md":
        out += [f"### `{crate}` ({len(funcs)} uncovered functions)", ""]
    else:
        out.append(f"\n  {crate}  ({len(funcs)} uncovered functions)")
    for fn in funcs[:5]:
        if FORMAT == "md":
            out.append(f"- `{fn['name'][:80]}`  \n  `{fn['file']}`")
        else:
            out.append(f"    - {fn['name'][:70]}")
            out.append(f"      {fn['file']}")
    if len(funcs) > 5:
        out.append(f"    ... and {len(funcs) - 5} more")
    if FORMAT == "md":
        out.append("")

# 6. Totals
out += [""]
out += section("TOTALS")
if FORMAT == "md":
    out += [
        f"| Lines | {total_cl:,} / {total_l:,} | {lpct_total:.1f}% |",
        f"| Functions | {total_cf:,} / {total_f:,} | {fpct_total:.1f}% |",
    ]
else:
    out += [
        f"  Lines:     {total_cl:,} / {total_l:,}  ({lpct_total:.1f}%)",
        f"  Functions: {total_cf:,} / {total_f:,}  ({fpct_total:.1f}%)",
        f"  Zero-cov files:  {len(zero_cov_files)}",
        f"  Low-cov files:   {len(low_cov_files)}",
    ]

print("\n".join(out))
