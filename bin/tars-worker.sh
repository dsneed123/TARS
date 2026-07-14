#!/usr/bin/env bash
# tars-worker.sh — execute one task through the node-graph pipeline.
# Thin shim: budget gate here; everything else (git prep, plan, implement,
# verify, review, push, Discord, website callbacks) lives in lib/graph_executor.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../tars.conf"
cd "$TARS_HOME"
export PYTHONPATH="${TARS_HOME}:${PYTHONPATH:-}"

PROJECT="${1:?Usage: tars-worker.sh <project> <task_json>}"
TASK_JSON="${2:?Usage: tars-worker.sh <project> <task_json>}"

TASK_ID=$(echo "$TASK_JSON" | jq -r '.id // "unknown"')
LOG_FILE="${TARS_LOGS}/task_${TASK_ID}_$(date +%Y%m%d_%H%M%S).log"

# Task JSON goes through a temp file, never a string literal.
# (Name must not match the controller's worker-registration glob state/worker_*.)
TMPDATA=$(mktemp "${TARS_STATE}/wtmp_XXXXXX")

# Kill all descendants on exit so model subprocesses don't outlive the worker.
_kill_tree() {
    local parent=$1
    for child in $(pgrep -P "$parent" 2>/dev/null); do _kill_tree "$child"; done
    kill -9 "$parent" 2>/dev/null || true
}
_cleanup() {
    for child in $(pgrep -P $$ 2>/dev/null); do _kill_tree "$child"; done
    rm -f "$TMPDATA"
}
trap _cleanup EXIT INT TERM

log() {
    echo "[$(date +"${LOG_DATE_FMT}")] [$1] [worker:${TASK_ID}] ${*:2}" \
        | tee -a "$LOG_FILE" >> "${TARS_LOGS}/daemon.log"
}

# Budget gate (tokens are free locally, but the cap protects a Claude fallback).
BUDGET_OK=$("${TARS_PYTHON}" -c "
from lib.token_tracker import TokenTracker
print('yes' if TokenTracker().can_spend() else 'no')
" 2>/dev/null || echo "yes")
if [ "$BUDGET_OK" = "no" ]; then
    log WARN "Token budget exceeded, skipping task"
    exit 0
fi

echo "$TASK_JSON" > "$TMPDATA"
log INFO "Starting task via graph executor"
if "${TARS_PYTHON}" -m lib.graph_executor "$PROJECT" "$TMPDATA" >> "$LOG_FILE" 2>&1; then
    log INFO "Task completed"
else
    log ERROR "Task failed — see ${LOG_FILE} and state/runs/${TASK_ID}.json"
    exit 1
fi
