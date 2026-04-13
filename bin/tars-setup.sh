#!/usr/bin/env bash
# tars-setup.sh — First-time setup & dependency check
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../tars.conf"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $*"; }
fail() { echo -e "  ${RED}✗${NC} $*"; }

ERRORS=0

echo "TARS Setup Check"
echo "================"
echo ""

# --- System dependencies ---
echo "System Dependencies:"

# Python 3.10+
if command -v "$TARS_PYTHON" &>/dev/null; then
    PY_VER=$("$TARS_PYTHON" --version 2>&1 | awk '{print $2}')
    PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 9 ]; then
        ok "Python ${PY_VER}"
    else
        fail "Python ${PY_VER} (need 3.10+)"
        ((ERRORS++))
    fi
else
    fail "python3 not found"
    ((ERRORS++))
fi

# Claude CLI
if command -v "$CLAUDE_CMD" &>/dev/null; then
    CLAUDE_VER=$("$CLAUDE_CMD" --version 2>&1 | head -1 || echo "unknown")
    ok "Claude CLI (${CLAUDE_VER})"

    # Check authentication
    if "$CLAUDE_CMD" -p "say ok" --output-format json --max-turns 1 &>/dev/null; then
        ok "Claude CLI authenticated"
    else
        warn "Claude CLI may not be authenticated (run: claude auth)"
    fi
else
    fail "claude CLI not found (install: npm install -g @anthropic-ai/claude-code)"
    ((ERRORS++))
fi

# gh CLI
if command -v "$GH_CMD" &>/dev/null; then
    GH_VER=$("$GH_CMD" --version 2>&1 | head -1)
    ok "gh CLI (${GH_VER})"

    if "$GH_CMD" auth status &>/dev/null; then
        ok "gh CLI authenticated"
    else
        warn "gh CLI not authenticated (run: gh auth login)"
    fi
else
    fail "gh CLI not found (install: https://cli.github.com)"
    ((ERRORS++))
fi

# git
if command -v "$GIT_CMD" &>/dev/null; then
    GIT_VER=$("$GIT_CMD" --version)
    ok "${GIT_VER}"
else
    fail "git not found"
    ((ERRORS++))
fi

# jq
if command -v jq &>/dev/null; then
    ok "jq $(jq --version 2>&1)"
else
    fail "jq not found (install: apt install jq / brew install jq)"
    ((ERRORS++))
fi

# xcodebuild (optional, macOS only)
if [[ "$(uname)" == "Darwin" ]]; then
    if command -v xcodebuild &>/dev/null; then
        XCODE_VER=$(xcodebuild -version 2>&1 | head -1)
        ok "Xcode (${XCODE_VER})"
    else
        warn "xcodebuild not found (optional, needed for Xcode projects)"
    fi
fi

echo ""

# --- Python packages ---
echo "Python Packages:"

for pkg in pyyaml requests python-dateutil discord.py; do
    if "$TARS_PYTHON" -c "import importlib; importlib.import_module('${pkg//-/_}')" &>/dev/null; then
        ok "$pkg"
    else
        # Try alternate import names
        case "$pkg" in
            pyyaml)
                if "$TARS_PYTHON" -c "import yaml" &>/dev/null; then ok "$pkg"; else fail "$pkg not installed"; ((ERRORS++)); fi
                ;;
            python-dateutil)
                if "$TARS_PYTHON" -c "import dateutil" &>/dev/null; then ok "$pkg"; else fail "$pkg not installed"; ((ERRORS++)); fi
                ;;
            discord.py)
                if "$TARS_PYTHON" -c "import discord" &>/dev/null; then ok "$pkg"; else warn "$pkg not installed (optional, needed for Discord bot commands)"; fi
                ;;
            *)
                fail "$pkg not installed"
                ((ERRORS++))
                ;;
        esac
    fi
done

echo ""

# --- Directory structure ---
echo "Directory Structure:"

for dir in "$TARS_STATE" "$TARS_LOGS" "$TARS_REPOS" "${TARS_STATE}/locks" "$TARS_CONFIG/projects" "$TARS_PROMPTS"; do
    if [ -d "$dir" ]; then
        ok "$dir"
    else
        mkdir -p "$dir"
        ok "$dir (created)"
    fi
done

echo ""

# --- Config files ---
echo "Configuration:"

if ls "$TARS_CONFIG/projects/"*.yaml &>/dev/null; then
    COUNT=$(ls "$TARS_CONFIG/projects/"*.yaml | wc -l)
    ok "${COUNT} project config(s) found"
else
    warn "No project configs in config/projects/ — create one to get started"
fi

if [ -f "$TARS_CONFIG/discord.yaml" ]; then
    ok "Discord config found"
    # Check for bot token
    if "$TARS_PYTHON" -c "
import yaml
cfg = yaml.safe_load(open('${TARS_CONFIG}/discord.yaml'))
exit(0 if cfg.get('bot_token') else 1)
" 2>/dev/null; then
        ok "Discord bot token configured"
    else
        warn "No bot_token in discord.yaml — Discord bot commands will be disabled"
    fi
else
    warn "No discord.yaml — Discord logging and bot will be disabled"
fi

if [ -f "$TARS_CONFIG/token_budget.yaml" ]; then
    ok "Token budget config found"
else
    warn "No token_budget.yaml — using defaults"
fi

echo ""

# --- Summary ---
if [ "$ERRORS" -gt 0 ]; then
    echo -e "${RED}Setup incomplete: ${ERRORS} error(s) found.${NC}"
    echo "Fix the errors above and run this script again."
    exit 1
else
    echo -e "${GREEN}TARS is ready!${NC}"
    echo ""
    echo "Next steps:"
    echo "  1. Create a project config: config/projects/my-project.yaml"
    echo "  2. (Optional) Set up Discord: config/discord.yaml"
    echo "  3. Test with: ./tars.sh run-once"
    echo "  4. Start daemon: ./tars.sh start"
fi
