#!/usr/bin/env bash
# tars-dashboard.sh — Start/stop the web dashboard process
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../tars.conf"

DASHBOARD_PID_FILE="${TARS_DASHBOARD_PID:-${TARS_STATE}/dashboard.pid}"

dashboard_is_running() {
    if [ -f "$DASHBOARD_PID_FILE" ]; then
        local pid
        pid=$(cat "$DASHBOARD_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return 0
        else
            rm -f "$DASHBOARD_PID_FILE"
        fi
    fi
    return 1
}

cmd_start() {
    if pid=$(dashboard_is_running); then
        echo "Dashboard is already running (PID: ${pid})"
        exit 1
    fi

    echo "Starting TARS Dashboard on port ${TARS_DASHBOARD_PORT:-8420}..."
    nohup "${TARS_PYTHON}" -m lib.dashboard --port "${TARS_DASHBOARD_PORT:-8420}" >> "${TARS_LOGS}/dashboard.log" 2>&1 &
    local dash_pid=$!
    echo "$dash_pid" > "$DASHBOARD_PID_FILE"
    echo "Dashboard started (PID: ${dash_pid})"
    echo "URL: http://localhost:${TARS_DASHBOARD_PORT:-8420}"
    echo "Logs: tail -f ${TARS_LOGS}/dashboard.log"
}

cmd_stop() {
    if pid=$(dashboard_is_running); then
        echo "Stopping Dashboard (PID: ${pid})..."
        kill "$pid"
        for i in $(seq 1 15); do
            if ! kill -0 "$pid" 2>/dev/null; then
                rm -f "$DASHBOARD_PID_FILE"
                echo "Dashboard stopped"
                return
            fi
            sleep 1
        done
        echo "Force killing Dashboard..."
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$DASHBOARD_PID_FILE"
        echo "Dashboard killed"
    else
        echo "Dashboard is not running"
    fi
}

cmd_status() {
    if pid=$(dashboard_is_running); then
        echo "Dashboard is running (PID: ${pid})"
        echo "URL: http://localhost:${TARS_DASHBOARD_PORT:-8420}"
    else
        echo "Dashboard is not running"
    fi
}

# --- Main ---
ACTION="${1:-}"

case "$ACTION" in
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    *)
        echo "Usage: tars-dashboard.sh start|stop|status"
        exit 1
        ;;
esac
