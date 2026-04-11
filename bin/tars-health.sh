#!/usr/bin/env bash
# tars-health.sh — Watchdog / health check
# Monitors daemon health, auto-restarts on crash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../tars.conf"

HEALTH_LOG="${TARS_LOGS}/health.log"

log() {
    local level="$1"; shift
    echo "[$(date +"${LOG_DATE_FMT}")] [${level}] [health] $*" | tee -a "$HEALTH_LOG"
}

check_daemon() {
    if [ ! -f "$TARS_PID_FILE" ]; then
        return 1
    fi

    local pid
    pid=$(cat "$TARS_PID_FILE")
    if kill -0 "$pid" 2>/dev/null; then
        return 0
    else
        rm -f "$TARS_PID_FILE"
        return 1
    fi
}

check_disk_space() {
    local usage
    usage=$(df "$TARS_HOME" | awk 'NR==2 {print $5}' | tr -d '%')
    if [ "$usage" -gt 90 ]; then
        log "WARN" "Disk usage at ${usage}%"
        return 1
    fi
    return 0
}

check_log_size() {
    # Rotate logs if daemon.log exceeds 50MB
    local log_file="${TARS_LOGS}/daemon.log"
    if [ -f "$log_file" ]; then
        local size
        size=$(stat -c %s "$log_file" 2>/dev/null || stat -f %z "$log_file" 2>/dev/null || echo "0")
        if [ "$size" -gt 52428800 ]; then
            log "INFO" "Rotating daemon.log (${size} bytes)"
            mv "$log_file" "${log_file}.$(date +%Y%m%d%H%M%S)"
            # Keep only last 5 rotated logs
            ls -t "${log_file}".* 2>/dev/null | tail -n +6 | xargs rm -f 2>/dev/null || true
        fi
    fi
}

check_stale_locks() {
    # Remove lock files older than 2 hours
    if [ -d "${TARS_STATE}/locks" ]; then
        find "${TARS_STATE}/locks" -type f -mmin +120 -delete 2>/dev/null || true
    fi
}

run_health_check() {
    local issues=0

    # Check daemon
    if ! check_daemon; then
        log "ERROR" "Daemon is not running"
        ((issues++))

        # Auto-restart
        log "INFO" "Attempting auto-restart..."
        nohup "${TARS_BIN}/tars-daemon.sh" >> "${TARS_LOGS}/daemon.log" 2>&1 &
        log "INFO" "Daemon restarted (PID: $!)"

        # Notify Discord
        cd "$TARS_HOME"
        "${TARS_PYTHON}" -c "
from lib.discord_logger import DiscordLogger
d = DiscordLogger()
d.log_warning('Daemon crashed and was auto-restarted by watchdog')
" 2>/dev/null || true
    fi

    # Check disk space
    if ! check_disk_space; then
        ((issues++))
        cd "$TARS_HOME"
        "${TARS_PYTHON}" -c "
from lib.discord_logger import DiscordLogger
d = DiscordLogger()
d.log_warning('Disk space running low (>90%)')
" 2>/dev/null || true
    fi

    # Maintenance
    check_log_size
    check_stale_locks

    # Check circuit breakers
    cd "$TARS_HOME"
    "${TARS_PYTHON}" -c "
from lib.error_analyzer import ErrorAnalyzer
ea = ErrorAnalyzer()
statuses = ea.get_all_statuses()
for project, status in statuses.items():
    if status['is_locked']:
        print(f'Circuit breaker active: {project} ({status[\"remaining_lockout\"]}s remaining)')
" 2>/dev/null || true

    if [ "$issues" -eq 0 ]; then
        log "INFO" "Health check passed"
    else
        log "WARN" "Health check found ${issues} issue(s)"
    fi

    return "$issues"
}

# --- Main ---
if [ "${1:-}" = "--once" ]; then
    # Single health check (called by tars.sh health)
    run_health_check
    exit $?
fi

# Continuous watchdog mode
log "INFO" "Health watchdog started"

while true; do
    run_health_check || true
    sleep "$TARS_HEALTH_INTERVAL" || break
done
