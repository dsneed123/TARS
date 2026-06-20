#!/usr/bin/env bash
# tars-ollama-check.sh — Verify the local Ollama provider is ready for TARS.
# Checks: daemon reachable, configured models pulled, and an end-to-end agent
# smoke test (the model actually edits a file via the TARS runner).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../tars.conf"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "    ${GREEN}✓${NC} $*"; }
warn() { echo -e "    ${YELLOW}⚠${NC} $*"; }
fail() { echo -e "    ${RED}✗${NC} $*"; }

echo "  Ollama:"
RC=0

# 1. Daemon reachable
if curl -fsS "${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; then
    ok "daemon up at ${OLLAMA_HOST}"
else
    fail "cannot reach Ollama at ${OLLAMA_HOST} (run: ollama serve)"
    exit 1
fi

# 2. Models pulled — uses the runner's has_model (tag-aware) check
check_model() {
    local label="$1" model="$2"
    if PYTHONPATH="${TARS_LIB}" "${TARS_PYTHON}" -c \
        "import sys; from ollama_client import OllamaClient; sys.exit(0 if OllamaClient().has_model('$model') else 1)" 2>/dev/null; then
        ok "${label}: ${model}"
    else
        warn "${label}: ${model} NOT pulled yet (run: ollama pull ${model})"
        RC=1
    fi
}
check_model "code  " "${OLLAMA_CODE_MODEL}"
check_model "fix   " "${OLLAMA_FIX_MODEL}"
check_model "review" "${OLLAMA_REVIEW_MODEL}"
check_model "chat  " "${OLLAMA_CHAT_MODEL}"

# 3. End-to-end smoke test (only if the code model is present and --smoke given)
if [ "${1:-}" = "--smoke" ]; then
    echo "  Smoke test (agent edits a real file):"
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    if PYTHONPATH="${TARS_LIB}" TARS_LLM_PROVIDER=ollama "${TARS_PYTHON}" - "$TMP" <<'PY'
import sys
from ollama_runner import OllamaRunner
cwd = sys.argv[1]
r = OllamaRunner()
res = r.run(
    "Create a file called hello.py containing exactly: print('hello from TARS')",
    cwd=cwd, role="code", max_turns=6,
)
import os
path = os.path.join(cwd, "hello.py")
if os.path.exists(path):
    print("    OK: file created ->", open(path).read().strip())
    print("    tokens:", res["tokens_in"], "in /", res["tokens_out"], "out")
    sys.exit(0)
print("    FAIL: model did not create the file. Summary:", res["result"][:200])
sys.exit(1)
PY
    then ok "agent smoke test passed"; else fail "agent smoke test failed"; RC=1; fi
fi

exit $RC
