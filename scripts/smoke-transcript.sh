#!/usr/bin/env bash
# smoke-transcript.sh — capture a full, replayable transcript of a fresh-install
# smoke test (see docs/internal/first-run-smoke-test-handoff.md).
#
# The 2026-08-16 run left findings but no record of the literal commands run,
# in what order, with what output — only a hand-written narrative. This wraps
# every `lanegate` invocation in the current shell so a fresh run produces:
#   <dir>/commands.jsonl   — one line per command: ts, cwd, argv, exit code, duration
#   <dir>/<NNN>-<cmd>.out  — full stdout for that command
#   <dir>/<NNN>-<cmd>.err  — full stderr for that command
#
# Usage: in the scratch project folder, before running any lanegate command:
#   source /path/to/scripts/smoke-transcript.sh
# Every `lanegate ...` typed afterward in this shell is transparently logged.
# Real `lanegate` output still goes to your terminal as normal.

_SMOKE_TRANSCRIPT_DIR="${SMOKE_TRANSCRIPT_DIR:-$(pwd)/smoke-transcript}"
mkdir -p "$_SMOKE_TRANSCRIPT_DIR"
_SMOKE_TRANSCRIPT_LOG="$_SMOKE_TRANSCRIPT_DIR/commands.jsonl"
_SMOKE_TRANSCRIPT_BIN="$(command -v lanegate)"
_SMOKE_TRANSCRIPT_SEQ_FILE="$_SMOKE_TRANSCRIPT_DIR/.seq"
[ -f "$_SMOKE_TRANSCRIPT_SEQ_FILE" ] || echo 0 > "$_SMOKE_TRANSCRIPT_SEQ_FILE"

if [ -z "$_SMOKE_TRANSCRIPT_BIN" ]; then
    echo "smoke-transcript: no 'lanegate' on PATH — source this after lanegate is installed" >&2
    return 1 2>/dev/null || exit 1
fi

lanegate() {
    local seq
    seq=$(($(cat "$_SMOKE_TRANSCRIPT_SEQ_FILE") + 1))
    echo "$seq" > "$_SMOKE_TRANSCRIPT_SEQ_FILE"
    local label
    label=$(printf '%03d-%s' "$seq" "${1:-unknown}")
    local out_file="$_SMOKE_TRANSCRIPT_DIR/${label}.out"
    local err_file="$_SMOKE_TRANSCRIPT_DIR/${label}.err"
    local ts_start ts_end exit_code duration_ms

    ts_start=$(date -u +%s.%N)
    "$_SMOKE_TRANSCRIPT_BIN" "$@" > >(tee "$out_file") 2> >(tee "$err_file" >&2)
    exit_code=$?
    ts_end=$(date -u +%s.%N)
    duration_ms=$(awk -v a="$ts_start" -v b="$ts_end" 'BEGIN{printf "%.0f", (b-a)*1000}')

    python3 - "$_SMOKE_TRANSCRIPT_LOG" "$seq" "$exit_code" "$duration_ms" "$out_file" "$err_file" "$(pwd)" "$@" <<'PYEOF'
import json, sys, datetime

log_path, seq, exit_code, duration_ms, out_file, err_file, cwd = sys.argv[1:8]
argv = sys.argv[8:]

record = {
    "seq": int(seq),
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "cwd": cwd,
    "argv": ["lanegate"] + argv,
    "exit_code": int(exit_code),
    "duration_ms": int(duration_ms),
    "stdout_file": out_file,
    "stderr_file": err_file,
}
with open(log_path, "a") as f:
    f.write(json.dumps(record) + "\n")
PYEOF

    return "$exit_code"
}

echo "smoke-transcript: logging every 'lanegate' call to $_SMOKE_TRANSCRIPT_DIR (commands.jsonl + per-command .out/.err)"
