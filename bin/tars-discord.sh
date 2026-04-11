#!/usr/bin/env bash
# tars-discord.sh — Start/stop the Discord bot process
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../tars.conf"

DISCORD_PID_FILE="${TARS_DISCORD_PID:-${TARS_STATE}/discord_bot.pid}"

discord_is_running() {
    if [ -f "$DISCORD_PID_FILE" ]; then
        local pid
        pid=$(cat "$DISCORD_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return 0
        else
            rm -f "$DISCORD_PID_FILE"
        fi
    fi
    return 1
}

cmd_start() {
    if pid=$(discord_is_running); then
        echo "Discord bot is already running (PID: ${pid})"
        exit 1
    fi

    echo "Starting TARS Discord bot..."
    nohup "${TARS_PYTHON}" -m lib.discord_bot >> "${TARS_LOGS}/discord_bot.log" 2>&1 &
    local bot_pid=$!
    echo "$bot_pid" > "$DISCORD_PID_FILE"
    echo "Discord bot started (PID: ${bot_pid})"
    echo "Logs: tail -f ${TARS_LOGS}/discord_bot.log"
}

cmd_stop() {
    if pid=$(discord_is_running); then
        echo "Stopping Discord bot (PID: ${pid})..."
        kill "$pid"
        for i in $(seq 1 15); do
            if ! kill -0 "$pid" 2>/dev/null; then
                rm -f "$DISCORD_PID_FILE"
                echo "Discord bot stopped"
                return
            fi
            sleep 1
        done
        echo "Force killing Discord bot..."
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$DISCORD_PID_FILE"
        echo "Discord bot killed"
    else
        echo "Discord bot is not running"
    fi
}

cmd_status() {
    if pid=$(discord_is_running); then
        echo "Discord bot is running (PID: ${pid})"
    else
        echo "Discord bot is not running"
    fi
}

# --- Main ---
ACTION="${1:-}"

case "$ACTION" in
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    *)
        echo "Usage: tars-discord.sh start|stop|status"
        exit 1
        ;;
esac
