#!/usr/bin/env bash
# tars-daemon.sh — Main daemon loop (the heartbeat)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../tars.conf"

# Ensure state/logs dirs exist
mkdir -p "$TARS_STATE" "$TARS_LOGS" "$TARS_REPOS" "${TARS_STATE}/locks"

DAEMON_LOG="${TARS_LOGS}/daemon.log"

# Single-instance guard — file lock that auto-releases on exit.
# Atomic: two daemons racing to start will have exactly one win the lock.
LOCK_FILE="${TARS_STATE}/locks/daemon.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    LOCK_HOLDER=$(cat "$LOCK_FILE" 2>/dev/null || echo "unknown")
    echo "[$(date +"${LOG_DATE_FMT}")] [INFO] [daemon] Another tars-daemon holds the lock (PID: ${LOCK_HOLDER}). Aborting." \
        | tee -a "$DAEMON_LOG" >&2
    exit 0
fi
echo $$ > "$LOCK_FILE"

log() {
    local level="$1"; shift
    echo "[$(date +"${LOG_DATE_FMT}")] [${level}] [daemon] $*" | tee -a "$DAEMON_LOG"
}

# Write PID file
echo $$ > "$TARS_PID_FILE"
log "INFO" "Daemon started (PID: $$)"

# Trap signals for clean shutdown
RUNNING=true
trap 'RUNNING=false; log "INFO" "Shutdown signal received"' SIGTERM SIGINT SIGHUP

# Track daily summary timing
LAST_SUMMARY_DATE=""

# Track per-task failure counts (task_id -> count)
TASK_FAILURES_FILE="${TARS_STATE}/task_failures.json"
# Initialize if missing
[ -f "$TASK_FAILURES_FILE" ] || echo '{}' > "$TASK_FAILURES_FILE"

get_task_failures() {
    jq -r --arg id "$1" '.[$id] // 0' "$TASK_FAILURES_FILE" 2>/dev/null || echo 0
}

record_task_failure() {
    local task_id="$1"
    local current
    current=$(get_task_failures "$task_id")
    local new_count=$(( current + 1 ))
    local tmp="${TASK_FAILURES_FILE}.tmp"
    jq --arg id "$task_id" --argjson count "$new_count" '.[$id] = $count' \
        "$TASK_FAILURES_FILE" > "$tmp" 2>/dev/null && mv "$tmp" "$TASK_FAILURES_FILE"
    echo "$new_count"
}

clear_task_failures() {
    local task_id="$1"
    local tmp="${TASK_FAILURES_FILE}.tmp"
    jq --arg id "$task_id" 'del(.[$id])' \
        "$TASK_FAILURES_FILE" > "$tmp" 2>/dev/null && mv "$tmp" "$TASK_FAILURES_FILE"
}

while $RUNNING; do
    CYCLE_START=$(date +%s)

    log "INFO" "=== Daemon cycle start ==="

    # Check token budget — if over limit, pause until it resets
    BUDGET_CHECK=$("${TARS_PYTHON}" -c "
from lib.token_tracker import TokenTracker
t = TokenTracker()
if t.can_spend():
    print('ok')
else:
    wait = t.get_wait_time()
    print(f'wait:{wait}')
" 2>/dev/null || echo "ok")

    if [[ "$BUDGET_CHECK" == wait:* ]]; then
        WAIT_SECS="${BUDGET_CHECK#wait:}"
        WAIT_MINS=$(( WAIT_SECS / 60 ))
        log "INFO" "Token budget hit — pausing for ${WAIT_MINS}m (${WAIT_SECS}s)"
        # Write pause state for dashboard
        "${TARS_PYTHON}" -c "
import json, time
json.dump({'paused': True, 'reason': 'token_budget', 'wait_s': ${WAIT_SECS}, 'until': time.time() + ${WAIT_SECS}}, open('${TARS_STATE}/daemon_status.json', 'w'))
" 2>/dev/null || true
        sleep "$WAIT_SECS" &
        wait $! || true
        rm -f "${TARS_STATE}/daemon_status.json"
        continue
    fi

    # Check if we should send daily summary
    TODAY=$(date +%Y-%m-%d)
    if [ "$LAST_SUMMARY_DATE" != "$TODAY" ]; then
        # Send summary for previous day (skip on first run)
        if [ -n "$LAST_SUMMARY_DATE" ]; then
            log "INFO" "Sending daily summary"
            "${TARS_PYTHON}" -c "
from lib.metrics import MetricsTracker
m = MetricsTracker()
m.send_daily_summary()
" 2>/dev/null || log "WARN" "Failed to send daily summary"
        fi
        LAST_SUMMARY_DATE="$TODAY"
    fi

    # Run scheduler to get next task
    NEXT_TASK=$("${TARS_BIN}/tars-scheduler.sh" 2>&1) || {
        log "INFO" "No tasks available: ${NEXT_TASK}"
        log "INFO" "Sleeping for ${TARS_POLL_INTERVAL}s"
        sleep "$TARS_POLL_INTERVAL" &
        wait $! || true  # Allow signal interruption during sleep
        continue
    }

    if [ -z "$NEXT_TASK" ] || [ "$NEXT_TASK" = "null" ]; then
        log "INFO" "No tasks in queue, sleeping ${TARS_POLL_INTERVAL}s"
        sleep "$TARS_POLL_INTERVAL" &
        wait $! || true
        continue
    fi

    # Parse task
    PROJECT=$(echo "$NEXT_TASK" | jq -r '.project')
    TASK_JSON=$(echo "$NEXT_TASK" | jq -c '.task')
    TASK_TITLE=$(echo "$TASK_JSON" | jq -r '.title')
    TASK_ID=$(echo "$TASK_JSON" | jq -r '.id // "unknown"')
    TASK_SOURCE=$(echo "$TASK_JSON" | jq -r '.source // "unknown"')

    # Check for permanent failure marker (permission errors, etc.)
    PERM_FAIL_FILE="${TARS_STATE}/task_perm_fail_${TASK_ID}"
    if [ -f "$PERM_FAIL_FILE" ]; then
        log "WARN" "Task ${TASK_ID} has a permanent failure — skipping"
        TARS_TASK_ID="$TASK_ID" "${TARS_PYTHON}" -c "
import os
from lib.task_manager import TaskManager
TaskManager().complete_task(os.environ['TARS_TASK_ID'])
" 2>/dev/null || true
        rm -f "$PERM_FAIL_FILE"
        clear_task_failures "$TASK_ID"
        continue
    fi

    # Check per-task retry limit
    TASK_FAIL_COUNT=$(get_task_failures "$TASK_ID")
    if [ "$TASK_FAIL_COUNT" -ge "$MAX_TASK_RETRIES" ]; then
        log "WARN" "Task ${TASK_ID} has failed ${TASK_FAIL_COUNT} times (max ${MAX_TASK_RETRIES}), skipping permanently"
        # Mark as completed so scheduler stops picking it up
        TARS_TASK_ID="$TASK_ID" "${TARS_PYTHON}" -c "
import os
from lib.task_manager import TaskManager
TaskManager().complete_task(os.environ['TARS_TASK_ID'])
" 2>/dev/null || true
        TARS_PROJECT="$PROJECT" TARS_TITLE="$TASK_TITLE" "${TARS_PYTHON}" -c "
import os
from lib.metrics import MetricsTracker
MetricsTracker().record_task(os.environ['TARS_PROJECT'], os.environ['TARS_TITLE'], 'abandoned')
" 2>/dev/null || true
        clear_task_failures "$TASK_ID"
        continue
    fi

    log "INFO" "Executing task: ${TASK_TITLE} (project: ${PROJECT})"

    # Write current task state for dashboard (use jq to safely create JSON)
    jq -n --arg id "$TASK_ID" --arg title "$TASK_TITLE" --arg project "$PROJECT" \
          --arg source "$TASK_SOURCE" --argjson started "$(date +%s)" \
        '{id:$id, title:$title, project:$project, started:$started, source:$source}' \
        > "${TARS_STATE}/current_task.json" 2>/dev/null || true

    # Check circuit breaker
    CB_OK=$(TARS_PROJECT="$PROJECT" "${TARS_PYTHON}" -c "
import os
from lib.error_analyzer import ErrorAnalyzer
print('yes' if not ErrorAnalyzer().is_locked(os.environ['TARS_PROJECT']) else 'no')
" 2>/dev/null || echo "yes")

    if [ "$CB_OK" = "no" ]; then
        rm -f "${TARS_STATE}/current_task.json"
        log "WARN" "Circuit breaker active for ${PROJECT}, skipping"
        sleep "$TARS_POLL_INTERVAL" &
        wait $! || true
        continue
    fi

    # Resource gate — don't start heavy local-model work if the box is saturated.
    # (Concurrency is already 1: the worker call below is synchronous.)
    GATE_REASON=$("${TARS_BIN}/tars-resource-gate.sh" 2>/dev/null) || {
        rm -f "${TARS_STATE}/current_task.json"
        log "INFO" "Resource gate: waiting (${GATE_REASON})"
        sleep "$TARS_POLL_INTERVAL" &
        wait $! || true
        continue
    }

    # Execute task via worker
    if "${TARS_BIN}/tars-worker.sh" "$PROJECT" "$TASK_JSON"; then
        rm -f "${TARS_STATE}/current_task.json"
        log "INFO" "Task completed successfully: ${TASK_TITLE}"
        clear_task_failures "$TASK_ID"

        # Mark task as completed so it's not picked up again
        TARS_TASK_ID="$TASK_ID" "${TARS_PYTHON}" -c "
import os
from lib.task_manager import TaskManager
TaskManager().complete_task(os.environ['TARS_TASK_ID'])
" 2>/dev/null || true

        # Record metrics (use env vars, not string embedding)
        TARS_PROJECT="$PROJECT" TARS_TITLE="$TASK_TITLE" "${TARS_PYTHON}" -c "
import os
from lib.metrics import MetricsTracker
MetricsTracker().record_task(os.environ['TARS_PROJECT'], os.environ['TARS_TITLE'], 'success')
" 2>/dev/null || true
    else
        rm -f "${TARS_STATE}/current_task.json"
        FAIL_COUNT=$(record_task_failure "$TASK_ID")
        log "ERROR" "Task failed: ${TASK_TITLE} (attempt ${FAIL_COUNT}/${MAX_TASK_RETRIES})"

        TARS_PROJECT="$PROJECT" TARS_TITLE="$TASK_TITLE" "${TARS_PYTHON}" -c "
import os
from lib.metrics import MetricsTracker
MetricsTracker().record_task(os.environ['TARS_PROJECT'], os.environ['TARS_TITLE'], 'failed')
" 2>/dev/null || true
    fi

    # Brief pause between tasks
    CYCLE_ELAPSED=$(( $(date +%s) - CYCLE_START ))
    if [ "$CYCLE_ELAPSED" -lt 30 ]; then
        PAUSE=$(( 30 - CYCLE_ELAPSED ))
        log "INFO" "Pausing ${PAUSE}s before next cycle"
        sleep "$PAUSE" &
        wait $! || true
    fi
done

# Cleanup
rm -f "$TARS_PID_FILE" "${TARS_STATE}/current_task.json" "${TARS_STATE}/daemon_status.json"
log "INFO" "Daemon stopped"
