#!/usr/bin/env bash
# tars.sh — TARS entry point: start/stop/status/run-once/setup
#
# "I have a cue light I can use to show you when I'm joking, if you want."
#   — TARS, Interstellar
#
set -euo pipefail

TARS_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export TARS_HOME
source "${TARS_HOME}/tars.conf"

# Ensure runtime dirs exist
mkdir -p "$TARS_STATE" "$TARS_LOGS" "$TARS_REPOS" "${TARS_STATE}/locks"

usage() {
    cat <<EOF
TARS — Task Automation & Repository Steward

Usage: ./tars.sh <command>

Commands:
  start       Start the daemon (background)
  stop        Stop the running daemon
  restart     Restart the daemon
  status      Show daemon status
  run-once    Run one task cycle and exit
  setup       Check dependencies & configuration
  health      Run health check
  logs        Tail daemon logs
  queue       Show pending tasks
  new-project Create a new project (name type [description] [--private] [--org <org>])
  discord     Manage Discord bot (start|stop|status)
  dashboard   Manage web dashboard (start|stop|status)

EOF
}

is_running() {
    if [ -f "$TARS_PID_FILE" ]; then
        local pid
        pid=$(cat "$TARS_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return 0
        else
            rm -f "$TARS_PID_FILE"
        fi
    fi
    return 1
}

cmd_start() {
    if pid=$(is_running); then
        echo "TARS is already running (PID: ${pid})"
        exit 1
    fi

    # Also catch orphan daemons not tracked by the PID file
    local orphans
    orphans=$(pgrep -f "bash.*tars-daemon\.sh" 2>/dev/null || true)
    if [[ -n "$orphans" ]]; then
        echo "Found orphan tars-daemon(s): $orphans"
        echo "Run './tars.sh stop' first to clean them up."
        exit 1
    fi

    echo "Starting TARS daemon..."
    nohup "${TARS_BIN}/tars-daemon.sh" > /dev/null 2>&1 &
    local daemon_pid=$!
    echo "$daemon_pid" > "$TARS_PID_FILE"
    echo "TARS started (PID: ${daemon_pid})"
    echo "Logs: tail -f ${TARS_LOGS}/daemon.log"

    # Start Discord bot if configured
    if [ -f "${TARS_CONFIG}/discord.yaml" ] && "${TARS_PYTHON}" -c "
import yaml
cfg = yaml.safe_load(open('${TARS_CONFIG}/discord.yaml'))
exit(0 if cfg.get('bot_token') else 1)
" 2>/dev/null; then
        "${TARS_BIN}/tars-discord.sh" start 2>/dev/null || true
    fi

    # Start dashboard
    "${TARS_BIN}/tars-dashboard.sh" start 2>/dev/null || true
}

cmd_stop() {
    if pid=$(is_running); then
        echo "Stopping TARS (PID: ${pid})..."
        kill "$pid"
        # Wait for clean shutdown (max 30s)
        for i in $(seq 1 30); do
            if ! kill -0 "$pid" 2>/dev/null; then
                rm -f "$TARS_PID_FILE"
                echo "TARS stopped"
                return
            fi
            sleep 1
        done
        echo "Force killing TARS..."
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$TARS_PID_FILE"
        echo "TARS killed"
    else
        echo "TARS is not running"
    fi

    # Kill any orphan daemons that weren't in the PID file
    local orphans
    orphans=$(pgrep -f "bash.*tars-daemon\.sh" 2>/dev/null || true)
    if [[ -n "$orphans" ]]; then
        echo "Killing orphan daemons: $orphans"
        echo "$orphans" | xargs -r kill -9 2>/dev/null || true
    fi

    # Kill any running workers (their trap will reap Claude subprocesses)
    pkill -TERM -f "tars-worker.sh" 2>/dev/null || true
    sleep 1
    pkill -9 -f "tars-worker.sh" 2>/dev/null || true

    # Also stop health watchdog
    pkill -f "tars-health.sh" 2>/dev/null || true

    # Also stop Discord bot
    "${TARS_BIN}/tars-discord.sh" stop 2>/dev/null || true

    # Also stop dashboard
    "${TARS_BIN}/tars-dashboard.sh" stop 2>/dev/null || true
}

cmd_restart() {
    cmd_stop
    sleep 2
    cmd_start
}

cmd_status() {
    if pid=$(is_running); then
        echo "TARS is running (PID: ${pid})"
        echo ""

        # Show uptime
        if [ -f "$TARS_PID_FILE" ]; then
            local started
            started=$(stat -c %Y "$TARS_PID_FILE" 2>/dev/null || stat -f %m "$TARS_PID_FILE" 2>/dev/null || echo "0")
            local now
            now=$(date +%s)
            local uptime_secs=$((now - started))
            local hours=$((uptime_secs / 3600))
            local mins=$(( (uptime_secs % 3600) / 60 ))
            echo "Uptime: ${hours}h ${mins}m"
        fi

        # Show token usage
        "${TARS_PYTHON}" -c "
from lib.token_tracker import TokenTracker
t = TokenTracker()
usage = t.get_today_usage()
budget = t.budget
pct = (usage['total_tokens'] / budget['daily_limit'] * 100) if budget['daily_limit'] > 0 else 0
print(f\"Tokens today: {usage['total_tokens']:,} / {budget['daily_limit']:,} ({pct:.1f}%)\")
print(f\"Cost today: \${usage['total_cost']:.4f}\")
" 2>/dev/null || true

        # Show task stats
        "${TARS_PYTHON}" -c "
from lib.metrics import MetricsTracker
m = MetricsTracker()
stats = m.get_today_stats()
print(f\"Tasks today: {stats.get('completed', 0)} completed, {stats.get('failed', 0)} failed\")
" 2>/dev/null || true
    else
        echo "TARS is not running"
    fi
}

cmd_run_once() {
    echo "Running one task cycle..."
    NEXT_TASK=$("${TARS_BIN}/tars-scheduler.sh" 2>&1) || {
        echo "No tasks available"
        exit 0
    }

    if [ -z "$NEXT_TASK" ] || [ "$NEXT_TASK" = "null" ]; then
        echo "No tasks in queue"
        exit 0
    fi

    PROJECT=$(echo "$NEXT_TASK" | jq -r '.project')
    TASK_JSON=$(echo "$NEXT_TASK" | jq -c '.task')
    TASK_TITLE=$(echo "$TASK_JSON" | jq -r '.title')

    echo "Executing: ${TASK_TITLE} (project: ${PROJECT})"
    "${TARS_BIN}/tars-worker.sh" "$PROJECT" "$TASK_JSON"
}

cmd_setup() {
    "${TARS_BIN}/tars-setup.sh"
}

cmd_health() {
    "${TARS_BIN}/tars-health.sh" --once
}

cmd_logs() {
    tail -f "${TARS_LOGS}/daemon.log"
}

cmd_new_project() {
    local name="${1:?Usage: ./tars.sh new-project <name> <type> [description] [--private] [--org <org>]}"
    local project_type="${2:?Usage: ./tars.sh new-project <name> <type> [description] [--private] [--org <org>]}"
    shift 2

    local description=""
    local visibility="public"
    local org=""

    while [ $# -gt 0 ]; do
        case "$1" in
            --private) visibility="private"; shift ;;
            --org) org="${2:?--org requires a value}"; shift 2 ;;
            *) description="$1"; shift ;;
        esac
    done

    echo "Creating project: ${name} (${project_type})..."
    "${TARS_PYTHON}" -c "
import json
from lib.project_creator import create_project
result = create_project(
    name='${name}',
    project_type='${project_type}',
    description='''${description}''',
    visibility='${visibility}',
    org='${org}',
)
print(json.dumps(result, indent=2))
"
    echo ""
    echo "Project created! Config at: config/projects/${name}.yaml"
}

cmd_discord() {
    local action="${1:?Usage: ./tars.sh discord start|stop|status}"
    "${TARS_BIN}/tars-discord.sh" "$action"
}

cmd_dashboard() {
    local action="${1:?Usage: ./tars.sh dashboard start|stop|status}"
    "${TARS_BIN}/tars-dashboard.sh" "$action"
}

cmd_queue() {
    "${TARS_PYTHON}" -c "
import json
from lib.task_manager import TaskManager
tm = TaskManager()
tasks = tm.get_all_tasks()
if not tasks:
    print('No pending tasks')
else:
    for t in tasks:
        src = t.get('source', '?')
        pri = t.get('priority', 0)
        proj = t.get('project', '?')
        title = t.get('title', 'Untitled')
        print(f'  [{pri:3d}] [{src:8s}] [{proj}] {title}')
"
}

# --- Main ---
COMMAND="${1:-}"

case "$COMMAND" in
    start)       cmd_start ;;
    stop)        cmd_stop ;;
    restart)     cmd_restart ;;
    status)      cmd_status ;;
    run-once)    cmd_run_once ;;
    setup)       cmd_setup ;;
    health)      cmd_health ;;
    logs)        cmd_logs ;;
    queue)       cmd_queue ;;
    new-project) shift; cmd_new_project "$@" ;;
    discord)     shift; cmd_discord "$@" ;;
    dashboard)   shift; cmd_dashboard "$@" ;;
    ""|help|-h|--help)
        usage
        ;;
    *)
        echo "Unknown command: $COMMAND"
        usage
        exit 1
        ;;
esac
