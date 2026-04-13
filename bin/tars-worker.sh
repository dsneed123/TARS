#!/usr/bin/env bash
# tars-worker.sh — Execute a single task end-to-end
#
# All data passed to Python via env vars or temp files — never embedded
# in string literals (which breaks on quotes, backslashes, JSON, etc.)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../tars.conf"

# Ensure Python imports work
cd "$TARS_HOME"
export PYTHONPATH="${TARS_HOME}:${PYTHONPATH:-}"

# Usage: tars-worker.sh <project_name> <task_json>
PROJECT="${1:?Usage: tars-worker.sh <project> <task_json>}"
TASK_JSON="${2:?Usage: tars-worker.sh <project> <task_json>}"

TASK_ID=$(echo "$TASK_JSON" | jq -r '.id // "unknown"')
TASK_TITLE=$(echo "$TASK_JSON" | jq -r '.title // "Untitled"')
TASK_DESC=$(echo "$TASK_JSON" | jq -r '.description // ""')
TASK_SOURCE=$(echo "$TASK_JSON" | jq -r '.source // "manual"')

LOG_FILE="${TARS_LOGS}/task_${TASK_ID}_$(date +%Y%m%d_%H%M%S).log"

# Temp file for passing large data to Python (cleaned up on exit)
TMPDATA=$(mktemp "${TARS_STATE}/worker_XXXXXX")
trap 'rm -f "$TMPDATA"' EXIT

DAEMON_LOG="${TARS_LOGS}/daemon.log"

log() {
    local level="$1"; shift
    local msg="[$(date +"${LOG_DATE_FMT}")] [${level}] [worker:${TASK_ID}] $*"
    echo "$msg" >> "$LOG_FILE"
    echo "$msg" >> "$DAEMON_LOG"
}

log "INFO" "Starting task: ${TASK_TITLE} (source: ${TASK_SOURCE})"

# --- Step 1: Check token budget ---
BUDGET_OK=$("${TARS_PYTHON}" -c "
from lib.token_tracker import TokenTracker
t = TokenTracker()
print('yes' if t.can_spend() else 'no')
" 2>/dev/null || echo "yes")

if [ "$BUDGET_OK" = "no" ]; then
    log "WARN" "Token budget exceeded, skipping task"
    TARS_MSG="Token budget exceeded, task skipped: ${TASK_TITLE}" \
    "${TARS_PYTHON}" -c "
import os
from lib.discord_logger import DiscordLogger
DiscordLogger().log_warning(os.environ['TARS_MSG'])
" 2>/dev/null || true
    exit 0
fi

# --- Step 2: Load project config ---
export TARS_PROJECT="$PROJECT"
PROJECT_CONFIG=$("${TARS_PYTHON}" -c "
import json, os
from lib.config_loader import load_project
print(json.dumps(load_project(os.environ['TARS_PROJECT'])))
" 2>/dev/null) || {
    log "ERROR" "Failed to load project config for ${PROJECT}"
    exit 1
}

REPO=$(echo "$PROJECT_CONFIG" | jq -r '.repo')
GIT_STRATEGY=$(echo "$PROJECT_CONFIG" | jq -r '.git.strategy')
BASE_BRANCH=$(echo "$PROJECT_CONFIG" | jq -r '.git.base_branch')
CLAUDE_MODEL=$(echo "$PROJECT_CONFIG" | jq -r '.claude.model')
CLAUDE_MAX_TURNS=$(echo "$PROJECT_CONFIG" | jq -r '.claude.max_turns')
BUILD_CMD=$(echo "$PROJECT_CONFIG" | jq -r '.build.command // empty')
TEST_CMD=$(echo "$PROJECT_CONFIG" | jq -r '.test.command // empty')

log "INFO" "Project: ${REPO}, strategy: ${GIT_STRATEGY}, model: ${CLAUDE_MODEL}"

# --- Step 3: Prepare workspace ---
log "INFO" "Preparing workspace..."
TARS_REPO="$REPO" TARS_BASE_BRANCH="$BASE_BRANCH" TARS_GIT_STRATEGY="$GIT_STRATEGY" \
TARS_TASK_ID="$TASK_ID" \
"${TARS_PYTHON}" -c "
import os
from lib.git_manager import GitManager
gm = GitManager(os.environ['TARS_REPO'], base_branch=os.environ['TARS_BASE_BRANCH'], strategy=os.environ['TARS_GIT_STRATEGY'])
gm.ensure_cloned()
gm.create_branch(os.environ['TARS_TASK_ID'])
"

WORK_DIR="${TARS_REPOS}/$(echo "${REPO}" | awk -F/ '{print $NF}')"

# --- Step 4: Implementation phase (Claude writes code) ---
log "INFO" "Running Claude implementation..."

# Write task data to temp file for Python to read
echo "$TASK_JSON" > "$TMPDATA"

IMPL_RESULT=$(TARS_TASK_FILE="$TMPDATA" TARS_REPO="$REPO" TARS_WORK_DIR="$WORK_DIR" \
TARS_CLAUDE_MODEL="$CLAUDE_MODEL" TARS_CLAUDE_MAX_TURNS="$CLAUDE_MAX_TURNS" \
"${TARS_PYTHON}" -c "
import json, os
from lib.claude_runner import ClaudeRunner

task = json.load(open(os.environ['TARS_TASK_FILE']))
runner = ClaudeRunner(model=os.environ['TARS_CLAUDE_MODEL'], max_turns=int(os.environ['TARS_CLAUDE_MAX_TURNS']))
result = runner.run_with_prompt_file(
    'implement_task.md',
    variables={
        'TASK_TITLE': task.get('title', ''),
        'TASK_DESCRIPTION': task.get('description', ''),
        'REPO_NAME': os.environ['TARS_REPO'],
    },
    cwd=os.environ['TARS_WORK_DIR'],
    timeout=900,
)
print(json.dumps(result))
" 2>&1) || {
    log "ERROR" "Claude implementation failed"
    log "ERROR" "$IMPL_RESULT"
    TARS_MSG="Implementation failed for: ${TASK_TITLE}" \
    "${TARS_PYTHON}" -c "
import os
from lib.discord_logger import DiscordLogger
DiscordLogger().log_error(os.environ['TARS_MSG'], '')
" 2>/dev/null || true
    exit 1
}

# Track tokens — extract with jq (safe, no string embedding)
TOKENS_IN=$(echo "$IMPL_RESULT" | jq -r '.tokens_in // 0' 2>/dev/null || echo 0)
TOKENS_OUT=$(echo "$IMPL_RESULT" | jq -r '.tokens_out // 0' 2>/dev/null || echo 0)
COST_USD=$(echo "$IMPL_RESULT" | jq -r '.cost_usd // 0' 2>/dev/null || echo 0)

"${TARS_PYTHON}" -c "
import sys
from lib.token_tracker import TokenTracker
t = TokenTracker()
t.record(int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]))
" "$TOKENS_IN" "$TOKENS_OUT" "$COST_USD" 2>/dev/null || true

log "INFO" "Implementation complete (tokens: ${TOKENS_IN} in, ${TOKENS_OUT} out, \$${COST_USD})"

# Check for session limit warning from Claude
SESSION_WARN=$(echo "$IMPL_RESULT" | jq -r '.session_warning // false' 2>/dev/null || echo false)
if [ "$SESSION_WARN" = "true" ]; then
    log "WARN" "Claude session near limit — writing cooldown"
    "${TARS_PYTHON}" -c "
import json, time
json.dump({'paused': True, 'reason': 'session_limit', 'wait_s': 1800, 'until': time.time() + 1800}, open('${TARS_STATE}/daemon_status.json', 'w'))
" 2>/dev/null || true
fi

# --- Step 5: Verification (build + test) ---
VERIFY_PASS=true

if [ -n "$BUILD_CMD" ]; then
    log "INFO" "Running build: ${BUILD_CMD}"
    if ! (cd "$WORK_DIR" && eval "$BUILD_CMD") >> "$LOG_FILE" 2>&1; then
        log "WARN" "Build failed, entering auto-patch loop"
        VERIFY_PASS=false
    fi
fi

if [ "$VERIFY_PASS" = true ] && [ -n "$TEST_CMD" ]; then
    log "INFO" "Running tests: ${TEST_CMD}"
    if ! (cd "$WORK_DIR" && eval "$TEST_CMD") >> "$LOG_FILE" 2>&1; then
        log "WARN" "Tests failed, entering auto-patch loop"
        VERIFY_PASS=false
    fi
fi

# --- Step 5b: Auto-patch loop on failure ---
if [ "$VERIFY_PASS" = false ]; then
    for attempt in 1 2 3; do
        log "INFO" "Auto-patch attempt ${attempt}/3"

        # Write error context to temp file
        tail -50 "$LOG_FILE" > "$TMPDATA"

        TARS_ERR_FILE="$TMPDATA" TARS_ATTEMPT="$attempt" \
        TARS_CLAUDE_MODEL="$CLAUDE_MODEL" TARS_CLAUDE_MAX_TURNS="$CLAUDE_MAX_TURNS" \
        TARS_WORK_DIR="$WORK_DIR" \
        "${TARS_PYTHON}" -c "
import os
from lib.claude_runner import ClaudeRunner
runner = ClaudeRunner(model=os.environ['TARS_CLAUDE_MODEL'], max_turns=int(os.environ['TARS_CLAUDE_MAX_TURNS']))
err_output = open(os.environ['TARS_ERR_FILE']).read()
result = runner.run_with_prompt_file(
    'fix_error.md',
    variables={
        'ERROR_OUTPUT': err_output,
        'ERROR_CONTEXT': 'building/testing after implementation',
        'PREVIOUS_ATTEMPT': 'Attempt ' + os.environ['TARS_ATTEMPT'] + ' of 3',
    },
    cwd=os.environ['TARS_WORK_DIR'],
    timeout=600,
)
" 2>&1 || {
            log "ERROR" "Auto-patch attempt ${attempt} failed"
            continue
        }

        # Re-verify
        VERIFY_PASS=true
        if [ -n "$BUILD_CMD" ]; then
            if ! (cd "$WORK_DIR" && eval "$BUILD_CMD") >> "$LOG_FILE" 2>&1; then
                VERIFY_PASS=false
            fi
        fi
        if [ "$VERIFY_PASS" = true ] && [ -n "$TEST_CMD" ]; then
            if ! (cd "$WORK_DIR" && eval "$TEST_CMD") >> "$LOG_FILE" 2>&1; then
                VERIFY_PASS=false
            fi
        fi

        if [ "$VERIFY_PASS" = true ]; then
            log "INFO" "Auto-patch succeeded on attempt ${attempt}"
            break
        fi
    done

    if [ "$VERIFY_PASS" = false ]; then
        log "ERROR" "All auto-patch attempts failed, escalating"
        TARS_PROJECT="$PROJECT" TARS_TASK_ID="$TASK_ID" \
        TARS_MSG="Task failed after 3 auto-patch attempts: ${TASK_TITLE}" \
        TARS_LOG_FILE="$LOG_FILE" \
        "${TARS_PYTHON}" -c "
import os
from lib.discord_logger import DiscordLogger
from lib.error_analyzer import ErrorAnalyzer
ErrorAnalyzer().record_failure(os.environ['TARS_PROJECT'], os.environ['TARS_TASK_ID'])
DiscordLogger().log_error(os.environ['TARS_MSG'], 'Check logs: ' + os.environ['TARS_LOG_FILE'])
" 2>/dev/null || true
        exit 1
    fi
fi

# --- Step 6: Check if there are changes to commit ---
export PYTHONPATH="${TARS_HOME}:${PYTHONPATH:-}"
cd "$WORK_DIR"
if ! "${GIT_CMD}" diff --quiet HEAD 2>/dev/null || [ -n "$("${GIT_CMD}" status --porcelain 2>/dev/null)" ]; then
    # --- Step 7: Self-review ---
    log "INFO" "Running self-review..."
    "${GIT_CMD}" diff HEAD > "$TMPDATA" 2>/dev/null || true
    "${GIT_CMD}" diff --cached >> "$TMPDATA" 2>/dev/null || true

    if [ -s "$TMPDATA" ]; then
        REVIEW=$(TARS_DIFF_FILE="$TMPDATA" TARS_WORK_DIR="$WORK_DIR" \
        "${TARS_PYTHON}" -c "
import os
from lib.claude_runner import ClaudeRunner
runner = ClaudeRunner()
diff = open(os.environ['TARS_DIFF_FILE']).read()
result = runner.self_review(diff, cwd=os.environ['TARS_WORK_DIR'])
print(result.get('result', '{}'))
" 2>/dev/null || echo '{"approved": true}')

        APPROVED=$(echo "$REVIEW" | "${TARS_PYTHON}" -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print('true' if data.get('approved', True) else 'false')
except:
    print('true')
" 2>/dev/null || echo "true")

        if [ "$APPROVED" = "false" ]; then
            log "WARN" "Self-review rejected changes, escalating"
            TARS_MSG="Self-review rejected changes for: ${TASK_TITLE}" \
            "${TARS_PYTHON}" -c "
import os
from lib.discord_logger import DiscordLogger
DiscordLogger().log_warning(os.environ['TARS_MSG'])
" 2>/dev/null || true
            exit 1
        fi
    fi

    # --- Step 8: Commit, push, PR ---
    log "INFO" "Committing and pushing..."
    BRANCH=$("${GIT_CMD}" rev-parse --abbrev-ref HEAD)
    "${GIT_CMD}" add -A
    "${GIT_CMD}" commit -m "${TASK_TITLE}"

    # Write task metadata to temp file for push_and_pr
    jq -n --arg title "$TASK_TITLE" --arg desc "$TASK_DESC" \
        '{"title": $title, "desc": $desc}' > "$TMPDATA"

    PR_URL=$(TARS_REPO="$REPO" TARS_BASE_BRANCH="$BASE_BRANCH" TARS_GIT_STRATEGY="$GIT_STRATEGY" \
    TARS_BRANCH="$BRANCH" TARS_TASK_FILE="$TMPDATA" \
    "${TARS_PYTHON}" -c "
import json, os
meta = json.load(open(os.environ['TARS_TASK_FILE']))
from lib.git_manager import GitManager
gm = GitManager(os.environ['TARS_REPO'], base_branch=os.environ['TARS_BASE_BRANCH'], strategy=os.environ['TARS_GIT_STRATEGY'])
url = gm.push_and_pr(os.environ['TARS_BRANCH'], meta['title'], meta['desc'])
print(url or 'direct-push')
" 2>&1) || {
        log "ERROR" "Push/PR failed: ${PR_URL}"
        # Mark permanent failures so daemon skips immediately instead of retrying
        if echo "$PR_URL" | grep -qi "workflow.*scope\|permission\|denied\|forbidden\|protected branch"; then
            log "ERROR" "Permanent failure (permission issue) — marking task as unretryable"
            echo '99' > "${TARS_STATE}/task_perm_fail_${TASK_ID}"
        fi
        exit 1
    }

    log "INFO" "Completed: ${PR_URL}"

    # --- Step 9: Discord notification ---
    TARS_MSG="$TASK_TITLE" TARS_PR_URL="$PR_URL" TARS_PROJECT="$PROJECT" \
    "${TARS_PYTHON}" -c "
import os
from lib.discord_logger import DiscordLogger
DiscordLogger().log_success(os.environ['TARS_MSG'], pr_url=os.environ['TARS_PR_URL'], project=os.environ['TARS_PROJECT'])
" 2>/dev/null || true

    # Reset circuit breaker on success
    TARS_PROJECT="$PROJECT" \
    "${TARS_PYTHON}" -c "
import os
from lib.error_analyzer import ErrorAnalyzer
ErrorAnalyzer().reset_failures(os.environ['TARS_PROJECT'])
" 2>/dev/null || true
else
    log "INFO" "No changes to commit (Claude may have determined no changes needed)"
    TARS_MSG="No changes needed for: ${TASK_TITLE}" TARS_PROJECT="$PROJECT" \
    "${TARS_PYTHON}" -c "
import os
from lib.discord_logger import DiscordLogger
DiscordLogger().log_info(os.environ['TARS_MSG'], project=os.environ['TARS_PROJECT'])
" 2>/dev/null || true
fi

log "INFO" "Task complete: ${TASK_ID}"
