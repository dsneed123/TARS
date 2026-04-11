#!/usr/bin/env bash
# elephant.sh — Start/stop the Elephant backend
set -euo pipefail

TARS_HOME="${TARS_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ELEPHANT_DIR="${TARS_HOME}/repos/elephant/backend"
PID_FILE="${TARS_HOME}/state/elephant.pid"
LOG_FILE="${TARS_HOME}/logs/elephant.log"

cd "$ELEPHANT_DIR"

case "${1:-}" in
  start)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Elephant is already running (PID $(cat "$PID_FILE"))"
      exit 1
    fi
    echo "Starting Elephant backend on port 8100..."
    nohup bash -c "cd '${ELEPHANT_DIR}' && exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8100 --timeout-keep-alive 30" >> "$LOG_FILE" 2>&1 &
    sleep 2
    # Track the actual uvicorn PID (child of bash)
    UVICORN_PID=$(pgrep -f "uvicorn app.main" | tail -1)
    if [ -n "$UVICORN_PID" ]; then
      echo "$UVICORN_PID" > "$PID_FILE"
      echo "Elephant started (PID $UVICORN_PID)"
    else
      echo "Elephant failed to start. Check $LOG_FILE"
      exit 1
    fi
    ;;
  stop)
    if [ -f "$PID_FILE" ]; then
      PID=$(cat "$PID_FILE")
      if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "Elephant stopped (PID $PID)"
      else
        echo "Elephant not running (stale PID)"
      fi
      rm -f "$PID_FILE"
    else
      echo "Elephant is not running"
    fi
    ;;
  status)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Elephant is running (PID $(cat "$PID_FILE"))"
    else
      echo "Elephant is not running"
    fi
    ;;
  *)
    echo "Usage: elephant.sh start|stop|status"
    exit 1
    ;;
esac
