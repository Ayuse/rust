#!/usr/bin/env bash
# Coverage pipeline for the Rust compiler.
# - Hooks into compiletest via COMPILETEST_LLVM_PROFILE_DIR so tests run with correct
#   flags, editions, and revisions (not hardcoded --edition 2021)
# - Eager merging via filesystem events:
#     Linux  — inotifywait (close_write): zero grace period, instant safe merge
#     macOS  — fswatch (Updated) + size-stability check, or polling fallback
# - Platform-agnostic: macOS (aarch64/x86_64) and Linux (x86_64/aarch64)
#
# Usage:
#   bash coverage/run_coverage.sh                        # quick mode: 5 key suites
#   COVERAGE_MODE=full bash coverage/run_coverage.sh     # full tests/ui

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
BATCH_DIR=/tmp/rust-coverage/batches
PROFDATA=coverage/out-compiler/merged.profdata
HTML_OUT=coverage/html-compiler
JSON_OUT=coverage/out-compiler/coverage.json

# COVERAGE_MODE: "quick" runs 5 key suites; "full" runs all of tests/ui
COVERAGE_MODE="${COVERAGE_MODE:-quick}"

# Watcher batch size: max profraw files to merge in one llvm-profdata call.
# Each file is ~15MB; 200 × 15MB = 3GB per merge — fast on SSD, bounded memory.
WATCHER_BATCH=200

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
if [ "$COVERAGE_MODE" = "full" ]; then
  TEST_DIRS=( tests/ui )
else
  TEST_DIRS=(
    tests/ui/async-await
    tests/ui/coroutine
    tests/ui/traits
    tests/ui/closures
    tests/ui/impl-trait
  )
fi

# ── Step 1: Clean old profraw and batch files ─────────────────────────────────
echo "[1/5] Cleaning old profraw and batch files..."
mkdir -p "$PROFRAW_DIR" "$BATCH_DIR" coverage/out-compiler
find "$PROFRAW_DIR" -name "*.profraw" -delete 2>/dev/null || true
find "$BATCH_DIR" -name "*.profdata" -delete 2>/dev/null || true

# ── Watcher ───────────────────────────────────────────────────────────────────
#
# Merges profraw files into a running accumulated profdata as they are written,
# keeping peak disk usage bounded to WATCHER_BATCH × ~15MB.
#
# Uses filesystem events for zero-latency detection:
#   Linux:  inotifywait --event close_write  (fires when fd is closed — file is complete)
#   macOS:  fswatch --event Updated + size-stability check (FSEvents has no close_write)
#           Falls back to polling if fswatch is not installed.
#
# Usage:
#   start_watcher <accumulated.profdata>
#   stop_watcher

WATCHER_PID=""
WATCHER_ACCUMULATED=""
_WATCHER_LIST=$(mktemp /tmp/profraw_list.XXXXXX)

# Core merge function: reads file list from $_WATCHER_LIST, merges into accumulated profdata.
# Uses -f (file list) to avoid ARG_MAX limits and -failure-mode=all to skip corrupt files.
_do_merge() {
  local accumulated="$1"
  local count="$2"
  [ "$count" -eq 0 ] && return 0

  if [ -f "$accumulated" ]; then
    "$LLVM_PROFDATA" merge -sparse -failure-mode=all \
      -f "$_WATCHER_LIST" "$accumulated" -o "${accumulated}.tmp"
    [ -s "${accumulated}.tmp" ] && mv "${accumulated}.tmp" "$accumulated" || \
      rm -f "${accumulated}.tmp"
  else
    "$LLVM_PROFDATA" merge -sparse -failure-mode=all \
      -f "$_WATCHER_LIST" -o "$accumulated" || true
  fi

  # Delete the merged profraw files
  while IFS= read -r f; do rm -f "$f"; done < "$_WATCHER_LIST"

  echo "    [watcher] merged $count profraw → $(du -sh "$accumulated" | cut -f1) accumulated"
}

# ── Linux watcher: inotifywait ────────────────────────────────────────────────
# close_write fires the instant rustc closes the profraw fd — no grace period needed.
# Accumulates events for up to 0.5s of idle time, then merges the batch.
_watcher_loop_linux() {
  local profraw_dir="$1"
  local accumulated="$2"

  # Raise the inotify event queue to handle high-throughput bursts.
  # At 200 files/sec, the default 16384 queue overflows in ~80s.
  sudo sysctl -w fs.inotify.max_queued_events=524288 2>/dev/null || true

  inotifywait -m -q \
    --event close_write \
    --format '%w%f' \
    "$profraw_dir" 2>/dev/null | \
  while true; do
    : > "$_WATCHER_LIST"
    count=0

    # Collect events until 0.5s of silence or WATCHER_BATCH files accumulated.
    # read -t 0.5 returns immediately on each event during a burst, and times
    # out after 0.5s of quiet — triggering the merge.
    while IFS= read -r -t 0.5 filepath; do
      [[ "$filepath" == *.profraw ]] || continue
      echo "$filepath" >> "$_WATCHER_LIST"
      count=$((count + 1))
      [ "$count" -ge "$WATCHER_BATCH" ] && break
    done

    [ "$count" -eq 0 ] && continue
    _do_merge "$accumulated" "$count"
  done
}

# ── macOS watcher: fswatch + size-stability ───────────────────────────────────
# FSEvents has no close_write equivalent. We watch for Updated events, then
# wait until the file size stops changing (typically < 100ms for rustc).
_watcher_loop_macos_fswatch() {
  local profraw_dir="$1"
  local accumulated="$2"

  fswatch -m poll_monitor \
    --event Updated \
    --format '%p' \
    "$profraw_dir" 2>/dev/null | \
  while true; do
    : > "$_WATCHER_LIST"
    count=0

    while IFS= read -r -t 0.5 filepath; do
      [[ "$filepath" == *.profraw ]] || continue
      # Size-stability check: poll until size stops changing.
      # rustc writes profraw at exit in one shot so this converges in 1-2 polls.
      local prev=-1 curr
      curr=$(stat -f%z "$filepath" 2>/dev/null) || continue
      while [ "$curr" -ne "$prev" ]; do
        prev=$curr
        sleep 0.05
        curr=$(stat -f%z "$filepath" 2>/dev/null) || break
      done
      echo "$filepath" >> "$_WATCHER_LIST"
      count=$((count + 1))
      [ "$count" -ge "$WATCHER_BATCH" ] && break
    done

    [ "$count" -eq 0 ] && continue
    _do_merge "$accumulated" "$count"
  done
}

# ── Polling watcher: fallback ─────────────────────────────────────────────────
# Used on macOS when fswatch is not installed, or as a universal fallback.
# Uses a marker file to find profraw files that have been idle for 3+ seconds.
_watcher_loop_polling() {
  local profraw_dir="$1"
  local accumulated="$2"

  while true; do
    sleep 5
    local marker
    marker=$(mktemp)
    sleep 3  # grace: files older than marker are safe to merge

    : > "$_WATCHER_LIST"
    count=0
    while IFS= read -r f; do
      echo "$f" >> "$_WATCHER_LIST"
      count=$((count + 1))
      [ "$count" -ge "$WATCHER_BATCH" ] && break
    done < <(find "$profraw_dir" -name "*.profraw" -not -newer "$marker" 2>/dev/null)
    rm -f "$marker"

    [ "$count" -lt 20 ] && continue  # don't bother for tiny batches
    _do_merge "$accumulated" "$count"
  done
}

# ── Dispatch: pick the best available watcher backend ────────────────────────
_watcher_loop() {
  local profraw_dir="$1"
  local accumulated="$2"

  if [ "$_OS" = "Linux" ] && command -v inotifywait >/dev/null 2>&1; then
    echo "    [watcher] backend: inotifywait (close_write)"
    _watcher_loop_linux "$profraw_dir" "$accumulated"
  elif [ "$_OS" = "Darwin" ] && command -v fswatch >/dev/null 2>&1; then
    echo "    [watcher] backend: fswatch (Updated + size-stability)"
    _watcher_loop_macos_fswatch "$profraw_dir" "$accumulated"
  else
    echo "    [watcher] backend: polling (install inotify-tools on Linux or fswatch on macOS for faster merging)"
    _watcher_loop_polling "$profraw_dir" "$accumulated"
  fi
}

start_watcher() {
  WATCHER_ACCUMULATED="$1"
  _watcher_loop "$PROFRAW_DIR" "$WATCHER_ACCUMULATED" &
  WATCHER_PID=$!
}

stop_watcher() {
  if [ -n "$WATCHER_PID" ]; then
    kill "$WATCHER_PID" 2>/dev/null || true
    wait "$WATCHER_PID" 2>/dev/null || true
    WATCHER_PID=""
  fi

  # Flush all remaining profraw in WATCHER_BATCH-sized batches.
  # No grace period needed — compiletest is done, no files still being written.
  while true; do
    : > "$_WATCHER_LIST"
    count=0
    while IFS= read -r f; do
      echo "$f" >> "$_WATCHER_LIST"
      count=$((count + 1))
      [ "$count" -ge "$WATCHER_BATCH" ] && break
    done < <(find "$PROFRAW_DIR" -name "*.profraw" 2>/dev/null)
    [ "$count" -eq 0 ] && break
    _do_merge "$WATCHER_ACCUMULATED" "$count"
  done
}

# Cleanup temp list file on exit
trap 'rm -f "$_WATCHER_LIST"' EXIT

# ── Step 2: Run tests via compiletest + eager merge ───────────────────────────
# All test directories are passed to a single x.py invocation so the stage1
# sysroot (std + test helpers) is built exactly once instead of once per suite.
echo "[2/5] Running tests via compiletest with eager profraw merging..."
echo "  Suites: ${TEST_DIRS[*]}"

# Filter to directories that actually exist
VALID_TEST_DIRS=()
for dir in "${TEST_DIRS[@]}"; do
  [ -d "$dir" ] && VALID_TEST_DIRS+=("$dir")
done

if [ "${#VALID_TEST_DIRS[@]}" -eq 0 ]; then
  echo "ERROR: None of the requested test directories exist."
  exit 1
fi

BATCH_PROFDATA="$BATCH_DIR/batch_1.profdata"
start_watcher "$BATCH_PROFDATA"

# LLVM_PROFILE_FILE=/dev/null suppresses profraw from the compiletest binary itself.
# compose_and_run_compiler() in runtest.rs overrides it with the correct path for rustc children.
RUSTFLAGS_BOOTSTRAP="-Cinstrument-coverage -Ccodegen-units=1" \
COMPILETEST_LLVM_PROFILE_DIR="$PROFRAW_DIR" \
LLVM_PROFILE_FILE=/dev/null \
./x.py test --stage 1 "${VALID_TEST_DIRS[@]}" --no-fail-fast --force-rerun --keep-stage 1 2>/dev/null || true

stop_watcher
echo "  Done — disk: $(df -h / | awk 'NR==2{print $4}') free"

PROFDATA_COUNT=$(find "$BATCH_DIR" -name "*.profdata" | wc -l | tr -d ' ')
echo "    Batches collected: $PROFDATA_COUNT"

if [ "$PROFDATA_COUNT" -eq 0 ]; then
  echo "ERROR: No profdata batches found. Is the compiler instrumented and runtest.rs patched?"
  echo ""
  echo "Build command:"
  echo "  RUSTFLAGS_BOOTSTRAP=\"-Cinstrument-coverage -Ccodegen-units=1\" \\"
  echo "    ./x.py build library --stage 1 --set build.profiler=true"
  exit 1
fi

# ── Step 3: Final merge of all batches ───────────────────────────────────────
echo "[3/5] Merging all batches..."
find "$BATCH_DIR" -name "*.profdata" > "$_WATCHER_LIST"
"$LLVM_PROFDATA" merge -sparse -f "$_WATCHER_LIST" -o "$PROFDATA"
echo "    Merged to: $PROFDATA ($(du -sh "$PROFDATA" | cut -f1))"

# ── Step 4: Generate reports ──────────────────────────────────────────────────
echo "[4/5] Generating reports..."

echo ""
echo "=== Coverage Summary (compiler sources only) ==="
"$LLVM_COV" report "$RUSTC" \
  --object "$DYLIB" \
  --instr-profile="$PROFDATA" \
  --ignore-filename-regex='\.cargo|library|/tmp' \
  2>&1 | tail -5

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
