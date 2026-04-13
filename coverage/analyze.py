#!/usr/bin/env python3
"""
analyze.py — Find and prioritize coverage gaps in the Rust compiler.

Usage:
    python3 coverage/analyze.py [coverage.json] [OPTIONS]

Options:
    --top N                 Number of entries to show per section (default: 30)
    --min-lines N           Minimum lines to include a file (default: 5)
    --format text|md        Output format (default: text)
    --baseline FILE         Previous coverage JSON for delta comparison
    --git-enrich            Enrich with git history (ICE fixes, bug activity, churn)
    --lookback N            Git history lookback in months (default: 18)
    --suggest-tests         Show test stubs for top-ranked uncovered files

Output:
    - Per-crate summary (sorted by uncovered lines)
    - Zero-coverage files
    - Low-coverage files (< threshold)
    - Top uncovered functions per crate
    - Prioritized action targets (highest-value gaps to fill)
    - Delta comparison (with --baseline)
    - Git-enriched priority ranking (with --git-enrich)
    - Test suggestions (with --suggest-tests)
"""

import json
import math
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# --- Config ---
JSON_PATH = "coverage/out-compiler/coverage.json"
TOP_N = 30
MIN_LINES = 5
LOW_COV_THRESH = 50
FORMAT = "text"
BASELINE_PATH = None
GIT_ENRICH = False
LOOKBACK_MONTHS = 18
SUGGEST_TESTS = False

args = sys.argv[1:]
while args:
    a = args.pop(0)
    if a == "--top" and args:
        TOP_N = int(args.pop(0))
    elif a == "--min-lines" and args:
        MIN_LINES = int(args.pop(0))
    elif a == "--format" and args:
        FORMAT = args.pop(0)
    elif a == "--baseline" and args:
        BASELINE_PATH = args.pop(0)
    elif a == "--git-enrich":
        GIT_ENRICH = True
    elif a == "--lookback" and args:
        LOOKBACK_MONTHS = int(args.pop(0))
    elif a == "--suggest-tests":
        SUGGEST_TESTS = True
        GIT_ENRICH = True  # suggestions need git data for best ranking
    elif not a.startswith("--"):
        JSON_PATH = a


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def rel_path_from_compiler(filename: str) -> str:
    if "/compiler/" in filename:
        return filename.split("/compiler/")[-1]
    return filename


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
    if total_lines == 0:
        return 0
    gap_ratio = uncovered_lines / total_lines
    return uncovered_lines * gap_ratio + uncovered_funcs * 10


# ---------------------------------------------------------------------------
# Git enrichment
# ---------------------------------------------------------------------------

@dataclass
class GitSignal:
    ice_fixes: int = 0
    bug_fixes: int = 0
    churn_count: int = 0
    days_since_last_fix: int = 9999


def query_git_history(lookback_months: int) -> dict:
    """Query git history for compiler/ files. Returns rel_path -> GitSignal."""
    repo_root = None
    try:
        repo_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        print("  WARNING: git not available, skipping git enrichment", file=sys.stderr)
        return {}

    print(f"  Querying git history ({lookback_months} months)...", file=sys.stderr)

    cmd = [
        "git", "-C", repo_root, "log",
        f"--since={lookback_months} months ago",
        "--format=---COMMIT--- %ad %s",
        "--date=unix",
        "--name-only",
        "--", "compiler/",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    lines = result.stdout.splitlines()

    # Build a map: compiler-relative path -> raw signals
    signals = {}  # path -> [ice_fixes, bug_fixes, churn, last_fix_timestamp]

    current_is_ice = False
    current_is_fix = False
    current_ts = 0

    for line in lines:
        if line.startswith("---COMMIT---"):
            parts = line.split(" ", 2)
            try:
                current_ts = int(parts[1])
            except (IndexError, ValueError):
                current_ts = 0
            subject = parts[2] if len(parts) > 2 else ""
            current_is_ice = bool(re.search(
                r'\bice\b|internal.compiler.error|span_bug|span_delayed_bug',
                subject, re.I))
            current_is_fix = bool(re.search(
                r'\bfix\b|\bbug\b|\bregression\b|\bpanic\b|\bICE\b',
                subject, re.I))
        elif line.startswith("compiler/") and line.endswith(".rs"):
            rel = line[len("compiler/"):]
            if rel not in signals:
                signals[rel] = [0, 0, 0, 0]
            signals[rel][2] += 1  # churn
            if current_is_ice:
                signals[rel][0] += 1
                if current_ts > signals[rel][3]:
                    signals[rel][3] = current_ts
            if current_is_fix:
                signals[rel][1] += 1
                if current_ts > signals[rel][3]:
                    signals[rel][3] = current_ts

    # Convert to GitSignal objects
    now_ts = datetime.now(timezone.utc).timestamp()
    result_map = {}
    for rel, sig in signals.items():
        gs = GitSignal(
            ice_fixes=sig[0],
            bug_fixes=sig[1],
            churn_count=sig[2],
        )
        if sig[3] > 0:
            gs.days_since_last_fix = int((now_ts - sig[3]) / 86400)
        result_map[rel] = gs

    print(f"  Found git signals for {len(result_map)} files", file=sys.stderr)
    return result_map


def compute_priority_score(uncovered_lines, total_lines, uncovered_funcs,
                           git: GitSignal) -> float:
    """Score a file by how valuable it is to cover, using coverage + git signal."""
    if total_lines == 0:
        return 0.0

    coverage_gap = 1.0 - (total_lines - uncovered_lines) / total_lines
    uncovered_mass = math.log1p(uncovered_lines) / math.log1p(5000)
    bug_signal = math.log1p(git.ice_fixes * 3 + git.bug_fixes)
    recency = math.exp(-git.days_since_last_fix / 180.0)
    churn_signal = math.log1p(git.churn_count) / math.log1p(200)

    return round(
        coverage_gap   * 3.0 +
        uncovered_mass * 2.0 +
        bug_signal     * 4.0 +
        recency        * 2.0 +
        churn_signal   * 0.5,
        2
    )


def generate_reasons(rel_path, uncovered_lines, line_pct, uncovered_funcs,
                     git: GitSignal) -> list:
    """Generate human-readable reasons why a file is high priority."""
    reasons = []

    if git.ice_fixes >= 3:
        reasons.append(f"{git.ice_fixes} ICE fixes in lookback period")
    elif git.ice_fixes > 0:
        reasons.append(f"{git.ice_fixes} ICE fix(es) in lookback period")

    if git.bug_fixes >= 5:
        reasons.append(f"{git.bug_fixes} bug fixes — high activity")
    elif git.bug_fixes > 0:
        reasons.append(f"{git.bug_fixes} bug fix(es)")

    if line_pct == 0.0:
        reasons.append("completely uncovered (0% lines)")
    elif line_pct < 10.0:
        reasons.append(f"nearly dark ({line_pct:.1f}% lines covered)")

    if uncovered_lines > 2000:
        reasons.append(f"massive gap: {uncovered_lines:,} uncovered lines")
    elif uncovered_lines > 500:
        reasons.append(f"large gap: {uncovered_lines:,} uncovered lines")

    if uncovered_funcs > 50:
        reasons.append(f"{uncovered_funcs} uncovered functions")

    if git.days_since_last_fix < 90:
        reasons.append(f"recently active ({git.days_since_last_fix}d since last fix)")

    p = rel_path.lower()
    if "suggest" in p or "diagnostic" in p or "error_reporting" in p:
        reasons.append("error-path code — needs invalid test programs to exercise")

    return reasons


# ---------------------------------------------------------------------------
# Baseline delta comparison
# ---------------------------------------------------------------------------

def load_baseline(path: str) -> dict:
    """Load a baseline coverage JSON and return file -> {lines_pct, fn_pct, covered, total}."""
    with open(path) as f:
        data = json.load(f)

    baseline = {}
    for file_entry in data["data"][0]["files"]:
        fname = file_entry["filename"]
        s = file_entry["summary"]
        baseline[fname] = {
            "line_pct": s["lines"]["percent"],
            "covered": s["lines"]["covered"],
            "total": s["lines"]["count"],
            "fn_pct": s["functions"]["percent"],
        }
    return baseline


def compute_crate_deltas(crate_stats, baseline_crate_stats):
    """Compute per-crate coverage deltas between current and baseline."""
    deltas = {}
    all_crates = set(crate_stats.keys()) | set(baseline_crate_stats.keys())
    for crate in all_crates:
        curr = crate_stats.get(crate)
        base = baseline_crate_stats.get(crate)
        if curr and base:
            curr_pct = 100.0 * curr["covered_lines"] / curr["total_lines"] if curr["total_lines"] else 0
            base_pct = 100.0 * base["covered_lines"] / base["total_lines"] if base["total_lines"] else 0
            delta = curr_pct - base_pct
            deltas[crate] = {
                "current_pct": curr_pct,
                "baseline_pct": base_pct,
                "delta": delta,
                "current_uncov": curr["total_lines"] - curr["covered_lines"],
                "baseline_uncov": base["total_lines"] - base["covered_lines"],
            }
        elif curr and not base:
            curr_pct = 100.0 * curr["covered_lines"] / curr["total_lines"] if curr["total_lines"] else 0
            deltas[crate] = {
                "current_pct": curr_pct,
                "baseline_pct": None,
                "delta": None,
                "current_uncov": curr["total_lines"] - curr["covered_lines"],
                "baseline_uncov": None,
                "note": "new crate",
            }
    return deltas


# ---------------------------------------------------------------------------
# Test suggestions
# ---------------------------------------------------------------------------

def classify_fn(fn_name: str) -> tuple:
    """Return (category, test_hint) based on the demangled function name."""
    base = re.sub(r'::\{[^}]+\}', '', fn_name)
    base = re.sub(r'<[^>]+>', '', base)
    n = base.lower()

    if any(x in n for x in ["report_", "_err", "_error", "emit_", "suggest_",
                             "note_", "span_err", "prohibit"]):
        return ("error-path", "invalid program that triggers this error")
    if any(x in n for x in ["async", "await", "poll", "future"]):
        return ("async", "async fn with .await")
    if any(x in n for x in ["drop", "borrow", "lifetime", "region"]):
        return ("ownership", "program exercising drop/borrow/lifetime rules")
    if any(x in n for x in ["const", "eval", "static_"]):
        return ("const-eval", "const expression or const fn")
    if any(x in n for x in ["trait", "vtable", "dyn_", "object_safe"]):
        return ("trait", "trait impl or dyn dispatch")
    if any(x in n for x in ["cast", "coerce", "transmute"]):
        return ("cast", "type cast or coercion")
    if any(x in n for x in ["pattern", "match_", "scrutinee"]):
        return ("pattern", "match expression with relevant patterns")
    if any(x in n for x in ["generic", "param", "bound", "where_"]):
        return ("generics", "generic function / struct with bounds")
    if any(x in n for x in ["macro", "expand"]):
        return ("macro", "macro invocation")
    if any(x in n for x in ["closure", "upvar", "capture"]):
        return ("closure", "function with a closure / capture")
    return ("general", "program exercising this code path")


def short_fn_name(fn_name: str) -> str:
    """Extract a readable short name from a fully-qualified demangled Rust name."""
    name = re.sub(r'::\{[^}]+\}', '', fn_name)
    name = re.sub(r'<[^<>]*>', '', name)
    name = re.sub(r'[<>]+', '', name)
    name = re.sub(r'::+', '::', name).strip(':')
    parts = [p for p in name.split("::") if p]
    if len(parts) >= 2:
        return "::".join(parts[-2:])
    return parts[-1] if parts else fn_name


TEST_TEMPLATES = {
    "error-path":  '// triggers {fn}\n//@ compile-flags:\nfn main() {{ /* write code that causes this error */ }}',
    "async":       'async fn foo() {{ /* add await points */ }}\nfn main() {{ }}',
    "closure":     'fn main() {{\n    let x = 1;\n    let f = || x;\n    f();\n}}',
    "ownership":   'fn main() {{\n    let s = String::from("hi");\n    drop(s);\n}}',
    "const-eval":  'const X: usize = /* expression */;\nfn main() {{ let _ = X; }}',
    "trait":       'trait Foo {{ fn bar(&self); }}\nstruct S;\nimpl Foo for S {{ fn bar(&self) {{}} }}\nfn main() {{ S.bar(); }}',
    "cast":        'fn main() {{\n    let x: i32 = 1;\n    let _ = x as f64;\n}}',
    "pattern":     'fn main() {{\n    match (1u8, true) {{\n        (0, _) => {{}}\n        (_, b) => {{}}\n    }}\n}}',
    "generics":    'fn foo<T: Clone>(x: T) -> T {{ x.clone() }}\nfn main() {{ foo(1i32); }}',
    "macro":       'macro_rules! m {{ ($x:expr) => {{ $x }} }}\nfn main() {{ m!(1); }}',
    "general":     'fn main() {{ /* exercise the relevant code path */ }}',
}

# Map crate names to likely test directories
CRATE_TEST_DIRS = {
    "rustc_borrowck": ["tests/ui/borrowck", "tests/ui/nll"],
    "rustc_hir_typeck": ["tests/ui/typeck", "tests/ui/type"],
    "rustc_trait_selection": ["tests/ui/traits", "tests/ui/associated-types"],
    "rustc_resolve": ["tests/ui/resolve", "tests/ui/imports"],
    "rustc_mir_transform": ["tests/mir-opt"],
    "rustc_mir_build": ["tests/mir-opt", "tests/ui/pattern"],
    "rustc_const_eval": ["tests/ui/consts"],
    "rustc_expand": ["tests/ui/macros"],
    "rustc_builtin_macros": ["tests/ui/macros"],
    "rustc_parse": ["tests/ui/parser"],
    "rustc_codegen_ssa": ["tests/codegen-llvm"],
    "rustc_codegen_llvm": ["tests/codegen-llvm"],
    "rustc_lint": ["tests/ui/lint"],
    "rustc_infer": ["tests/ui/inference", "tests/ui/type"],
    "rustc_pattern_analysis": ["tests/ui/pattern"],
    "rustc_incremental": ["tests/incremental"],
    "rustc_monomorphize": ["tests/codegen-units"],
}


def is_diagnostic_file(rel_path: str) -> bool:
    p = rel_path.lower()
    return any(x in p for x in ["suggest", "diagnostics/", "error_reporting/",
                                "borrowck_errors", "errors.rs"])


# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------

print(f"Loading {JSON_PATH} ...", flush=True)
with open(JSON_PATH) as f:
    data = json.load(f)

files_data = data["data"][0]["files"]
funcs_data = data["data"][0]["functions"]
print(f"  {len(files_data):,} files, {len(funcs_data):,} functions\n")

# Load baseline if provided
baseline_file_map = {}
baseline_crate_stats = defaultdict(lambda: {"total_lines": 0, "covered_lines": 0})
if BASELINE_PATH:
    print(f"Loading baseline {BASELINE_PATH} ...", flush=True)
    baseline_file_map = load_baseline(BASELINE_PATH)
    # Build baseline crate stats
    for fname, bdata in baseline_file_map.items():
        if bdata["total"] < MIN_LINES:
            continue
        crate = extract_crate(fname)
        baseline_crate_stats[crate]["total_lines"] += bdata["total"]
        baseline_crate_stats[crate]["covered_lines"] += bdata["covered"]

# Load git signals if requested
git_signals = {}
if GIT_ENRICH:
    git_signals = query_git_history(LOOKBACK_MONTHS)

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

# ── Git-enriched file ranking ─────────────────────────────────────────────────

git_ranked_files = []
if GIT_ENRICH:
    for file_entry in files_data:
        fname = file_entry["filename"]
        if "/compiler/" not in fname:
            continue
        s = file_entry["summary"]
        total_lines = s["lines"]["count"]
        covered_lines = s["lines"]["covered"]
        if total_lines < MIN_LINES:
            continue

        rel = rel_path_from_compiler(fname)
        uncov = total_lines - covered_lines
        uncov_funcs = s["functions"]["count"] - s["functions"]["covered"]
        git = git_signals.get(rel, GitSignal())

        score = compute_priority_score(uncov, total_lines, uncov_funcs, git)
        reasons = generate_reasons(rel, uncov, s["lines"]["percent"], uncov_funcs, git)

        git_ranked_files.append({
            "rel_path": rel,
            "crate": extract_crate(fname),
            "score": score,
            "line_pct": s["lines"]["percent"],
            "uncovered_lines": uncov,
            "uncovered_functions": uncov_funcs,
            "ice_fixes": git.ice_fixes,
            "bug_fixes": git.bug_fixes,
            "churn": git.churn_count,
            "days_since_last_fix": git.days_since_last_fix,
            "reasons": reasons,
            "is_diagnostic": is_diagnostic_file(rel),
        })

    git_ranked_files.sort(key=lambda r: r["score"], reverse=True)


# ── Output helpers ────────────────────────────────────────────────────────────

SEP = "=" * 70

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

# Totals
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

# ── Delta section (with --baseline) ──────────────────────────────────────────

if BASELINE_PATH:
    # Compute baseline totals
    base_total_l = sum(b["total"] for b in baseline_file_map.values()
                       if b["total"] >= MIN_LINES)
    base_total_cl = sum(b["covered"] for b in baseline_file_map.values()
                        if b["total"] >= MIN_LINES)
    base_lpct = 100 * base_total_cl / base_total_l if base_total_l else 0
    overall_delta = lpct_total - base_lpct

    out += section("COVERAGE DELTA  (vs baseline)")
    if FORMAT == "md":
        arrow = "+" if overall_delta >= 0 else ""
        out += [
            f"**Overall: {base_lpct:.1f}% -> {lpct_total:.1f}% ({arrow}{overall_delta:.1f}%)**",
            "",
        ]
    else:
        arrow = "+" if overall_delta >= 0 else ""
        out += [f"  Overall: {base_lpct:.1f}% -> {lpct_total:.1f}% ({arrow}{overall_delta:.1f}%)", ""]

    # Per-crate deltas
    deltas = compute_crate_deltas(dict(crate_stats), dict(baseline_crate_stats))

    # Show regressions first, then improvements
    regressions = [(c, d) for c, d in deltas.items()
                   if d.get("delta") is not None and d["delta"] < -0.1
                   and not c.startswith("std::") and c != "unknown"]
    improvements = [(c, d) for c, d in deltas.items()
                    if d.get("delta") is not None and d["delta"] > 0.1
                    and not c.startswith("std::") and c != "unknown"]

    regressions.sort(key=lambda x: x[1]["delta"])
    improvements.sort(key=lambda x: x[1]["delta"], reverse=True)

    if regressions:
        if FORMAT == "md":
            out += ["### Regressions", "",
                    "| Crate | Before | After | Delta | Uncov Lines |",
                    "|-------|--------|-------|-------|-------------|"]
        else:
            out += ["  REGRESSIONS:",
                    f"  {'Crate':<35} {'Before':>7} {'After':>7} {'Delta':>7} {'UncovL':>8}"]

        for crate, d in regressions[:15]:
            if FORMAT == "md":
                out.append(f"| `{crate}` | {d['baseline_pct']:.1f}% | {d['current_pct']:.1f}% "
                           f"| {d['delta']:+.1f}% | {d['current_uncov']:,} |")
            else:
                out.append(f"  {crate:<35} {d['baseline_pct']:>6.1f}% {d['current_pct']:>6.1f}% "
                           f"{d['delta']:>+6.1f}% {d['current_uncov']:>8,}")
        out.append("")

    if improvements:
        if FORMAT == "md":
            out += ["### Improvements", "",
                    "| Crate | Before | After | Delta |",
                    "|-------|--------|-------|-------|"]
        else:
            out += ["  IMPROVEMENTS:",
                    f"  {'Crate':<35} {'Before':>7} {'After':>7} {'Delta':>7}"]

        for crate, d in improvements[:15]:
            if FORMAT == "md":
                out.append(f"| `{crate}` | {d['baseline_pct']:.1f}% | {d['current_pct']:.1f}% "
                           f"| {d['delta']:+.1f}% |")
            else:
                out.append(f"  {crate:<35} {d['baseline_pct']:>6.1f}% {d['current_pct']:>6.1f}% "
                           f"{d['delta']:>+6.1f}%")
        out.append("")

    # New zero-coverage files (regressions)
    new_zeros = []
    for total, short, fname in zero_cov_files:
        base = baseline_file_map.get(fname)
        if base and base["line_pct"] > 0:
            new_zeros.append((base["line_pct"], short))
    if new_zeros:
        out += ["  NEW ZERO-COVERAGE FILES (were covered in baseline):"]
        for old_pct, short in new_zeros[:10]:
            out.append(f"    {short}  (was {old_pct:.1f}%)")
        out.append("")

# ── Git-enriched priority ranking ─────────────────────────────────────────────

if GIT_ENRICH and git_ranked_files:
    # Split into core vs diagnostic
    core_files = [r for r in git_ranked_files if not r["is_diagnostic"]]
    diag_files = [r for r in git_ranked_files if r["is_diagnostic"]]

    out += [""]
    out += section("GIT-ENRICHED PRIORITY RANKING  (coverage gaps x bug history)")

    if FORMAT == "md":
        out += [
            "Files ranked by coverage gap weighted with ICE fixes, bug fixes, and churn.",
            "",
            "| Rank | Score | Line% | UncovL | ICE | File | Why |",
            "|------|-------|-------|--------|-----|------|-----|",
        ]
        for i, r in enumerate(core_files[:TOP_N], 1):
            reasons_str = "; ".join(r["reasons"][:2]) if r["reasons"] else ""
            short = r["rel_path"]
            out.append(f"| {i} | {r['score']:.1f} | {r['line_pct']:.1f}% | "
                       f"{r['uncovered_lines']:,} | {r['ice_fixes']} | "
                       f"`{short}` | {reasons_str} |")
    else:
        out += [f"{'Rank':>4}  {'Score':>6}  {'Line%':>6}  {'UncovL':>7}  "
                f"{'ICE':>3}  {'Bugs':>4}  File",
                "-" * 90]
        for i, r in enumerate(core_files[:TOP_N], 1):
            short = r["rel_path"]
            if len(short) > 55:
                short = "..." + short[-52:]
            out.append(f"{i:>4}  {r['score']:>6.1f}  {r['line_pct']:>5.1f}%  "
                       f"{r['uncovered_lines']:>7,}  {r['ice_fixes']:>3}  "
                       f"{r['bug_fixes']:>4}  {short}")
            for reason in r["reasons"][:2]:
                out.append(f"{'':>43}  -> {reason}")

    # Quick wins: tier-1 crate files, 0% coverage, small enough to easily cover
    quick_wins = [r for r in core_files
                  if r["line_pct"] == 0.0
                  and r["uncovered_lines"] <= 300
                  and (r["ice_fixes"] > 0 or r["bug_fixes"] > 0)]
    if quick_wins:
        out += [""]
        out += section("QUICK WINS  (0% coverage, <=300 lines, has bug/ICE history)")
        for r in quick_wins[:15]:
            if FORMAT == "md":
                out.append(f"- `{r['rel_path']}` ({r['uncovered_lines']} lines, "
                           f"{r['ice_fixes']} ICE, {r['bug_fixes']} bugs)")
            else:
                out.append(f"  {r['rel_path']:<65}  {r['uncovered_lines']:>4} lines  "
                           f"ICE:{r['ice_fixes']}  bugs:{r['bug_fixes']}")

    # Diagnostic appendix
    if diag_files:
        out += [""]
        out += section("DIAGNOSTIC / ERROR-PATH CODE  (needs invalid programs to exercise)")
        if FORMAT == "md":
            out += [
                "| Score | Line% | UncovL | File |",
                "|-------|-------|--------|------|",
            ]
        for r in diag_files[:15]:
            if FORMAT == "md":
                out.append(f"| {r['score']:.1f} | {r['line_pct']:.1f}% | "
                           f"{r['uncovered_lines']:,} | `{r['rel_path']}` |")
            else:
                out.append(f"  {r['line_pct']:>5.1f}%  {r['uncovered_lines']:>6,}L  {r['rel_path']}")

# ── Standard sections (always shown) ─────────────────────────────────────────

# 1. Priority targets
out += [""]
out += section("TOP PRIORITY TARGETS  (highest-value coverage gaps)")
if FORMAT == "md":
    out += [
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

# 2. Per-crate summary
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

# 3. Sweet-spot files (20-80% coverage — highest ROI for adding tests)
sweet_spot = []
for file_entry in files_data:
    fname = file_entry["filename"]
    if "/compiler/" not in fname:
        continue
    s = file_entry["summary"]
    pct = s["lines"]["percent"]
    total = s["lines"]["count"]
    uncov = total - s["lines"]["covered"]
    if total >= MIN_LINES and 20.0 <= pct <= 80.0 and uncov >= 20:
        short = fname.split("/compiler/")[-1]
        sweet_spot.append((uncov, pct, short))

sweet_spot.sort(reverse=True)
if sweet_spot:
    out += [""]
    out += section("SWEET-SPOT FILES  (20-80% covered — highest ROI for new tests)")
    if FORMAT == "md":
        out += [
            "These files already have test infrastructure exercising them. "
            "A small additional test can push coverage significantly higher.",
            "",
        ]
    for uncov, pct, short in sweet_spot[:TOP_N]:
        if FORMAT == "md":
            out.append(f"- `{short}` — {pct:.1f}% ({uncov} uncovered lines)")
        else:
            out.append(f"  {pct:>5.1f}%  {uncov:>5} uncov  {short}")
    if len(sweet_spot) > TOP_N:
        out.append(f"  ... and {len(sweet_spot) - TOP_N} more")

# 4. Zero-coverage files
out += [""]
out += section(f"ZERO-COVERAGE FILES  (>={MIN_LINES} lines, by value score)")
for _, total, short in zero_scored[:TOP_N]:
    if FORMAT == "md":
        out.append(f"- `{short}` ({total} lines)")
    else:
        out.append(f"  {total:>5} lines  {short}")
if len(zero_cov_files) > TOP_N:
    out.append(f"  ... and {len(zero_cov_files) - TOP_N} more")

# 5. Low-coverage files
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

# 6. Top uncovered functions per crate
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

# 7. Totals
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

# ── Test suggestions (with --suggest-tests) ───────────────────────────────────

if SUGGEST_TESTS:
    # Build a map: rel_path -> list of demangled uncovered function names
    file_uncov_fns = defaultdict(list)
    for crate, funcs in crate_uncovered_funcs.items():
        for fn in funcs:
            file_uncov_fns[fn["file"]].append(fn["name"])

    print(f"\n{'=' * 70}")
    print(f"SUGGESTED NEXT TESTS  (top {min(TOP_N, 10)} priority files)")
    print("  For each file: uncovered functions are classified and a test stub is shown.")
    print("-" * 70)

    targets = git_ranked_files if git_ranked_files else []
    # Fall back to zero-coverage + low-coverage if no git ranking
    if not targets:
        for _, total, short in zero_scored[:10]:
            targets.append({"rel_path": short, "crate": extract_crate(short),
                            "score": 0, "line_pct": 0, "is_diagnostic": False})

    shown = 0
    for r in targets:
        if shown >= 10:
            break
        rel = r["rel_path"]
        fns = file_uncov_fns.get(rel, [])

        print(f"\n[{rel}]  score={r['score']}  {r['line_pct']:.1f}% covered")

        # Suggest test directories
        crate = r["crate"]
        test_dirs = CRATE_TEST_DIRS.get(crate, [])
        if test_dirs:
            print(f"  test dirs: {', '.join(test_dirs)}")

        if not fns:
            if r["is_diagnostic"]:
                print("  -> write an invalid program to trigger errors in this file")
            else:
                print("  -> no uncovered function detail for this file")
            shown += 1
            continue

        # Group functions by category
        by_cat = defaultdict(list)
        for fn in fns:
            cat, hint = classify_fn(fn)
            by_cat[cat].append((fn, hint))

        # Pick the dominant category
        dominant = max(by_cat, key=lambda c: len(by_cat[c]))
        examples_raw = [fn for fn, _ in by_cat[dominant][:3]]
        examples_short = [short_fn_name(fn) for fn in examples_raw]

        print(f"  category : {dominant} ({len(by_cat[dominant])} functions)")
        print(f"  targets  : {', '.join(examples_short)}"
              + (f" (+{len(fns) - 3} more)" if len(fns) > 3 else ""))
        print(f"  hint     : {by_cat[dominant][0][1]}")
        print(f"  stub     :")
        template = TEST_TEMPLATES.get(dominant, TEST_TEMPLATES["general"])
        for line in template.format(fn=examples_short[0]).splitlines():
            print(f"    {line}")

        shown += 1

    print(f"\n{'=' * 70}\n")
