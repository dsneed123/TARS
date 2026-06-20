#!/usr/bin/env bash
# tars-controller.sh — manage the public Flask controller API as a daemon.
#   Usage: tars-controller.sh {start|stop|restart|status}
# Runs gunicorn detached (independent of the launching shell), tracks a PID
# file, and logs to logs/controller.log.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARS_HOME="$(cd "${SCRIPT_DIR}/.." && pwd)"
export TARS_HOME
# shellcheck disable=SC1090
source "${TARS_HOME}/tars.conf"

CTL_DIR="${TARS_HOME}/controller"
VENV="${CTL_DIR}/.venv"
PID_FILE="${TARS_STATE}/controller.pid"
LOG_FILE="${TARS_LOGS}/controller.log"
PORT="${TARS_CONTROLLER_PORT:-8420}"
WORKERS="${TARS_CONTROLLER_WORKERS:-4}"
# Long timeout: chat endpoints may run a large local model that takes minutes.
TIMEOUT="${TARS_CONTROLLER_TIMEOUT:-1200}"

mkdir -p "$TARS_STATE" "$TARS_LOGS"

is_running() {
    [ -f "$PID_FILE" ] || return 1
    local pid; pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && { echo "$pid"; return 0; }
    rm -f "$PID_FILE"; return 1
}

start() {
    if pid=$(is_running); then echo "Controller already running (PID ${pid}) on :${PORT}"; exit 0; fi
    if [ ! -x "${VENV}/bin/gunicorn" ]; then
        echo "Controller venv missing — run controller/start.sh once to create it." >&2; exit 1
    fi
    [ -n "${TARS_API_KEY:-}" ] || echo "WARNING: TARS_API_KEY unset — all requests will be rejected." >&2

    echo "Starting controller on 0.0.0.0:${PORT} (workers=${WORKERS}, timeout=${TIMEOUT}s)..."
    # setsid detaches into its own session so it survives this shell exiting.
    TARS_HOME="$TARS_HOME" setsid "${VENV}/bin/gunicorn" \
        --chdir "${CTL_DIR}" \
        --bind "0.0.0.0:${PORT}" \
        --workers "${WORKERS}" \
        --timeout "${TIMEOUT}" \
        --access-logfile "${LOG_FILE}" \
        --error-logfile "${LOG_FILE}" \
        "api:app" >>"${LOG_FILE}" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    sleep 2
    if kill -0 "$pid" 2>/dev/null; then
        echo "Controller started (PID ${pid}). Logs: ${LOG_FILE}"
    else
        echo "Controller failed to start — last log lines:" >&2
        tail -15 "${LOG_FILE}" >&2; rm -f "$PID_FILE"; exit 1
    fi
}

stop() {
    if pid=$(is_running); then
        echo "Stopping controller (PID ${pid})..."
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$PID_FILE"; echo "Stopped."
    else
        echo "Controller not running."
    fi
}

status() {
    if pid=$(is_running); then
        echo "Controller: RUNNING (PID ${pid}) on :${PORT}"
        curl -fsS "http://127.0.0.1:${PORT}/api/health" 2>/dev/null && echo || echo "  (health check did not respond yet)"
    else
        echo "Controller: stopped"
    fi
}

run() {
    # Foreground gunicorn — for systemd/supervisors (Type=simple). No PID file.
    # Threaded workers: model/chat requests can block for minutes, so use
    # gthread so a slow request occupies one THREAD, not a whole worker — fast
    # endpoints (health, status) stay responsive instead of starving.
    [ -n "${TARS_API_KEY:-}" ] || echo "WARNING: TARS_API_KEY unset — all requests will be rejected." >&2
    exec "${VENV}/bin/gunicorn" \
        --chdir "${CTL_DIR}" \
        --bind "0.0.0.0:${PORT}" \
        --worker-class gthread \
        --workers "${TARS_CONTROLLER_WORKERS:-2}" \
        --threads "${TARS_CONTROLLER_THREADS:-8}" \
        --timeout "${TIMEOUT}" \
        --graceful-timeout 30 \
        --access-logfile - \
        --error-logfile - \
        "api:app"
}

case "${1:-status}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 1; start ;;
    status)  status ;;
    run)     run ;;
    *) echo "Usage: $0 {start|stop|restart|status|run}" >&2; exit 1 ;;
esac
