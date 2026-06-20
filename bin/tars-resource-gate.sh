#!/usr/bin/env bash
# tars-resource-gate.sh — Return 0 if the machine has headroom to start a task,
# non-zero (with a reason on stdout) if the daemon should wait.
#
# TARS already runs ONE task at a time (the daemon loop is synchronous and
# single-instance via flock), so this guards against starting heavy local-model
# work when the box is already saturated — e.g. you're using the GX10 for
# something else, or a 70B model is still loading. Thresholds come from tars.conf.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../tars.conf"

# --- Load average gate -------------------------------------------------------
# Default ceiling = number of CPU cores (1-minute load above that = oversubscribed).
CORES="$(nproc 2>/dev/null || echo 4)"
MAX_LOAD="${TARS_MAX_LOADAVG:-$CORES}"
LOAD1="$(awk '{print $1}' /proc/loadavg 2>/dev/null || echo 0)"
if awk -v l="$LOAD1" -v m="$MAX_LOAD" 'BEGIN{exit !(l+0 > m+0)}'; then
    echo "load average ${LOAD1} exceeds ${MAX_LOAD}"
    exit 1
fi

# --- GPU memory gate (optional) ----------------------------------------------
# Only enforced if nvidia-smi exists AND TARS_MIN_FREE_VRAM_MB is set (>0).
MIN_FREE_VRAM="${TARS_MIN_FREE_VRAM_MB:-0}"
if [ "${MIN_FREE_VRAM}" -gt 0 ] && command -v nvidia-smi >/dev/null 2>&1; then
    FREE_VRAM="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null \
        | sort -n | head -1 || echo 999999)"
    if [ -n "$FREE_VRAM" ] && [ "$FREE_VRAM" -lt "$MIN_FREE_VRAM" ]; then
        echo "free VRAM ${FREE_VRAM}MB below ${MIN_FREE_VRAM}MB"
        exit 1
    fi
fi

exit 0
