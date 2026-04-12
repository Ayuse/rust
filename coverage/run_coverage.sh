#!/usr/bin/env bash
# Coverage pipeline for the Rust compiler.
# - Hooks into compiletest via COMPILETEST_LLVM_PROFILE_DIR so tests run with correct
#   flags, editions, and revisions (not hardcoded --edition 2021)
# - Uses LLVM's %m online profile merging: each rustc process atomically merges
#   counters into a shared pool file via file locking — no external watcher needed
# - Platform-agnostic: macOS (aarch64/x86_64) and Linux (x86_64/aarch64)
#
# Usage:
#   bash coverage/run_coverage.sh                        # full mode: all test suites
#   COVERAGE_MODE=quick bash coverage/run_coverage.sh    # quick: default 5 ui suites
#   COVERAGE_MODE=quick COVERAGE_SUITES="tests/ui/traits,tests/mir-opt" bash coverage/run_coverage.sh

set -euo pipefail

# ── Platform detection ────────────────────────────────────────────────────────
_OS=$(uname -s)
_ARCH=$(uname -m)

if [ "$_OS" = "Darwin" ]; then
  [ "$_ARCH" = "arm64" ] && HOST_TRIPLE="aarch64-apple-darwin" || HOST_TRIPLE="x86_64-apple-darwin"
  DYLIB_EXT="dylib"
else
  [ "$_ARCH" = "aarch64" ] && HOST_TRIPLE="aarch64-unknown-linux-gnu" || HOST_TRIPLE="x86_64-unknown-linux-gnu"
  DYLIB_EXT="so"
fi

echo "Platform: $HOST_TRIPLE"

RUSTC="build/$HOST_TRIPLE/stage1/bin/rustc"

# Pick the best available LLVM tool.
#
# On Linux: prefer system tools (apt-installed, compiled for x86-64 baseline).
#   Build tools compiled from source on one runner can crash with SIGILL when
#   restored from cache on a different runner with fewer CPU features.
# On macOS: prefer build tools (version-matched to the compiler).
_pick_llvm_tool() {
  local name="$1"
  if [ "$_OS" = "Linux" ]; then
    # System first on Linux — avoids SIGILL from cached cross-runner binaries
    local sys
    sys=$(command -v "$name" 2>/dev/null || \
      ls /usr/bin/${name}-* 2>/dev/null | sort -V | tail -1)
    [ -n "$sys" ] && { echo "$sys"; return; }
  fi
  # Build tools (CI download or source-built) — version-matched to compiler
  local c
  for c in \
    "build/$HOST_TRIPLE/ci-llvm/bin/$name" \
    "build/$HOST_TRIPLE/llvm/bin/$name"; do
    [ -x "$c" ] && { echo "$c"; return; }
  done
  # Last resort: system (macOS or Linux without apt llvm)
  command -v "$name" 2>/dev/null || \
    ls /usr/bin/${name}-* 2>/dev/null | sort -V | tail -1
}

LLVM_PROFDATA=$(_pick_llvm_tool llvm-profdata)
LLVM_COV=$(_pick_llvm_tool llvm-cov)

if [ -z "$LLVM_PROFDATA" ] || [ -z "$LLVM_COV" ]; then
  echo "ERROR: llvm-profdata/llvm-cov not found in build/ or system PATH."
  echo "Install llvm or ensure stage1 build completed."
  exit 1
fi
echo "llvm-profdata: $LLVM_PROFDATA"
echo "llvm-cov:      $LLVM_COV"

PROFRAW_DIR=/tmp/rust-coverage/profraw
PROFDATA=coverage/out-compiler/merged.profdata
HTML_OUT=coverage/html-compiler
JSON_OUT=coverage/out-compiler/coverage.json

# COVERAGE_MODE: "full" runs all compiletest suites; "quick" runs user-specified suites
COVERAGE_MODE="${COVERAGE_MODE:-full}"
# COVERAGE_SUITES: comma-separated list of test paths (quick mode only)
COVERAGE_SUITES="${COVERAGE_SUITES:-}"

# Dynamically find the active librustc_driver — hash changes on every rebuild
DYLIB=$(find "build/$HOST_TRIPLE/stage1-rustc/$HOST_TRIPLE/release/deps" \
  -name "librustc_driver-*.$DYLIB_EXT" 2>/dev/null | head -1)
if [ -z "$DYLIB" ]; then
  echo "ERROR: librustc_driver dylib not found. Has stage1 been built?"
  exit 1
fi
echo "Using dylib: $DYLIB"
echo "Coverage mode: $COVERAGE_MODE"

# Test suites
#
# Full mode runs all compiletest suites. tests/ui is split into subdirectories
# so cleanup runs between each one — running it as a single suite produces ~100GB
# of test artifacts with no cleanup opportunity, exhausting disk.
#
# Quick mode accepts user-specified suites via COVERAGE_SUITES (comma-separated).
# Defaults to 5 key ui suites if none specified.

# Suites excluded from full mode:
#   tests/auxiliary    — helper files, not a test suite
#   tests/build-std   — requires special setup (rebuilds std)
#   tests/rustdoc-gui  — requires browser/webdriver
#   tests/rustdoc-js-std — tests JS search index, not compiler code
FULL_SUITES=(
  tests/assembly-llvm
  tests/codegen-llvm
  tests/codegen-units
  tests/coverage
  tests/coverage-run-rustdoc
  tests/crashes
  tests/debuginfo
  tests/incremental
  tests/mir-opt
  tests/pretty
  tests/run-make
  tests/run-make-cargo
  tests/rustdoc-html
  tests/rustdoc-js
  tests/rustdoc-json
  tests/rustdoc-ui
  tests/ui-fulldeps
)

DEFAULT_QUICK_SUITES="tests/ui/async-await,tests/ui/coroutine,tests/ui/traits,tests/ui/closures,tests/ui/impl-trait"

TEST_DIRS=()

if [ "$COVERAGE_MODE" = "full" ]; then
  # Add non-ui suites as-is (they're small enough to run without splitting)
  for suite in "${FULL_SUITES[@]}"; do
    [ -d "$suite" ] && TEST_DIRS+=("$suite")
  done
  # Split tests/ui into subdirectories for disk management
  while IFS= read -r d; do
    TEST_DIRS+=("$d")
  done < <(find tests/ui -mindepth 1 -maxdepth 1 -type d | sort)
else
  # Quick mode: use user-specified suites or defaults
  SUITES="${COVERAGE_SUITES:-$DEFAULT_QUICK_SUITES}"
  IFS=',' read -ra SUITE_LIST <<< "$SUITES"
  for s in "${SUITE_LIST[@]}"; do
    # Trim whitespace
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    [ -n "$s" ] && TEST_DIRS+=("$s")
  done
fi

echo "Test suites to run: ${#TEST_DIRS[@]}"

# ── Step 1: Clean old profraw and batch files ─────────────────────────────────
echo "[1/5] Cleaning old profraw files..."
mkdir -p "$PROFRAW_DIR" coverage/out-compiler
find "$PROFRAW_DIR" -name "*.profraw" -delete 2>/dev/null || true

# Cleanup background processes on exit
CLEANUP_PID=""
trap '[ -n "$CLEANUP_PID" ] && kill "$CLEANUP_PID" 2>/dev/null' EXIT

# ── Step 2: Run tests with online profile merging ────────────────────────────
# Uses LLVM's %m profraw modifier: each rustc process atomically merges its
# counters into a shared pool file using file locking. No watcher needed —
# the LLVM runtime handles merging, eliminating TOCTOU races and keeping
# disk usage bounded to one pool file (~50MB) instead of thousands (~100GB).
echo "[2/5] Running tests with online profile merging (%m)..."

# Unset GITHUB_ACTIONS so bootstrap uses the non-CI commit detection path for
# download-ci-llvm. On a fork's CI, the CI path (HEAD^1) resolves to our own
# commits which have no CI artifacts. The non-CI path searches for bors-authored
# commits and finds the real upstream merge base.
unset GITHUB_ACTIONS

TEST_ARTIFACT_DIR="build/$HOST_TRIPLE/test"

# Background cleanup: continuously delete old test artifacts (compiled binaries,
# object files, debug info) to prevent disk exhaustion. Compiletest accumulates
# these in build/$HOST/test/ — with 3500+ instrumented-build tests they reach
# ~100GB. We only need the profraw files (in /tmp), so old artifacts are safe to
# delete. Files older than 1 minute are guaranteed to be from completed tests.
_artifact_cleanup_loop() {
  while true; do
    sleep 30
    if [ -d "$TEST_ARTIFACT_DIR" ]; then
      # Delete test artifact FILES older than 2 min — instrumented binaries are large
      # and we only need the profraw files (written to /tmp, not here).
      # IMPORTANT: Do NOT delete directories — compiletest creates output dirs before
      # writing .out files. Deleting empty dirs causes "No such file or directory"
      # panics. Directory cleanup happens in the between-suite rm -rf.
      find "$TEST_ARTIFACT_DIR" -type f -mmin +2 -delete 2>/dev/null || true
    fi
    df -h / | awk 'NR==2{printf "    [cleanup] disk: %s free\n", $4}'
  done
}
_artifact_cleanup_loop &
CLEANUP_PID=$!

TOTAL=${#TEST_DIRS[@]}
DONE=0

for dir in "${TEST_DIRS[@]}"; do
  [ -d "$dir" ] || continue
  DONE=$((DONE + 1))
  echo "  [$DONE/$TOTAL] $dir"

  # LLVM_PROFILE_FILE=/dev/null suppresses profraw from the compiletest binary itself.
  # compose_and_run_compiler() in runtest.rs overrides it with the correct path for rustc children.
  # The %m modifier in runtest.rs makes each rustc process atomically merge its
  # counters into a shared pool file via file locking — no external watcher needed.
  RUSTFLAGS_BOOTSTRAP="-Cinstrument-coverage -Ccodegen-units=1" \
  COMPILETEST_LLVM_PROFILE_DIR="$PROFRAW_DIR" \
  LLVM_PROFILE_FILE=/dev/null \
  ./x.py test --stage 1 "$dir" --no-fail-fast --force-rerun --keep-stage 1 || true

  # Bulk cleanup between suites
  if [ -d "$TEST_ARTIFACT_DIR" ]; then
    echo "    Cleaning test artifacts: $(du -sh "$TEST_ARTIFACT_DIR" | cut -f1)"
    rm -rf "$TEST_ARTIFACT_DIR"
  fi
  echo "    [$DONE/$TOTAL] done — disk: $(df -h / | awk 'NR==2{print $4}') free"
done

kill "$CLEANUP_PID" 2>/dev/null || true
CLEANUP_PID=""

# ── Step 3: Merge profraw pool files ─────────────────────────────────────────
echo "[3/5] Merging profraw pool files..."
PROFRAW_LIST=$(mktemp /tmp/profraw_list.XXXXXX)
trap 'rm -f "$PROFRAW_LIST"' EXIT
find "$PROFRAW_DIR" -name "*.profraw" > "$PROFRAW_LIST"
PROFRAW_COUNT=$(wc -l < "$PROFRAW_LIST" | tr -d ' ')
echo "    Pool files found: $PROFRAW_COUNT"

if [ "$PROFRAW_COUNT" -eq 0 ]; then
  echo "ERROR: No profraw pool files found. Is the compiler instrumented and runtest.rs patched?"
  echo ""
  echo "Build command:"
  echo "  RUSTFLAGS_BOOTSTRAP=\"-Cinstrument-coverage -Ccodegen-units=1\" \\"
  echo "    ./x.py build library --stage 1 --set build.profiler=true"
  rm -f "$PROFRAW_LIST"
  exit 1
fi

"$LLVM_PROFDATA" merge -sparse -failure-mode=all -f "$PROFRAW_LIST" -o "$PROFDATA"
echo "    Merged to: $PROFDATA ($(du -sh "$PROFDATA" | cut -f1))"
rm -f "$PROFRAW_LIST"

# Free disk: remove profraw pool files — no longer needed
rm -rf "$PROFRAW_DIR"
df -h / | awk 'NR==2{printf "    Disk after cleanup: %s free\n", $4}'

# ── Step 4: Generate reports ──────────────────────────────────────────────────
echo "[4/5] Generating reports..."

echo ""
echo "=== Coverage Summary (compiler sources only) ==="
"$LLVM_COV" report "$RUSTC" \
  --object "$DYLIB" \
  --instr-profile="$PROFDATA" \
  --ignore-filename-regex='\.cargo|library|/tmp' \
  2>&1 | tail -5

# HTML report (~425MB) — skip in CI to save disk, generate only when running locally
if [ -z "${CI:-}" ]; then
  echo ""
  echo "=== HTML report (Fuchsia --show-directory-coverage) ==="
  "$LLVM_COV" show "$RUSTC" \
    --object "$DYLIB" \
    --instr-profile="$PROFDATA" \
    --format=html \
    --show-directory-coverage \
    --ignore-filename-regex='\.cargo|library|/tmp' \
    -o "$HTML_OUT"
  echo "    HTML written to: $HTML_OUT"
  echo "    Open: open $HTML_OUT/index.html"
else
  echo ""
  echo "=== Skipping HTML report in CI (saves ~425MB disk) ==="
fi

# ── Step 5: Export machine-readable JSON ──────────────────────────────────────
echo ""
echo "[5/5] Exporting JSON coverage data..."
"$LLVM_COV" export "$RUSTC" \
  --object "$DYLIB" \
  --instr-profile="$PROFDATA" \
  --ignore-filename-regex='\.cargo|library|/tmp' \
  --format=text \
  > "$JSON_OUT"
echo "    JSON written to: $JSON_OUT"
echo "    Size: $(du -sh "$JSON_OUT" | cut -f1)"

# ── Step 6: Analyze coverage gaps ────────────────────────────────────────────
echo ""
echo "[6/6] Analyzing coverage gaps..."
python3 coverage/analyze.py "$JSON_OUT"
