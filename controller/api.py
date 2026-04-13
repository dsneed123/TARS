"""
TARS Controller API

Lightweight Flask API running on the Mac Mini "brain" that receives
task commands from the Railway-hosted Django website (usetars.dev).

Reads/writes TARS state files (JSON) and queue config (YAML).
Auth via X-API-Key header, key read from TARS_API_KEY env var.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import yaml
from flask import Flask, abort, jsonify, render_template_string, request
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).resolve().parent.parent))
STATE_DIR = TARS_HOME / "state"
CONFIG_DIR = TARS_HOME / "config"
QUEUE_FILE = CONFIG_DIR / "queue.yaml"

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)

CORS(
    app,
    origins=[
        "https://tarsai.dev",
        "https://tars-survey-production.up.railway.app",
    ],
    supports_credentials=True,
)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("TARS_API_KEY", "")


def require_api_key(fn):
    """Decorator that checks for a valid X-API-Key header."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not API_KEY:
            # If no key is configured, reject everything so we never run open.
            return jsonify({"error": "API key not configured on server"}), 500
        provided = request.headers.get("X-API-Key", "")
        if not provided or provided != API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Helpers — state file I/O
# ---------------------------------------------------------------------------


def _read_json(path: Path, default=None):
    """Read a JSON file, returning *default* if missing or corrupt."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def _write_json(path: Path, data):
    """Atomically write JSON (write-tmp then rename)."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    tmp.rename(path)


def _read_queue() -> dict:
    """Read config/queue.yaml, returning {'tasks': []} on failure."""
    try:
        with open(QUEUE_FILE) as f:
            data = yaml.safe_load(f)
            if isinstance(data, dict) and "tasks" in data:
                return data
    except (FileNotFoundError, yaml.YAMLError):
        pass
    return {"tasks": []}


def _write_queue(data: dict):
    """Atomically write config/queue.yaml."""
    tmp = QUEUE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    tmp.rename(QUEUE_FILE)


def _discover_workers() -> list[dict]:
    """
    Scan state/ for worker_* files to build a worker list.

    Each worker file is expected to be a JSON object with at least
    {hostname, status, last_heartbeat, ...}.  If the file is just a
    marker (empty/non-JSON) we still report the worker with defaults.
    """
    workers = []
    for p in sorted(STATE_DIR.glob("worker_*")):
        wid = p.name  # e.g. "worker_n8Q150"
        info = _read_json(p, default={})
        if not isinstance(info, dict):
            info = {}
        info.setdefault("id", wid)
        info.setdefault("hostname", wid)
        info.setdefault("status", "unknown")
        info.setdefault("last_heartbeat", None)
        workers.append(info)
    return workers


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

VALID_TASK_TYPES = {"tars-code", "tars-marketing"}
VALID_PRIORITIES = range(1, 101)  # 1–100 inclusive


def _validate_task_payload(data: dict) -> str | None:
    """Return an error string if the payload is invalid, else None."""
    if not data:
        return "Request body must be JSON"
    for field in ("project", "task_type", "description"):
        if not data.get(field):
            return f"Missing required field: {field}"
    if data["task_type"] not in VALID_TASK_TYPES:
        return f"Invalid task_type. Must be one of: {', '.join(sorted(VALID_TASK_TYPES))}"
    priority = data.get("priority", 50)
    if not isinstance(priority, int) or priority not in VALID_PRIORITIES:
        return "priority must be an integer between 1 and 100"
    return None


# ---------------------------------------------------------------------------
# Routes — Status & Health
# ---------------------------------------------------------------------------


@app.route("/api/status", methods=["GET"])
@require_api_key
def cluster_status():
    """Return cluster status: online workers, current task, queue depth."""
    current_task = _read_json(STATE_DIR / "current_task.json")
    queue = _read_queue()
    pending = [t for t in queue.get("tasks", []) if t.get("status") == "pending"]
    workers = _discover_workers()
    online = [w for w in workers if w.get("status") not in ("offline", "unknown")]
    active_projects = _read_json(STATE_DIR / "active_projects.json", {"active": []})

    return jsonify({
        "status": "online",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "current_task": current_task if current_task else None,
        "queue_length": len(pending),
        "workers_online": len(online),
        "workers_total": len(workers),
        "active_projects": active_projects.get("active", []),
    })


@app.route("/api/health", methods=["GET"])
def health():
    """Unauthenticated health-check for uptime monitors."""
    return jsonify({"ok": True, "timestamp": datetime.now(timezone.utc).isoformat()})


# ---------------------------------------------------------------------------
# Routes — TARS Daemon Control
# ---------------------------------------------------------------------------

PID_FILE = STATE_DIR / "daemon.pid"
TARS_SH = TARS_HOME / "tars.sh"


def _is_daemon_running():
    """Check if the TARS daemon is running."""
    if not PID_FILE.exists():
        return False, None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # signal 0 = check if alive
        return True, pid
    except (ValueError, ProcessLookupError, PermissionError):
        return False, None


@app.route("/api/daemon/status", methods=["GET"])
@require_api_key
def daemon_status():
    running, pid = _is_daemon_running()
    return jsonify({"running": running, "pid": pid})


@app.route("/api/daemon/start", methods=["POST"])
@require_api_key
def daemon_start():
    running, pid = _is_daemon_running()
    if running:
        return jsonify({"message": "TARS is already running", "pid": pid})
    try:
        subprocess.Popen(
            [str(TARS_SH), "start"],
            cwd=str(TARS_HOME),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        running, pid = _is_daemon_running()
        return jsonify({"message": "TARS started", "running": running, "pid": pid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/daemon/stop", methods=["POST"])
@require_api_key
def daemon_stop():
    running, pid = _is_daemon_running()
    if not running:
        return jsonify({"message": "TARS is not running"})
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(1)
        still_running, _ = _is_daemon_running()
        if still_running:
            os.kill(pid, signal.SIGKILL)
        if PID_FILE.exists():
            PID_FILE.unlink()
        return jsonify({"message": "TARS stopped", "running": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Routes — Workers
# ---------------------------------------------------------------------------


@app.route("/api/workers", methods=["GET"])
@require_api_key
def list_workers():
    """Return all registered workers and their status."""
    workers = _discover_workers()
    return jsonify({"workers": workers, "count": len(workers)})


# ---------------------------------------------------------------------------
# Routes — Tasks
# ---------------------------------------------------------------------------


@app.route("/api/tasks", methods=["GET"])
@require_api_key
def list_tasks():
    """List all queued / active tasks from queue.yaml and current_task.json."""
    queue = _read_queue()
    tasks = queue.get("tasks", [])
    current = _read_json(STATE_DIR / "current_task.json")

    # Optionally filter by status or project
    status_filter = request.args.get("status")
    project_filter = request.args.get("project")
    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]
    if project_filter:
        tasks = [t for t in tasks if t.get("project") == project_filter]

    return jsonify({
        "tasks": tasks,
        "count": len(tasks),
        "current_task": current if current else None,
    })


@app.route("/api/tasks", methods=["POST"])
@require_api_key
def create_task():
    """
    Accept a new task from the website and append it to queue.yaml.

    Expected JSON body:
        project      (str, required)  — repo / project name
        task_type    (str, required)  — "tars-code" or "tars-marketing"
        description  (str, required)  — what TARS should do
        title        (str, optional)  — short summary (auto-generated if omitted)
        priority     (int, optional)  — 1-100, default 50
        user_id      (str, optional)  — website user who submitted
    """
    data = request.get_json(silent=True) or {}
    err = _validate_task_payload(data)
    if err:
        return jsonify({"error": err}), 400

    task_id = f"web-{uuid.uuid4().hex[:8]}"
    title = data.get("title") or data["description"][:80]
    priority = data.get("priority", 50)

    task = {
        "id": task_id,
        "title": title,
        "description": data["description"],
        "project": data["project"],
        "task_type": data["task_type"],
        "priority": priority,
        "status": "pending",
        "user_id": data.get("user_id"),
        "source": "website",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Append to queue.yaml
    queue = _read_queue()
    queue["tasks"].append(task)

    # Keep tasks sorted by priority descending so TARS picks highest first.
    queue["tasks"].sort(key=lambda t: t.get("priority", 0), reverse=True)

    _write_queue(queue)

    return jsonify({"task": task}), 201


@app.route("/api/tasks/<task_id>/cancel", methods=["POST"])
@require_api_key
def cancel_task(task_id: str):
    """
    Cancel a pending/queued task by setting its status to 'cancelled'.

    Cannot cancel a task that is already in-progress (that would require
    killing the Claude subprocess, which is handled by the daemon).
    """
    queue = _read_queue()
    target = None
    for t in queue["tasks"]:
        if t.get("id") == task_id:
            target = t
            break

    if target is None:
        return jsonify({"error": f"Task {task_id} not found"}), 404

    if target.get("status") not in ("pending", "queued"):
        return jsonify({
            "error": f"Cannot cancel task in status '{target.get('status')}'. "
                     "Only pending or queued tasks can be cancelled."
        }), 409

    target["status"] = "cancelled"
    target["cancelled_at"] = datetime.now(timezone.utc).isoformat()
    _write_queue(queue)

    return jsonify({"task": target})


# ---------------------------------------------------------------------------
# Routes — Metrics
# ---------------------------------------------------------------------------


@app.route("/api/metrics", methods=["GET"])
@require_api_key
def metrics():
    """
    Return aggregated metrics from state/metrics.json and state/token_usage.json.
    """
    raw_metrics = _read_json(STATE_DIR / "metrics.json", {"days": {}})
    token_usage = _read_json(STATE_DIR / "token_usage.json", {})
    task_state = _read_json(STATE_DIR / "task_state.json", {"completed": []})

    # Aggregate totals across all days
    total_completed = 0
    total_failed = 0
    total_prs = 0
    total_cost = 0.0
    for day_data in raw_metrics.get("days", {}).values():
        total_completed += day_data.get("completed", 0)
        total_failed += day_data.get("failed", 0)
        total_prs += day_data.get("prs_created", 0)
        total_cost += day_data.get("cost_usd", 0.0)

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today = raw_metrics.get("days", {}).get(today_str, {})

    return jsonify({
        "totals": {
            "completed": total_completed,
            "failed": total_failed,
            "prs_created": total_prs,
            "cost_usd": round(total_cost, 4),
            "tasks_ever_completed": len(task_state.get("completed", [])),
        },
        "today": {
            "date": today_str,
            "completed": today.get("completed", 0),
            "failed": today.get("failed", 0),
            "prs_created": today.get("prs_created", 0),
            "cost_usd": round(today.get("cost_usd", 0.0), 4),
        },
        "token_usage": token_usage,
        "days": raw_metrics.get("days", {}),
    })


# ---------------------------------------------------------------------------
# Dashboard — visual GUI at /
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TARS Controller</title>
<style>
  :root {
    --bg: #0a0a0f;
    --surface: #12121a;
    --border: #1e1e2e;
    --text: #e4e4ed;
    --muted: #6b6b80;
    --accent: #6366f1;
    --green: #10b981;
    --orange: #f59e0b;
    --red: #ef4444;
    --blue: #3b82f6;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro', 'Inter', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
  }

  /* Top bar */
  .topbar {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 0.75rem 1.5rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
  }
  .topbar-brand {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    font-weight: 700;
    font-size: 1rem;
    letter-spacing: 0.04em;
  }
  .topbar-brand .logo {
    width: 28px; height: 28px;
    background: linear-gradient(135deg, var(--accent), #818cf8);
    border-radius: 6px;
    display: flex; align-items: center; justify-content: center;
    font-size: 0.8rem; font-weight: 800; color: #fff;
  }
  .topbar-status {
    display: flex;
    align-items: center;
    gap: 1.5rem;
    font-size: 0.8rem;
    color: var(--muted);
  }
  .pulse-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--green);
    animation: pulse 2s ease-in-out infinite;
    display: inline-block;
    margin-right: 0.4rem;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(16,185,129,0.5); }
    50% { opacity: 0.7; box-shadow: 0 0 0 6px rgba(16,185,129,0); }
  }

  /* Layout */
  .container { max-width: 1400px; margin: 0 auto; padding: 1.5rem; }
  .grid { display: grid; gap: 1.25rem; }
  .grid-4 { grid-template-columns: repeat(4, 1fr); }
  .grid-2 { grid-template-columns: 1fr 1fr; }
  .grid-3 { grid-template-columns: 2fr 1fr; }
  @media (max-width: 900px) {
    .grid-4, .grid-2, .grid-3 { grid-template-columns: 1fr; }
  }

  /* Cards */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.25rem;
  }
  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1rem;
  }
  .card-title {
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted);
  }

  /* Stat cards */
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.25rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .stat-label {
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .stat-value {
    font-size: 2rem;
    font-weight: 800;
    line-height: 1;
    color: var(--text);
  }
  .stat-sub {
    font-size: 0.75rem;
    color: var(--muted);
  }

  /* Worker nodes */
  .worker-node {
    display: flex;
    align-items: center;
    gap: 0.85rem;
    padding: 0.85rem;
    border: 1px solid var(--border);
    border-radius: 10px;
    margin-bottom: 0.6rem;
    transition: border-color 0.2s;
  }
  .worker-node:hover { border-color: var(--accent); }
  .worker-node .node-icon {
    width: 42px; height: 42px;
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-size: 1.1rem;
    flex-shrink: 0;
  }
  .worker-node .node-icon.brain {
    background: linear-gradient(135deg, rgba(99,102,241,0.2), rgba(129,140,248,0.1));
    color: var(--accent);
  }
  .worker-node .node-icon.worker {
    background: rgba(16,185,129,0.12);
    color: var(--green);
  }
  .worker-node .node-icon.offline {
    background: rgba(107,107,128,0.12);
    color: var(--muted);
  }
  .node-name { font-weight: 600; font-size: 0.88rem; }
  .node-role {
    font-size: 0.7rem;
    color: var(--muted);
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  /* Status badges */
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 0.2rem 0.55rem;
    border-radius: 999px;
  }
  .badge-green { background: rgba(16,185,129,0.15); color: var(--green); }
  .badge-orange { background: rgba(245,158,11,0.15); color: var(--orange); }
  .badge-red { background: rgba(239,68,68,0.15); color: var(--red); }
  .badge-blue { background: rgba(59,130,246,0.15); color: var(--blue); }
  .badge-muted { background: rgba(107,107,128,0.12); color: var(--muted); }

  /* Task list */
  .task-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.65rem 0;
    border-bottom: 1px solid rgba(30,30,46,0.6);
    font-size: 0.85rem;
  }
  .task-row:last-child { border-bottom: none; }
  .task-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .task-id {
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 0.72rem;
    color: var(--accent);
    background: rgba(99,102,241,0.1);
    padding: 0.15rem 0.45rem;
    border-radius: 4px;
    flex-shrink: 0;
  }
  .task-title {
    flex-grow: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .task-project {
    font-size: 0.72rem;
    color: var(--muted);
    flex-shrink: 0;
  }
  .task-priority {
    font-size: 0.72rem;
    font-weight: 600;
    color: var(--muted);
    flex-shrink: 0;
    width: 28px;
    text-align: center;
  }

  /* Current task highlight */
  .current-task {
    background: linear-gradient(135deg, rgba(99,102,241,0.08), rgba(99,102,241,0.03));
    border: 1px solid rgba(99,102,241,0.25);
    border-radius: 10px;
    padding: 1rem 1.25rem;
    margin-bottom: 1rem;
  }
  .current-task-label {
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--accent);
    margin-bottom: 0.4rem;
  }
  .current-task-title {
    font-weight: 600;
    font-size: 0.95rem;
    margin-bottom: 0.25rem;
  }
  .current-task-meta {
    font-size: 0.75rem;
    color: var(--muted);
    display: flex;
    align-items: center;
    gap: 1rem;
  }
  .spinner {
    display: inline-block;
    width: 14px; height: 14px;
    border: 2px solid rgba(99,102,241,0.3);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 1s linear infinite;
    margin-right: 0.4rem;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Distribution bar */
  .dist-bar {
    display: flex;
    height: 8px;
    border-radius: 4px;
    overflow: hidden;
    background: var(--border);
    margin-top: 0.5rem;
  }
  .dist-bar .seg {
    height: 100%;
    transition: width 0.5s ease;
  }
  .dist-legend {
    display: flex;
    gap: 1rem;
    margin-top: 0.5rem;
    font-size: 0.72rem;
    color: var(--muted);
  }
  .dist-legend span::before {
    content: '';
    display: inline-block;
    width: 8px; height: 8px;
    border-radius: 2px;
    margin-right: 0.35rem;
    vertical-align: middle;
  }
  .dist-legend .pending::before { background: var(--muted); }
  .dist-legend .in-progress::before { background: var(--accent); }
  .dist-legend .completed::before { background: var(--green); }
  .dist-legend .failed::before { background: var(--red); }

  /* Refresh indicator */
  .refresh-bar {
    font-size: 0.7rem;
    color: var(--muted);
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  /* Scrollable task list */
  .task-scroll {
    max-height: 420px;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: var(--border) transparent;
  }
  .task-scroll::-webkit-scrollbar { width: 5px; }
  .task-scroll::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  .empty-state {
    text-align: center;
    padding: 2rem;
    color: var(--muted);
    font-size: 0.85rem;
  }

  /* Control buttons */
  .ctrl-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.4rem 0.85rem;
    border-radius: 8px;
    font-size: 0.78rem;
    font-weight: 600;
    border: 1px solid;
    cursor: pointer;
    transition: all 0.2s;
  }
  .ctrl-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .ctrl-btn.start {
    background: rgba(16,185,129,0.12);
    border-color: rgba(16,185,129,0.3);
    color: var(--green);
  }
  .ctrl-btn.start:hover:not(:disabled) { background: rgba(16,185,129,0.25); }
  .ctrl-btn.stop {
    background: rgba(239,68,68,0.12);
    border-color: rgba(239,68,68,0.3);
    color: var(--red);
  }
  .ctrl-btn.stop:hover:not(:disabled) { background: rgba(239,68,68,0.25); }
  .ctrl-btn.cancel-btn {
    background: rgba(239,68,68,0.08);
    border-color: rgba(239,68,68,0.2);
    color: var(--red);
    font-size: 0.65rem;
    padding: 0.15rem 0.45rem;
  }
  .ctrl-btn.cancel-btn:hover:not(:disabled) { background: rgba(239,68,68,0.2); }
  .daemon-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    font-size: 0.75rem;
    font-weight: 600;
    padding: 0.25rem 0.65rem;
    border-radius: 999px;
  }
  .daemon-badge.running { background: rgba(16,185,129,0.15); color: var(--green); }
  .daemon-badge.stopped { background: rgba(239,68,68,0.15); color: var(--red); }
</style>
</head>
<body>

<!-- Top bar -->
<div class="topbar">
  <div class="topbar-brand">
    <div class="logo">T</div>
    TARS Controller
  </div>
  <div class="topbar-status">
    <span class="daemon-badge stopped" id="daemon-badge">&#9679; Checking...</span>
    <button class="ctrl-btn start" id="btn-start" onclick="daemonStart()" disabled>&#9654; Start TARS</button>
    <button class="ctrl-btn stop" id="btn-stop" onclick="daemonStop()" disabled>&#9632; Stop TARS</button>
    <div class="refresh-bar">
      <span class="pulse-dot"></span>
      <span id="last-refresh">Loading...</span>
    </div>
  </div>
</div>

<div class="container">

  <!-- Stat cards row -->
  <div class="grid grid-4" style="margin-bottom:1.25rem;">
    <div class="stat-card">
      <div class="stat-label">Workers Online</div>
      <div class="stat-value" id="workers-online">-</div>
      <div class="stat-sub" id="workers-sub">of - total</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Queue Depth</div>
      <div class="stat-value" id="queue-depth">-</div>
      <div class="stat-sub">pending tasks</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Completed Today</div>
      <div class="stat-value" id="completed-today">-</div>
      <div class="stat-sub" id="failed-today">- failed</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Total Completed</div>
      <div class="stat-value" id="total-completed">-</div>
      <div class="stat-sub" id="total-prs">- PRs created</div>
    </div>
  </div>

  <!-- Main content grid -->
  <div class="grid grid-3">

    <!-- Left: Tasks -->
    <div>
      <!-- Current Task -->
      <div id="current-task-card" class="current-task" style="display:none;">
        <div class="current-task-label">
          <span class="spinner"></span> Currently Executing
        </div>
        <div class="current-task-title" id="current-task-title"></div>
        <div class="current-task-meta">
          <span id="current-task-id"></span>
          <span id="current-task-project"></span>
          <span id="current-task-time"></span>
        </div>
      </div>
      <div id="no-current-task" class="current-task" style="border-color:var(--border); background:var(--surface);">
        <div class="current-task-label" style="color:var(--muted);">No Active Task</div>
        <div style="font-size:0.85rem; color:var(--muted);">TARS is idle — waiting for next task in queue.</div>
      </div>

      <!-- Task Distribution -->
      <div class="card" style="margin-bottom:1.25rem;">
        <div class="card-header">
          <span class="card-title">Task Distribution</span>
          <span class="badge badge-muted" id="total-tasks-badge">0 tasks</span>
        </div>
        <div class="dist-bar">
          <div class="seg" id="dist-completed" style="background:var(--green); width:0%"></div>
          <div class="seg" id="dist-active" style="background:var(--accent); width:0%"></div>
          <div class="seg" id="dist-pending" style="background:var(--muted); width:0%"></div>
          <div class="seg" id="dist-failed" style="background:var(--red); width:0%"></div>
        </div>
        <div class="dist-legend">
          <span class="completed" id="dist-completed-n">0 completed</span>
          <span class="in-progress" id="dist-active-n">0 active</span>
          <span class="pending" id="dist-pending-n">0 pending</span>
          <span class="failed" id="dist-failed-n">0 failed</span>
        </div>
      </div>

      <!-- Task Queue -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">Task Queue</span>
        </div>
        <div class="task-scroll" id="task-list">
          <div class="empty-state">Loading tasks...</div>
        </div>
      </div>
    </div>

    <!-- Right: Workers + Metrics -->
    <div>
      <!-- Cluster Nodes -->
      <div class="card" style="margin-bottom:1.25rem;">
        <div class="card-header">
          <span class="card-title">Cluster Nodes</span>
        </div>

        <!-- Brain node (always shown) -->
        <div class="worker-node">
          <div class="node-icon brain">&#9881;</div>
          <div style="flex-grow:1;">
            <div class="node-name">Mac Mini (Brain)</div>
            <div class="node-role">
              <span class="badge badge-blue">coordinator</span>
              localhost
            </div>
          </div>
          <span class="badge badge-green">online</span>
        </div>

        <div id="worker-list"></div>

        <div style="margin-top:0.75rem; padding:0.65rem; border:1px dashed var(--border); border-radius:8px; text-align:center;">
          <div style="font-size:0.75rem; color:var(--muted);">
            Add workers by connecting more Mac Minis to the cluster
          </div>
        </div>
      </div>

      <!-- Active Projects -->
      <div class="card" style="margin-bottom:1.25rem;">
        <div class="card-header">
          <span class="card-title">Active Projects</span>
        </div>
        <div id="active-projects">
          <div class="empty-state">Loading...</div>
        </div>
      </div>

      <!-- Metrics -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">All-Time Metrics</span>
        </div>
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:0.6rem;" id="metrics-grid">
          <div style="padding:0.6rem; background:rgba(16,185,129,0.06); border-radius:8px; text-align:center;">
            <div style="font-size:1.3rem; font-weight:700;" id="m-completed">-</div>
            <div style="font-size:0.65rem; color:var(--muted); text-transform:uppercase; letter-spacing:0.08em;">Completed</div>
          </div>
          <div style="padding:0.6rem; background:rgba(239,68,68,0.06); border-radius:8px; text-align:center;">
            <div style="font-size:1.3rem; font-weight:700;" id="m-failed">-</div>
            <div style="font-size:0.65rem; color:var(--muted); text-transform:uppercase; letter-spacing:0.08em;">Failed</div>
          </div>
          <div style="padding:0.6rem; background:rgba(59,130,246,0.06); border-radius:8px; text-align:center;">
            <div style="font-size:1.3rem; font-weight:700;" id="m-prs">-</div>
            <div style="font-size:0.65rem; color:var(--muted); text-transform:uppercase; letter-spacing:0.08em;">PRs Created</div>
          </div>
          <div style="padding:0.6rem; background:rgba(245,158,11,0.06); border-radius:8px; text-align:center;">
            <div style="font-size:1.3rem; font-weight:700;" id="m-cost">-</div>
            <div style="font-size:0.65rem; color:var(--muted); text-transform:uppercase; letter-spacing:0.08em;">Cost (USD)</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const API_KEY = '{{ api_key }}';
const HEADERS = {'X-API-Key': API_KEY, 'Content-Type': 'application/json'};

function statusColor(s) {
  if (s === 'completed') return 'var(--green)';
  if (s === 'failed') return 'var(--red)';
  if (['in_progress','assigned','reviewing'].includes(s)) return 'var(--accent)';
  if (s === 'queued') return 'var(--orange)';
  return 'var(--muted)';
}

function badgeClass(s) {
  if (s === 'completed') return 'badge-green';
  if (s === 'failed') return 'badge-red';
  if (['in_progress','assigned','reviewing'].includes(s)) return 'badge-blue';
  if (s === 'queued') return 'badge-orange';
  return 'badge-muted';
}

function timeAgo(epoch) {
  if (!epoch) return '';
  const diff = Math.floor(Date.now()/1000 - epoch);
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff/60) + 'm ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  return Math.floor(diff/86400) + 'd ago';
}

async function fetchJSON(url) {
  try {
    const r = await fetch(url, {headers: HEADERS});
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

async function refresh() {
  const [status, tasks, metrics] = await Promise.all([
    fetchJSON('/api/status'),
    fetchJSON('/api/tasks'),
    fetchJSON('/api/metrics'),
  ]);

  // Refresh timestamp
  document.getElementById('last-refresh').textContent =
    'Updated ' + new Date().toLocaleTimeString();

  if (status) {
    document.getElementById('workers-online').textContent = status.workers_online;
    document.getElementById('workers-sub').textContent = 'of ' + status.workers_total + ' total';
    document.getElementById('queue-depth').textContent = status.queue_length;

    // Current task
    const ct = status.current_task;
    if (ct && ct.id) {
      document.getElementById('current-task-card').style.display = 'block';
      document.getElementById('no-current-task').style.display = 'none';
      document.getElementById('current-task-title').textContent = ct.title || ct.id;
      document.getElementById('current-task-id').textContent = ct.id;
      document.getElementById('current-task-project').textContent = ct.project || '';
      document.getElementById('current-task-time').textContent = ct.started ? 'started ' + timeAgo(ct.started) : '';
    } else {
      document.getElementById('current-task-card').style.display = 'none';
      document.getElementById('no-current-task').style.display = 'block';
    }

    // Active projects
    const projDiv = document.getElementById('active-projects');
    const projs = status.active_projects || [];
    if (projs.length) {
      projDiv.innerHTML = projs.map(p =>
        '<div style="display:flex;align-items:center;gap:0.5rem;padding:0.5rem 0;border-bottom:1px solid var(--border);">' +
        '<span style="font-size:1rem;">&#128193;</span>' +
        '<span style="font-weight:600;font-size:0.85rem;">' + p + '</span>' +
        '<span class="badge badge-green" style="margin-left:auto;">active</span></div>'
      ).join('');
    } else {
      projDiv.innerHTML = '<div class="empty-state">No active projects</div>';
    }
  }

  if (tasks) {
    const list = tasks.tasks || [];
    const el = document.getElementById('task-list');

    // Distribution
    let pending=0, active=0, completed=0, failed=0;
    list.forEach(t => {
      const s = t.status;
      if (s === 'completed') completed++;
      else if (s === 'failed') failed++;
      else if (['in_progress','assigned','reviewing','queued'].includes(s)) active++;
      else pending++;
    });
    const total = list.length || 1;
    document.getElementById('total-tasks-badge').textContent = list.length + ' tasks';
    document.getElementById('dist-completed').style.width = (completed/total*100)+'%';
    document.getElementById('dist-active').style.width = (active/total*100)+'%';
    document.getElementById('dist-pending').style.width = (pending/total*100)+'%';
    document.getElementById('dist-failed').style.width = (failed/total*100)+'%';
    document.getElementById('dist-completed-n').textContent = completed + ' completed';
    document.getElementById('dist-active-n').textContent = active + ' active';
    document.getElementById('dist-pending-n').textContent = pending + ' pending';
    document.getElementById('dist-failed-n').textContent = failed + ' failed';

    if (list.length === 0) {
      el.innerHTML = '<div class="empty-state">No tasks in queue</div>';
    } else {
      el.innerHTML = list.map(t => {
        const canCancel = ['pending','queued'].includes(t.status);
        return '<div class="task-row">' +
        '<div class="task-dot" style="background:' + statusColor(t.status) + '"></div>' +
        '<span class="task-id">' + (t.id||'?') + '</span>' +
        '<span class="task-title">' + (t.title||'Untitled') + '</span>' +
        '<span class="task-project">' + (t.project||'') + '</span>' +
        '<span class="task-priority">P' + (t.priority||50) + '</span>' +
        '<span class="badge ' + badgeClass(t.status) + '">' + (t.status||'pending') + '</span>' +
        (canCancel ? '<button class="ctrl-btn cancel-btn" onclick="cancelTask(\'' + t.id + '\')">&#10005;</button>' : '') +
        '</div>';
      }).join('');
    }
  }

  if (metrics) {
    document.getElementById('completed-today').textContent = metrics.today?.completed || 0;
    document.getElementById('failed-today').textContent = (metrics.today?.failed || 0) + ' failed';
    document.getElementById('total-completed').textContent = metrics.totals?.completed || 0;
    document.getElementById('total-prs').textContent = (metrics.totals?.prs_created || 0) + ' PRs created';
    document.getElementById('m-completed').textContent = metrics.totals?.completed || 0;
    document.getElementById('m-failed').textContent = metrics.totals?.failed || 0;
    document.getElementById('m-prs').textContent = metrics.totals?.prs_created || 0;
    document.getElementById('m-cost').textContent = '$' + (metrics.totals?.cost_usd || 0).toFixed(2);
  }
}

// Daemon controls
async function refreshDaemon() {
  const d = await fetchJSON('/api/daemon/status');
  if (!d) return;
  const badge = document.getElementById('daemon-badge');
  const btnStart = document.getElementById('btn-start');
  const btnStop = document.getElementById('btn-stop');
  if (d.running) {
    badge.className = 'daemon-badge running';
    badge.innerHTML = '&#9679; TARS Running (PID ' + d.pid + ')';
    btnStart.disabled = true;
    btnStop.disabled = false;
  } else {
    badge.className = 'daemon-badge stopped';
    badge.innerHTML = '&#9679; TARS Stopped';
    btnStart.disabled = false;
    btnStop.disabled = true;
  }
}

async function daemonStart() {
  document.getElementById('btn-start').disabled = true;
  document.getElementById('btn-start').innerHTML = '&#9654; Starting...';
  await fetch('/api/daemon/start', {method:'POST', headers: HEADERS});
  setTimeout(() => { refreshDaemon(); document.getElementById('btn-start').innerHTML = '&#9654; Start TARS'; }, 3000);
}

async function daemonStop() {
  document.getElementById('btn-stop').disabled = true;
  document.getElementById('btn-stop').innerHTML = '&#9632; Stopping...';
  await fetch('/api/daemon/stop', {method:'POST', headers: HEADERS});
  setTimeout(() => { refreshDaemon(); document.getElementById('btn-stop').innerHTML = '&#9632; Stop TARS'; }, 2000);
}

async function cancelTask(taskId) {
  if (!confirm('Cancel task ' + taskId + '?')) return;
  await fetch('/api/tasks/' + taskId + '/cancel', {method:'POST', headers: HEADERS});
  refresh();
}

// Initial load + auto-refresh every 5 seconds
refresh();
refreshDaemon();
setInterval(refresh, 5000);
setInterval(refreshDaemon, 5000);
</script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def dashboard():
    """Visual dashboard showing cluster status, tasks, workers, metrics."""
    return render_template_string(DASHBOARD_HTML, api_key=API_KEY)


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


@app.errorhandler(404)
def not_found(_e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(405)
def method_not_allowed(_e):
    return jsonify({"error": "Method not allowed"}), 405


@app.errorhandler(500)
def internal_error(_e):
    return jsonify({"error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("TARS_CONTROLLER_PORT", 8420))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
