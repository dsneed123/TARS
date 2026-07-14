#!/usr/bin/env bash
# tars-scheduler.sh — Task scheduling & queue management
# Outputs a JSON object with {project, task} for the next task to execute
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../tars.conf"

# Change to TARS_HOME so Python imports work
cd "$TARS_HOME"

# Get next task from task manager (respecting active projects).
# Python's stderr goes to a temp file: on failure we replay it to our own
# stderr so the daemon can log WHY ("Token budget exceeded", "All projects
# locked", ...) instead of the empty "No tasks available: " it used to get;
# on success we discard it so warnings can't corrupt the JSON on stdout.
ERR_TMP=$(mktemp)
trap 'rm -f "$ERR_TMP"' EXIT

NEXT=$("${TARS_PYTHON}" -c "
import json, sys
from pathlib import Path
from lib.task_manager import TaskManager
from lib.token_tracker import TokenTracker
from lib.error_analyzer import ErrorAnalyzer

tm = TaskManager()
tt = TokenTracker()
ea = ErrorAnalyzer()

# Check token budget first
if not tt.can_spend():
    wait = tt.get_wait_time()
    print(f'Token budget exceeded, wait {wait}s', file=sys.stderr)
    sys.exit(1)

# Load active projects filter
active_file = Path('state/active_projects.json')
active_projects = None
if active_file.exists():
    try:
        active_projects = json.loads(active_file.read_text()).get('active', [])
    except Exception:
        pass

# Get all tasks and filter to active projects
all_tasks = tm.get_all_tasks()
if active_projects is not None and len(active_projects) > 0:
    all_tasks = [t for t in all_tasks if t.get('project', '') in active_projects]

if not all_tasks:
    print('No tasks available', file=sys.stderr)
    sys.exit(1)

# Find first task whose project isn't locked
task = None
for t in all_tasks:
    proj = t.get('project', '')
    if proj and not ea.is_locked(proj):
        task = t
        break

if task is None:
    print('All projects locked', file=sys.stderr)
    sys.exit(1)

project = task.get('project', '')
if not project:
    print('Task has no project', file=sys.stderr)
    sys.exit(1)

# Output task for worker
output = {'project': project, 'task': task}
print(json.dumps(output))
" 2>"$ERR_TMP") || {
    cat "$ERR_TMP" >&2
    exit 1
}

if [ -z "$NEXT" ]; then
    echo "Scheduler produced no output" >&2
    exit 1
fi

echo "$NEXT"
