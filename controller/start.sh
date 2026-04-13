#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────
# TARS Controller API — start script
# Runs the Flask API on 0.0.0.0:8420
# ─────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARS_HOME="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"

export TARS_HOME

# ── Load settings from tars.conf if not already set ─────────────
CONF_FILE="${TARS_HOME}/tars.conf"
if [[ -f "${CONF_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${CONF_FILE}"
fi
export TARS_API_KEY TARS_WEBSITE_URL

if [[ -z "${TARS_API_KEY:-}" ]]; then
    echo "WARNING: TARS_API_KEY is not set. All API requests will be rejected." >&2
    echo "         Set it via:  export TARS_API_KEY='your-secret-key'" >&2
fi

# ── Virtual-env setup ───────────────────────────────────────────
if [[ ! -d "${VENV_DIR}" ]]; then
    echo "Creating virtualenv at ${VENV_DIR} ..."
    python3 -m venv "${VENV_DIR}"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

echo "Installing requirements ..."
pip install --quiet --upgrade pip
pip install --quiet -r "${SCRIPT_DIR}/requirements.txt"

# ── Run ─────────────────────────────────────────────────────────
PORT="${TARS_CONTROLLER_PORT:-8420}"

echo ""
echo "  TARS Controller API"
echo "  Listening on 0.0.0.0:${PORT}"
echo "  TARS_HOME=${TARS_HOME}"
echo ""

# Use gunicorn in production, fall back to Flask dev server.
if command -v gunicorn &>/dev/null || "${VENV_DIR}/bin/gunicorn" --version &>/dev/null 2>&1; then
    exec "${VENV_DIR}/bin/gunicorn" \
        --chdir "${SCRIPT_DIR}" \
        --bind "0.0.0.0:${PORT}" \
        --workers 2 \
        --timeout 30 \
        --access-logfile - \
        "api:app"
else
    echo "(gunicorn not found — falling back to Flask dev server)" >&2
    exec python3 "${SCRIPT_DIR}/api.py"
fi
