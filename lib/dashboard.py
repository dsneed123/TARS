"""
TARS Web Dashboard — real-time monitoring and control panel.

Reads state JSON files directly (no heavy Python object instantiation).
POST endpoints for daemon control, project creation, queue management.

Run: python -m lib.dashboard --port 8421   # public Flask controller owns 8420
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import yaml
from datetime import date, datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

TARS_HOME = Path(__file__).resolve().parent.parent
if str(TARS_HOME) not in sys.path:
    sys.path.insert(0, str(TARS_HOME))

os.environ.setdefault("TARS_HOME", str(TARS_HOME))

app = FastAPI(title="TARS Dashboard")

STATE_DIR = TARS_HOME / "state"
LOGS_DIR = TARS_HOME / "logs"
CONFIG_DIR = TARS_HOME / "config"
QUEUES_DIR = CONFIG_DIR / "queues"
PROJECTS_DIR = CONFIG_DIR / "projects"
BIN_DIR = TARS_HOME / "bin"

# ---------------------------------------------------------------------------
# Lightweight state readers (no heavy object instantiation)
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _read_yaml(path: Path) -> dict:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _pid_alive(pid_file: Path) -> dict:
    if not pid_file.exists():
        return {"running": False, "pid": None}
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        uptime = int(time.time() - pid_file.stat().st_mtime)
        return {"running": True, "pid": pid, "uptime_s": uptime}
    except (ProcessLookupError, PermissionError, ValueError, OSError):
        return {"running": False, "pid": None}


def _tail_file(path: Path, n: int = 50) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n * 300))
            return f.read().decode("utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return []


def _find_task_log(task_id: str) -> Path | None:
    """Find the most recent log file for a task ID."""
    prefix = f"task_{task_id}_"
    try:
        matches = sorted(LOGS_DIR.glob(f"{prefix}*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        return matches[0] if matches else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Cached config (projects + budget rarely change)
# ---------------------------------------------------------------------------

_cache = {"projects": None, "projects_t": 0, "budget": None, "budget_t": 0}
CACHE_TTL = 30  # seconds


def _get_projects() -> list[dict]:
    now = time.time()
    if _cache["projects"] is not None and now - _cache["projects_t"] < CACHE_TTL:
        return _cache["projects"]
    projects = []
    if PROJECTS_DIR.exists():
        for p in sorted(PROJECTS_DIR.glob("*.yaml")):
            cfg = _read_yaml(p)
            if cfg.get("enabled", True):
                cfg["_name"] = p.stem
                projects.append(cfg)
    _cache["projects"] = projects
    _cache["projects_t"] = now
    return projects


def _get_budget() -> dict:
    now = time.time()
    if _cache["budget"] is not None and now - _cache["budget_t"] < CACHE_TTL:
        return _cache["budget"]
    defaults = {"daily_limit": 1_000_000, "peak_hours": {"start": 9, "end": 17},
                "peak_multiplier": 0.5, "warning_threshold": 0.8}
    raw = _read_yaml(CONFIG_DIR / "token_budget.yaml")
    defaults.update(raw)
    _cache["budget"] = defaults
    _cache["budget_t"] = now
    return defaults


def _get_queue_tasks() -> list[dict]:
    """Read all per-project queues — no GitHub/Claude calls."""
    completed_ids = set(_read_json(STATE_DIR / "task_state.json").get("completed", []))
    result = []
    if QUEUES_DIR.exists():
        for qf in sorted(QUEUES_DIR.glob("*.yaml")):
            project_name = qf.stem
            raw = _read_yaml(qf)
            for t in raw.get("tasks", []):
                if t.get("status", "pending") != "pending":
                    continue
                t.setdefault("project", project_name)
                tid = f"manual-{t.get('id', '')}"
                if tid in completed_ids:
                    continue
                t["_full_id"] = tid
                t["source"] = t.get("source", "manual")
                t["priority"] = t.get("priority", 100)
                result.append(t)
    return sorted(result, key=lambda x: x.get("priority", 0), reverse=True)


# ---------------------------------------------------------------------------
# Per-project queue helpers
# ---------------------------------------------------------------------------

def _project_queue_file(project: str) -> Path:
    """Get the queue file path for a project."""
    QUEUES_DIR.mkdir(parents=True, exist_ok=True)
    return QUEUES_DIR / f"{project}.yaml"


# ---------------------------------------------------------------------------
# Auto-populate worker (background thread)
# ---------------------------------------------------------------------------

_auto_populate = {"running": False, "project": "", "tasks": [], "error": ""}


def _auto_populate_worker(project_name: str):
    """Background thread: run Claude discovery and write tasks to queue."""
    global _auto_populate
    try:
        from lib.config_loader import load_project
        from lib.git_manager import GitManager
        from lib.claude_runner import ClaudeRunner

        cfg = load_project(project_name)
        repo = cfg.get("repo", "")
        if not repo:
            _auto_populate["error"] = f"No repo configured for {project_name}"
            _auto_populate["running"] = False
            return

        focus = ", ".join(cfg.get("auto_discover", {}).get("focus_areas", ["code quality"]))
        description = cfg.get("description", "")
        project_type = cfg.get("type", cfg.get("build", {}).get("type", "generic"))
        gm = GitManager(repo)
        work_dir = gm.ensure_cloned()

        runner = ClaudeRunner(model=cfg.get("claude", {}).get("model", "sonnet"))
        result = runner.run_with_prompt_file(
            "discover_improvements.md",
            variables={
                "REPO_NAME": repo,
                "FOCUS_AREAS": focus,
                "PROJECT_DESCRIPTION": description or f"A {project_type} project",
                "PROJECT_TYPE": project_type,
            },
            cwd=str(work_dir),
            max_turns=5,
            timeout=300,
        )

        # Parse suggestions using shared parser
        from lib.task_manager import _parse_suggestion_array
        text = result.get("result", "")
        suggestions = _parse_suggestion_array(text)

        if not suggestions:
            _auto_populate["error"] = "No suggestions found in Claude response"
            _auto_populate["running"] = False
            return

        # Write discovered tasks into per-project queue
        queue_file = _project_queue_file(project_name)
        raw = _read_yaml(queue_file)
        tasks = raw.get("tasks", [])
        existing_ids = [t.get("id", "") for t in tasks]
        prefix = project_name[:2] if project_name else "au"

        new_tasks = []
        for s in suggestions:
            idx = 1
            while f"{prefix}-{idx:02d}" in existing_ids:
                idx += 1
            new_id = f"{prefix}-{idx:02d}"
            existing_ids.append(new_id)
            entry = {
                "id": new_id,
                "title": s.get("title", "Improvement"),
                "description": s.get("description", ""),
                "project": project_name,
                "priority": s.get("priority_score", 50) if isinstance(s.get("priority_score"), int) else 50,
                "status": "pending",
                "source": "auto",
            }
            tasks.append(entry)
            new_tasks.append(entry)

        raw["tasks"] = tasks
        with open(queue_file, "w") as f:
            yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

        _auto_populate["tasks"] = new_tasks
    except Exception as e:
        _auto_populate["error"] = str(e)
    finally:
        _auto_populate["running"] = False


# ---------------------------------------------------------------------------
# Pipeline stage detection (from daemon log)
# ---------------------------------------------------------------------------

_STAGE_ORDER = ["discover", "implement", "build", "test", "review", "push", "done"]

_STAGE_PATTERNS = [
    ("done",      "Task complete:"),
    ("failed",    "All auto-patch attempts failed"),
    ("failed",    "Task failed:"),
    ("push",      "Committing and pushing"),
    ("review",    "Running self-review"),
    ("patch",     "Auto-patch attempt"),
    ("test",      "Running tests:"),
    ("build",     "Running build:"),
    ("implement", "Running Claude implementation"),
    ("discover",  "=== Daemon cycle start ==="),
]


def detect_stage(task_id: str | None) -> dict:
    """Detect current pipeline stage from daemon log for the active task."""
    result = {"stage": None, "detail": "", "completed": []}
    if not task_id:
        return result
    lines = _tail_file(LOGS_DIR / "daemon.log", 40)
    # Scan in reverse to find latest matching pattern
    for line in reversed(lines):
        for stage, pattern in _STAGE_PATTERNS:
            if pattern in line:
                if stage == "patch":
                    detail = ""
                    idx = line.find("Auto-patch attempt")
                    if idx != -1:
                        rest = line[idx + len("Auto-patch attempt"):].strip()
                        detail = rest.split()[0] if rest else ""
                    result["stage"] = "patch"
                    result["detail"] = detail
                    result["completed"] = ["discover", "implement", "build", "test"]
                elif stage == "failed":
                    result["stage"] = "failed"
                    # Scan all lines to find highest completed stage
                    result["completed"] = _find_completed(lines)
                elif stage == "done":
                    result["stage"] = "done"
                    result["completed"] = _STAGE_ORDER[:]
                else:
                    idx = _STAGE_ORDER.index(stage) if stage in _STAGE_ORDER else 0
                    result["stage"] = stage
                    result["completed"] = _STAGE_ORDER[:idx]
                return result
    return result


def _find_completed(lines: list[str]) -> list[str]:
    """Scan log lines to determine which stages completed before failure."""
    # Map non-terminal stage patterns to their order index
    seen_max = -1
    for line in lines:
        for stage, pattern in _STAGE_PATTERNS:
            if stage in ("done", "failed", "patch"):
                continue
            if pattern in line and stage in _STAGE_ORDER:
                idx = _STAGE_ORDER.index(stage)
                if idx > seen_max:
                    seen_max = idx
    return _STAGE_ORDER[:seen_max] if seen_max > 0 else []


# ---------------------------------------------------------------------------
# State snapshot (lightweight — reads files, not Python objects)
# ---------------------------------------------------------------------------

def build_state() -> dict:
    daemon = _pid_alive(STATE_DIR / "daemon.pid")
    daemon_status = _read_json(STATE_DIR / "daemon_status.json")

    # If daemon is paused for token budget, calculate remaining wait
    if daemon_status.get("paused"):
        remaining = max(0, int(daemon_status.get("until", 0) - time.time()))
        daemon_status["remaining_s"] = remaining
        if remaining <= 0:
            daemon_status = {}

    # Token usage (direct file read)
    usage = _read_json(STATE_DIR / "token_usage.json")
    if usage.get("date") != str(date.today()):
        usage = {"date": str(date.today()), "tokens_in": 0, "tokens_out": 0,
                 "total_tokens": 0, "total_cost": 0.0, "requests": 0, "hourly": {}}
    budget = _get_budget()
    daily_limit = budget.get("daily_limit", 1)
    total_tokens = usage.get("total_tokens", 0)
    pct = round(total_tokens / max(daily_limit, 1) * 100, 1)

    # Metrics (direct file read)
    metrics_data = _read_json(STATE_DIR / "metrics.json")
    today_key = str(date.today())
    today_stats = metrics_data.get("days", {}).get(today_key, {
        "completed": 0, "failed": 0, "tasks": [], "prs_created": 0})
    today_stats["tokens_used"] = total_tokens
    today_stats["cost_usd"] = usage.get("total_cost", 0.0)

    # Current task
    current = _read_json(STATE_DIR / "current_task.json")
    if current and current.get("started"):
        current["elapsed_s"] = int(time.time() - current["started"])

    # Circuit breakers
    cb_raw = _read_json(STATE_DIR / "circuit_breakers.json")
    breakers = {}
    now = time.time()
    for proj, data in cb_raw.items():
        locked_until = data.get("locked_until", 0)
        breakers[proj] = {
            "consecutive_failures": data.get("consecutive_failures", 0),
            "total_failures": data.get("total_failures", 0),
            "is_locked": locked_until > now,
            "locked_until": locked_until,
            "remaining_lockout": max(0, int(locked_until - now)),
            "last_error": data.get("last_error", ""),
            "last_task": data.get("last_task", ""),
        }

    # Pipeline stage detection
    task_id = current.get("id") if current else None
    pipeline = detect_stage(task_id) if task_id else {"stage": None, "detail": "", "completed": []}

    return {
        "timestamp": datetime.now().isoformat(),
        "daemon": daemon,
        "daemon_status": daemon_status,
        "tokens": {
            "usage": usage,
            "budget": budget,
            "remaining": max(0, daily_limit - total_tokens),
            "pct": pct,
            "warning": pct >= budget.get("warning_threshold", 0.8) * 100,
        },
        "metrics": today_stats,
        "current_task": current if current else None,
        "pipeline": pipeline,
        "queue": _get_queue_tasks(),
        "completed": today_stats.get("tasks", []),
        "circuit_breakers": breakers,
        "projects": _get_projects(),
        "active_projects": _get_active_projects(),
    }


# ---------------------------------------------------------------------------
# REST endpoints — read
# ---------------------------------------------------------------------------

@app.get("/api/state")
def api_state():
    return JSONResponse(build_state())


@app.get("/api/logs/daemon")
def api_logs_daemon(lines: int = 80):
    return JSONResponse({"lines": _tail_file(LOGS_DIR / "daemon.log", lines)})


@app.get("/api/logs/task/{task_id}")
def api_logs_task(task_id: str, lines: int = 80):
    log_file = _find_task_log(task_id)
    if log_file:
        return JSONResponse({"lines": _tail_file(log_file, lines)})
    return JSONResponse({"lines": []})


@app.get("/api/metrics/history")
def api_metrics_history():
    return JSONResponse(_read_json(STATE_DIR / "metrics.json").get("days", {}))


# ---------------------------------------------------------------------------
# REST endpoints — control
# ---------------------------------------------------------------------------

def _run_tars(args: list[str], timeout: int = 10) -> dict:
    """Run a tars.sh subcommand."""
    try:
        r = subprocess.run(
            [str(TARS_HOME / "tars.sh")] + args,
            capture_output=True, text=True, timeout=timeout,
            cwd=str(TARS_HOME),
        )
        return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "Command timed out"}
    except Exception as e:
        return {"ok": False, "output": str(e)}


@app.post("/api/daemon/start")
def api_daemon_start():
    return JSONResponse(_run_tars(["start"]))


@app.post("/api/daemon/stop")
def api_daemon_stop():
    return JSONResponse(_run_tars(["stop"], timeout=35))


@app.post("/api/daemon/restart")
def api_daemon_restart():
    return JSONResponse(_run_tars(["restart"], timeout=40))


@app.post("/api/breaker/reset")
async def api_breaker_reset(body: dict = {}):
    project = body.get("project", "")
    if not project:
        return JSONResponse({"ok": False, "output": "Missing project name"}, 400)
    try:
        from lib.error_analyzer import ErrorAnalyzer
        ea = ErrorAnalyzer()
        ea.reset_failures(project)
        return JSONResponse({"ok": True, "output": f"Circuit breaker reset for {project}"})
    except Exception as e:
        return JSONResponse({"ok": False, "output": str(e)})


@app.post("/api/project/create")
async def api_project_create(body: dict = {}):
    name = body.get("name", "").strip()
    project_type = body.get("type", "python").strip()
    description = body.get("description", "").strip()
    visibility = body.get("visibility", "public")
    org = body.get("org", "")
    if not name:
        return JSONResponse({"ok": False, "output": "Project name required"}, 400)
    try:
        from lib.project_creator import create_project
        result = create_project(
            name=name, project_type=project_type,
            description=description, visibility=visibility, org=org,
        )
        _cache["projects"] = None  # invalidate cache
        return JSONResponse({"ok": True, "output": json.dumps(result, indent=2)})
    except Exception as e:
        return JSONResponse({"ok": False, "output": str(e)})


@app.post("/api/queue/add")
async def api_queue_add(body: dict = {}):
    title = body.get("title", "").strip()
    project = body.get("project", "").strip()
    if not title or not project:
        return JSONResponse({"ok": False, "output": "Title and project required"}, 400)
    queue_file = _project_queue_file(project)
    raw = _read_yaml(queue_file)
    tasks = raw.get("tasks", [])
    existing_ids = [t.get("id", "") for t in tasks]
    prefix = project[:2] if project else "t"
    idx = 1
    while f"{prefix}-{idx:02d}" in existing_ids:
        idx += 1
    new_id = f"{prefix}-{idx:02d}"
    tasks.append({
        "id": new_id,
        "title": title,
        "description": body.get("description", title),
        "project": project,
        "priority": body.get("priority", 50),
        "status": "pending",
    })
    raw["tasks"] = tasks
    with open(queue_file, "w") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    return JSONResponse({"ok": True, "output": f"Task {new_id} added"})


@app.post("/api/queue/remove")
async def api_queue_remove(body: dict = {}):
    task_id = body.get("id", "").strip()
    if not task_id:
        return JSONResponse({"ok": False, "output": "Task ID required"}, 400)
    # Search all project queues for the task
    if QUEUES_DIR.exists():
        for qf in QUEUES_DIR.glob("*.yaml"):
            raw = _read_yaml(qf)
            tasks = raw.get("tasks", [])
            filtered = [t for t in tasks if t.get("id") != task_id]
            if len(filtered) < len(tasks):
                raw["tasks"] = filtered
                with open(qf, "w") as f:
                    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
                return JSONResponse({"ok": True, "output": f"Task {task_id} removed"})
    return JSONResponse({"ok": False, "output": f"Task {task_id} not found"})


@app.post("/api/tasks/auto-populate")
async def api_auto_populate(body: dict = {}):
    project = body.get("project", "").strip()
    if not project:
        return JSONResponse({"ok": False, "error": "Project name required"}, 400)
    if _auto_populate["running"]:
        return JSONResponse({"ok": False, "error": "Auto-populate already running"}, 409)
    _auto_populate["running"] = True
    _auto_populate["project"] = project
    _auto_populate["tasks"] = []
    _auto_populate["error"] = ""
    threading.Thread(target=_auto_populate_worker, args=(project,), daemon=True).start()
    return JSONResponse({"ok": True, "status": "running"})


@app.get("/api/tasks/auto-populate/status")
def api_auto_populate_status():
    return JSONResponse({
        "running": _auto_populate["running"],
        "project": _auto_populate["project"],
        "tasks": _auto_populate["tasks"],
        "error": _auto_populate["error"],
    })


# ---------------------------------------------------------------------------
# Active projects — controls which projects the daemon works on
# ---------------------------------------------------------------------------

ACTIVE_PROJECTS_FILE = STATE_DIR / "active_projects.json"


def _get_active_projects() -> list[str]:
    """Read the list of active project names the daemon should work on."""
    data = _read_json(ACTIVE_PROJECTS_FILE)
    return data.get("active", [])


def _set_active_projects(active: list[str]):
    """Write the active projects list."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(ACTIVE_PROJECTS_FILE, "w") as f:
        json.dump({"active": active}, f)


@app.get("/api/projects/active")
def api_active_projects():
    return JSONResponse({"active": _get_active_projects()})


@app.post("/api/projects/active")
async def api_set_active_projects(body: dict = {}):
    active = body.get("active", [])
    if not isinstance(active, list):
        return JSONResponse({"ok": False, "output": "active must be a list"}, 400)
    _set_active_projects(active)
    return JSONResponse({"ok": True, "active": active})


@app.post("/api/projects/toggle")
async def api_toggle_project(body: dict = {}):
    project = body.get("project", "").strip()
    if not project:
        return JSONResponse({"ok": False, "output": "Project name required"}, 400)
    active = _get_active_projects()
    if project in active:
        active.remove(project)
    else:
        active.append(project)
    _set_active_projects(active)
    return JSONResponse({"ok": True, "active": active})


# ---------------------------------------------------------------------------
# WebSocket — live log streaming + state snapshots
# ---------------------------------------------------------------------------

class LogWatcher:
    __slots__ = ("path", "pos")

    def __init__(self, path: Path):
        self.path = path
        try:
            self.pos = path.stat().st_size
        except OSError:
            self.pos = 0

    def read_new(self) -> list[str]:
        try:
            size = self.path.stat().st_size
            if size < self.pos:
                self.pos = 0  # file truncated/rotated
            if size == self.pos:
                return []
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
                self.pos = f.tell()
                return data.decode("utf-8", errors="replace").splitlines()
        except OSError:
            return []


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    watcher = LogWatcher(LOGS_DIR / "daemon.log")
    last_state_t = 0.0
    try:
        while True:
            new_lines = watcher.read_new()
            if new_lines:
                await ws.send_json({"type": "log", "lines": new_lines})

            now = time.time()
            if now - last_state_t >= 5:
                await ws.send_json({"type": "state", "data": build_state()})
                last_state_t = now

            await asyncio.sleep(1)
    except (WebSocketDisconnect, RuntimeError):
        pass


# ---------------------------------------------------------------------------
# HTML — full control panel
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TARS</title>
<style>
:root{--bg:#0a0e14;--bg1:#12161e;--bg2:#1a1f2b;--bg3:#232936;--border:#2a3040;--fg:#e0e4ec;--fg2:#a0a8b8;--fg3:#6b7280;--accent:#6c9cfc;--green:#4ade80;--red:#f87171;--orange:#fbbf24;--purple:#c084fc}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font-family:'Inter',-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px;line-height:1.5;height:100vh;overflow:hidden}
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
button{font-family:inherit;cursor:pointer;border:1px solid var(--border);border-radius:8px;padding:6px 14px;font-size:12px;font-weight:500;color:var(--fg);background:var(--bg2);transition:all .15s}
button:hover{background:var(--bg3);border-color:var(--fg3)}
button.btn-green{background:rgba(74,222,128,.1);color:var(--green);border-color:rgba(74,222,128,.25)}
button.btn-green:hover{background:rgba(74,222,128,.18)}
button.btn-red{background:rgba(248,113,113,.1);color:var(--red);border-color:rgba(248,113,113,.25)}
button.btn-accent{background:rgba(108,156,252,.1);color:var(--accent);border-color:rgba(108,156,252,.25)}
button.btn-accent:hover{background:rgba(108,156,252,.18)}
button:disabled{opacity:.4;cursor:not-allowed;pointer-events:none}
input,select,textarea{font-family:inherit;font-size:13px;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:8px;padding:8px 12px;outline:none;transition:border-color .15s}
input:focus,select:focus,textarea:focus{border-color:var(--accent)}
textarea{resize:vertical;min-height:60px}

/* Layout */
.header{background:var(--bg1);border-bottom:1px solid var(--border);padding:0 24px;display:flex;align-items:center;justify-content:space-between;height:52px;flex-shrink:0}
.header-left{display:flex;align-items:center;gap:16px}
.header-logo{font-size:16px;font-weight:700;letter-spacing:2px;color:var(--fg)}
.status-pill{display:flex;align-items:center;gap:6px;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:500}
.status-pill.on{background:rgba(74,222,128,.1);color:var(--green)}
.status-pill.off{background:rgba(107,114,128,.1);color:var(--fg3)}
.status-pill .dot{width:8px;height:8px;border-radius:50%}
.status-pill.on .dot{background:var(--green);box-shadow:0 0 8px rgba(74,222,128,.5)}
.status-pill.off .dot{background:var(--fg3)}
.header-right{display:flex;gap:10px;align-items:center}
.ws-indicator{font-size:11px;color:var(--fg3);display:flex;align-items:center;gap:4px}
.ws-indicator .dot{width:6px;height:6px;border-radius:50%;background:var(--fg3)}
.ws-indicator.live .dot{background:var(--green)}
.ws-indicator.dead .dot{background:var(--red)}

.pause-banner{background:rgba(251,191,36,.06);border-bottom:1px solid rgba(251,191,36,.15);color:var(--orange);padding:10px 24px;font-size:13px;display:none;align-items:center;gap:8px;flex-shrink:0}

.layout{display:grid;grid-template-columns:240px 1fr;height:calc(100vh - 52px);overflow:hidden}

/* Sidebar */
.sidebar{background:var(--bg1);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden}
.sidebar::-webkit-scrollbar{width:0}
.sb-section{padding:16px 16px 14px}
.sb-section+.sb-section{border-top:1px solid var(--border)}
.sb-title{font-size:10px;text-transform:uppercase;letter-spacing:1.2px;color:var(--fg3);margin-bottom:12px;font-weight:600}
.project-row{display:flex;align-items:center;gap:10px;padding:6px 0}
.toggle{position:relative;width:34px;height:18px;cursor:pointer;flex-shrink:0}
.toggle input{display:none}
.toggle .slider{position:absolute;inset:0;background:var(--bg3);border-radius:10px;transition:.2s}
.toggle .slider:before{content:'';position:absolute;width:14px;height:14px;left:2px;top:2px;background:var(--fg3);border-radius:50%;transition:.2s}
.toggle input:checked+.slider{background:rgba(74,222,128,.2)}
.toggle input:checked+.slider:before{transform:translateX(16px);background:var(--green)}
.project-name{font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:opacity .15s}
.project-name.inactive{opacity:.35}
.project-cb{font-size:10px;margin-left:auto;flex-shrink:0}
.sb-actions{display:flex;gap:6px;margin-top:12px;flex-wrap:wrap}
.sb-actions button{flex:1;min-width:0;text-align:center;padding:7px 6px;font-size:11px}

/* Today stats */
.stats-row{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:12px}
.stat-card{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:10px 8px;text-align:center}
.stat-val{font-size:22px;font-weight:700;line-height:1.1}
.stat-label{font-size:10px;color:var(--fg3);margin-top:2px;font-weight:500;text-transform:uppercase;letter-spacing:.5px}
.stat-val.green{color:var(--green)}.stat-val.red{color:var(--red)}.stat-val.accent{color:var(--accent)}.stat-val.purple{color:var(--purple)}
.budget-bar{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:10px 12px}
.budget-top{display:flex;justify-content:space-between;font-size:11px;color:var(--fg3);margin-bottom:6px;font-weight:500}
.progress-outer{background:var(--bg3);border-radius:4px;height:4px;overflow:hidden}
.progress-inner{height:100%;border-radius:4px;transition:width .4s ease}
.pct-ok{background:var(--green)}.pct-warn{background:var(--orange)}.pct-danger{background:var(--red)}

/* Main area */
.main{display:flex;flex-direction:column;overflow:hidden;background:var(--bg)}

/* Hero */
.hero{padding:36px 40px 24px;flex-shrink:0}
.hero.active .hero-inner{background:linear-gradient(135deg,rgba(108,156,252,.06) 0%,rgba(74,222,128,.04) 100%);border-color:rgba(108,156,252,.15)}
.hero-inner{background:var(--bg1);border:1px solid var(--border);border-radius:16px;padding:28px 32px;transition:all .3s}
.hero-project{font-size:12px;color:var(--accent);font-weight:600;margin-bottom:6px;letter-spacing:.5px}
.hero-title{font-size:20px;font-weight:600;line-height:1.4;margin-bottom:14px;max-width:700px}
.hero-title.idle{color:var(--fg3);font-weight:400;font-size:16px}
.hero-meta{display:flex;gap:14px;align-items:center;font-size:13px;color:var(--fg2)}
.hero-timer{font-family:'SF Mono',Consolas,monospace;font-size:13px;color:var(--fg);background:var(--bg);padding:3px 10px;border-radius:6px;border:1px solid var(--border)}
.src-badge{font-size:11px;padding:3px 10px;border-radius:6px;font-weight:500}
.src-manual{background:rgba(108,156,252,.12);color:var(--accent)}
.src-github{background:rgba(192,132,252,.12);color:var(--purple)}
.src-auto{background:rgba(251,191,36,.12);color:var(--orange)}

/* Pipeline */
.pipeline-wrap{padding:0 40px 24px;flex-shrink:0}
.pipeline{display:flex;align-items:center;justify-content:center;gap:0;padding:4px 0}
.pipeline-step{display:flex;align-items:center;gap:0;white-space:nowrap}
.pipeline-step:not(:last-child)::after{content:'';display:block;width:32px;height:2px;background:var(--bg3);margin:0 6px;flex-shrink:0;border-radius:1px;transition:background .3s}
.pipeline-step:not(:last-child).done::after{background:var(--green)}
.step-inner{display:flex;flex-direction:column;align-items:center;gap:5px}
.step-circle{width:34px;height:34px;border-radius:50%;border:2px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:14px;color:var(--fg3);background:transparent;flex-shrink:0;transition:all .3s}
.step-circle.done{background:var(--green);border-color:var(--green);color:#fff;box-shadow:0 0 12px rgba(74,222,128,.25)}
.step-circle.active{background:var(--accent);border-color:var(--accent);color:#fff;animation:pulse 2s ease-in-out infinite;box-shadow:0 0 16px rgba(108,156,252,.4)}
.step-circle.failed{background:var(--red);border-color:var(--red);color:#fff;box-shadow:0 0 12px rgba(248,113,113,.3)}
.step-circle.patch{background:var(--orange);border-color:var(--orange);color:#fff;animation:pulse 2s ease-in-out infinite;box-shadow:0 0 14px rgba(251,191,36,.4)}
.step-label{font-size:10px;color:var(--fg3);text-transform:uppercase;letter-spacing:.5px;font-weight:500}
.step-label.active-label{color:var(--accent);font-weight:700}
.step-label.done-label{color:var(--green)}
.step-label.failed-label{color:var(--red);font-weight:700}
.pipeline-idle .step-circle{opacity:.25}.pipeline-idle .step-label{opacity:.25}
@keyframes pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.1)}}

/* Bottom panels */
.bottom{display:grid;grid-template-columns:1fr 1fr 1fr;flex:1;overflow:hidden;min-height:0;gap:1px;background:var(--border);border-top:1px solid var(--border)}
.panel{display:flex;flex-direction:column;overflow:hidden;background:var(--bg)}
.panel-header{display:flex;justify-content:space-between;align-items:center;padding:12px 20px;flex-shrink:0}
.panel-header h2{font-size:12px;color:var(--fg2);margin:0;font-weight:600}
.panel-header .count{color:var(--fg3);font-weight:400}
.panel-body{flex:1;overflow-y:auto;padding:0 20px 16px}
.panel-body::-webkit-scrollbar{width:4px}
.panel-body::-webkit-scrollbar-thumb{background:var(--bg3);border-radius:2px}

/* Queue items */
.q-item{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid rgba(42,48,64,.5)}
.q-item:last-child{border-bottom:none}
.q-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.q-dot.manual{background:var(--accent)}.q-dot.github{background:var(--purple)}.q-dot.auto{background:var(--orange)}
.q-title{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
.q-project{color:var(--fg3);font-size:11px;flex-shrink:0}
.q-remove{background:none;border:none;color:var(--fg3);font-size:14px;padding:2px 6px;opacity:0;transition:opacity .15s}
.q-item:hover .q-remove{opacity:1}
.q-remove:hover{color:var(--red)}

/* Completed items */
.done-item{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid rgba(42,48,64,.5);font-size:13px}
.done-item:last-child{border-bottom:none}
.done-icon{flex-shrink:0;font-size:12px}
.done-icon.ok{color:var(--green)}.done-icon.fail{color:var(--red)}
.done-title{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.done-project{color:var(--fg3);font-size:11px;flex-shrink:0}
.done-time{color:var(--fg3);font-size:11px;flex-shrink:0}
.done-pr{font-size:11px;flex-shrink:0;color:var(--accent);text-decoration:none}
.done-pr:hover{text-decoration:underline}

/* Activity feed */
.activity-item{display:flex;align-items:flex-start;gap:10px;padding:6px 0;font-size:13px;line-height:1.5}
.activity-icon{flex-shrink:0;width:20px;text-align:center;font-size:12px;padding-top:2px}
.activity-msg{color:var(--fg2);flex:1;word-break:break-word}
.activity-msg.error{color:var(--red)}.activity-msg.warn{color:var(--orange)}.activity-msg.success{color:var(--green)}
.activity-time{color:var(--fg3);font-size:11px;flex-shrink:0;padding-top:2px}
.raw-toggle{background:none;border:none;color:var(--fg3);font-size:11px;padding:0;cursor:pointer;text-decoration:underline;text-underline-offset:2px}
.raw-toggle:hover{color:var(--fg2);background:none;border:none}
.raw-log{display:none;font-family:'SF Mono',Consolas,monospace;font-size:11px;line-height:1.6;color:var(--fg3);white-space:pre-wrap;word-break:break-all;margin-top:8px;padding:12px;background:var(--bg1);border-radius:8px;max-height:200px;overflow-y:auto}
.raw-log.open{display:block}

.empty{color:var(--fg3);font-size:13px;padding:8px 0}

/* Modals */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.65);display:none;align-items:center;justify-content:center;z-index:100;backdrop-filter:blur(4px)}
.modal-bg.open{display:flex}
.modal{background:var(--bg1);border:1px solid var(--border);border-radius:16px;padding:28px;min-width:420px;max-width:500px;box-shadow:0 24px 48px rgba(0,0,0,.3)}
.modal h3{font-size:17px;font-weight:600;margin-bottom:20px}
.modal .field{margin-bottom:14px}
.modal .field label{display:block;font-size:12px;color:var(--fg2);margin-bottom:5px;font-weight:500}
.modal .field input,.modal .field select,.modal .field textarea{width:100%}
.modal .actions{display:flex;gap:8px;justify-content:flex-end;margin-top:20px}

/* Toast */
.toast{position:fixed;bottom:24px;right:24px;background:var(--bg1);border:1px solid var(--border);border-radius:10px;padding:12px 18px;font-size:13px;z-index:200;animation:slideUp .25s ease;box-shadow:0 8px 24px rgba(0,0,0,.25)}
.toast.ok{border-color:rgba(74,222,128,.3)}.toast.err{border-color:rgba(248,113,113,.3)}
@keyframes slideUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}

/* Responsive */
@media(max-width:900px){
  .layout{grid-template-columns:1fr}
  .sidebar{display:none}
  .bottom{grid-template-columns:1fr!important}
  .hero{padding:20px 16px 14px}
  .hero-inner{padding:20px}
  .hero-title{font-size:17px}
  .pipeline-wrap{padding:0 16px 16px}
  .step-circle{width:26px;height:26px;font-size:11px}
  .pipeline-step:not(:last-child)::after{width:14px}
  .step-label{font-size:8px}
}
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="header-left">
    <span class="header-logo">TARS</span>
    <div id="status-pill" class="status-pill off">
      <span class="dot"></span>
      <span id="status-label">Offline</span>
    </div>
  </div>
  <div class="header-right">
    <button id="btn-daemon" class="btn-green" onclick="toggleDaemon()">Start</button>
    <div id="ws-indicator" class="ws-indicator">
      <span class="dot"></span>
    </div>
  </div>
</div>

<div class="pause-banner" id="pause-banner">
  <strong>Paused</strong> &mdash; Token budget reached. Resuming in <span id="pause-remaining">--</span>
</div>

<div class="layout">
  <!-- Sidebar -->
  <div class="sidebar">
    <div class="sb-section">
      <div class="sb-title">Projects</div>
      <div id="projects-list"></div>
      <div class="sb-actions">
        <button class="btn-accent" onclick="openModal('new-project')">+ Project</button>
        <button class="btn-accent" onclick="openModal('add-task')">+ Task</button>
      </div>
      <div style="margin-top:6px">
        <button id="btn-auto-populate" class="btn-accent" onclick="autoPopulate()" disabled style="display:none;width:100%;text-align:center;font-size:11px;padding:7px 6px">Auto Populate</button>
      </div>
    </div>
    <div class="sb-section">
      <div class="sb-title">Today</div>
      <div class="stats-row">
        <div class="stat-card"><div class="stat-val green" id="s-completed">0</div><div class="stat-label">Done</div></div>
        <div class="stat-card"><div class="stat-val red" id="s-failed">0</div><div class="stat-label">Failed</div></div>
        <div class="stat-card"><div class="stat-val accent" id="s-cost">$0</div><div class="stat-label">Cost</div></div>
        <div class="stat-card"><div class="stat-val purple" id="s-prs">0</div><div class="stat-label">PRs</div></div>
      </div>
      <div class="budget-bar">
        <div class="budget-top"><span>Token budget</span><span id="tok-pct">0%</span></div>
        <div class="progress-outer"><div class="progress-inner pct-ok" id="tok-bar" style="width:0%"></div></div>
      </div>
    </div>
  </div>

  <!-- Main -->
  <div class="main">
    <div class="hero" id="hero">
      <div class="hero-inner">
        <div class="hero-project" id="hero-project"></div>
        <div class="hero-title idle" id="hero-title">Waiting for next task...</div>
        <div class="hero-meta" id="hero-meta"></div>
      </div>
    </div>

    <div class="pipeline-wrap">
      <div id="pipeline"></div>
    </div>

    <div class="bottom">
      <div class="panel">
        <div class="panel-header">
          <h2>Queue <span class="count" id="queue-count"></span></h2>
        </div>
        <div class="panel-body" id="task-queue"><span class="empty">No tasks queued</span></div>
      </div>
      <div class="panel">
        <div class="panel-header">
          <h2>Completed <span class="count" id="done-count"></span></h2>
        </div>
        <div class="panel-body" id="completed-tasks"><span class="empty">No completed tasks today</span></div>
      </div>
      <div class="panel">
        <div class="panel-header">
          <h2>Activity</h2>
          <button class="raw-toggle" onclick="toggleRawLog()">raw log</button>
        </div>
        <div class="panel-body" id="activity-feed">
          <input type="checkbox" id="auto-scroll" checked style="display:none">
          <div id="activity-list"></div>
          <div id="daemon-log" class="raw-log"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Add Task Modal -->
<div class="modal-bg" id="modal-add-task" onclick="if(event.target===this)closeModals()">
  <div class="modal">
    <h3>Add Task</h3>
    <div class="field"><label>Project</label><select id="at-project"></select></div>
    <div class="field"><label>Title</label><input id="at-title" placeholder="What should TARS do?"></div>
    <div class="field"><label>Description</label><textarea id="at-desc" placeholder="More detail (optional)"></textarea></div>
    <div class="field"><label>Priority (1-100)</label><input id="at-priority" type="number" value="50" min="1" max="100"></div>
    <div class="actions">
      <button onclick="closeModals()">Cancel</button>
      <button class="btn-green" onclick="addTask()">Add</button>
    </div>
  </div>
</div>

<!-- New Project Modal -->
<div class="modal-bg" id="modal-new-project" onclick="if(event.target===this)closeModals()">
  <div class="modal">
    <h3>New Project</h3>
    <div class="field"><label>Name</label><input id="np-name" placeholder="my-project"></div>
    <div class="field"><label>Type</label>
      <select id="np-type">
        <option value="python">Python</option><option value="node">Node.js</option>
        <option value="typescript">TypeScript</option><option value="react">React</option>
        <option value="rust">Rust</option><option value="go">Go</option>
        <option value="java">Java</option><option value="swift">Swift</option>
      </select>
    </div>
    <div class="field"><label>Description</label><input id="np-desc" placeholder="What does this project do?"></div>
    <div class="field"><label>Visibility</label>
      <select id="np-vis"><option value="public">Public</option><option value="private">Private</option></select>
    </div>
    <div class="field"><label>Org (optional)</label><input id="np-org" placeholder="github-org"></div>
    <div class="actions">
      <button onclick="closeModals()">Cancel</button>
      <button class="btn-green" onclick="createProject()">Create</button>
    </div>
  </div>
</div>

<script>
let state = {};
let ws = null;
let reconnectTimer = null;
let selectedProject = '';
let autoPopulatePolling = null;
let rawLogVisible = false;

// ---- Helpers ----
function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function fmtTimer(s) {
  if (s == null) return '';
  const m = Math.floor(s / 60), sec = s % 60;
  return m + ':' + (sec < 10 ? '0' : '') + sec;
}

// ---- Activity feed: parse raw log lines into friendly events ----
const EVENT_PATTERNS = [
  { re: /=== Daemon cycle start ===/,     icon: '\u27F3', msg: 'Starting new cycle',     cls: '' },
  { re: /Running Claude implementation/,  icon: '\u270E', msg: 'Writing code...',         cls: '' },
  { re: /Running build:/,                 icon: '\u2692', msg: 'Building project...',     cls: '' },
  { re: /Running tests:/,                 icon: '\u2713', msg: 'Running tests...',        cls: '' },
  { re: /Running self-review/,            icon: '\u2606', msg: 'Self-reviewing...',       cls: '' },
  { re: /Auto-patch attempt\s*(\S*)/,     icon: '\u21BB', msg: 'Auto-patching $1...',     cls: 'warn' },
  { re: /Committing and pushing/,         icon: '\u2191', msg: 'Pushing changes...',      cls: '' },
  { re: /Task complete:\s*(.*)/,          icon: '\u2714', msg: 'Completed: $1',           cls: 'success' },
  { re: /Task failed:\s*(.*)/,            icon: '\u2718', msg: 'Failed: $1',              cls: 'error' },
  { re: /All auto-patch attempts failed/, icon: '\u2718', msg: 'All patches failed',      cls: 'error' },
  { re: /PR created:\s*(.*)/,             icon: '\u2197', msg: 'PR created',              cls: 'success' },
  { re: /Sleeping for/,                   icon: '\u23F8', msg: 'Waiting for next cycle',  cls: '' },
  { re: /ERROR[:\s]+(.*)/i,               icon: '!',      msg: '$1',                      cls: 'error' },
  { re: /WARN[:\s]+(.*)/i,                icon: '!',      msg: '$1',                      cls: 'warn' },
];

function parseLogLine(line) {
  for (const p of EVENT_PATTERNS) {
    const m = line.match(p.re);
    if (m) {
      let msg = p.msg;
      for (let i = 1; i < m.length; i++) msg = msg.replace('$' + i, (m[i]||'').trim());
      // Extract time from log line if present (HH:MM:SS pattern)
      const tm = line.match(/(\d{2}:\d{2}:\d{2})/);
      return { icon: p.icon, msg, cls: p.cls, time: tm ? tm[1] : '' };
    }
  }
  return null;
}

let activityItems = [];

function addActivity(lines) {
  const list = document.getElementById('activity-list');
  if (!list) return;
  for (const line of lines) {
    const evt = parseLogLine(line);
    if (!evt) continue;
    // Deduplicate consecutive identical messages
    if (activityItems.length > 0 && activityItems[activityItems.length-1].msg === evt.msg) continue;
    activityItems.push(evt);
    const div = document.createElement('div');
    div.className = 'activity-item';
    div.innerHTML =
      '<span class="activity-icon">' + evt.icon + '</span>' +
      '<span class="activity-msg ' + evt.cls + '">' + esc(evt.msg) + '</span>' +
      (evt.time ? '<span class="activity-time">' + evt.time + '</span>' : '');
    list.appendChild(div);
  }
  // Keep max 40 items
  while (list.children.length > 40) { list.removeChild(list.firstChild); activityItems.shift(); }
  // Auto-scroll the panel
  const panel = document.getElementById('activity-feed');
  const autoScroll = document.getElementById('auto-scroll');
  if (panel && autoScroll && autoScroll.checked) {
    panel.scrollTop = panel.scrollHeight;
  }
}

// Raw log (hidden by default)
function appendRawLog(lines) {
  const el = document.getElementById('daemon-log');
  if (!el) return;
  for (const line of lines) {
    el.textContent += line + '\n';
  }
  // Trim to ~300 lines
  const allLines = el.textContent.split('\n');
  if (allLines.length > 300) {
    el.textContent = allLines.slice(-200).join('\n');
  }
  if (rawLogVisible) el.scrollTop = el.scrollHeight;
}

function toggleRawLog() {
  const el = document.getElementById('daemon-log');
  rawLogVisible = !rawLogVisible;
  el.classList.toggle('open', rawLogVisible);
  if (rawLogVisible) el.scrollTop = el.scrollHeight;
}

// ---- Toast ----
function toast(msg, ok) {
  const t = document.createElement('div');
  t.className = 'toast ' + (ok ? 'ok' : 'err');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

// ---- API ----
function api(method, url, body) {
  const opts = { method };
  if (body) { opts.headers = {'Content-Type':'application/json'}; opts.body = JSON.stringify(body); }
  return fetch(url, opts).then(r => r.json());
}

// ---- Controls ----
function toggleDaemon() {
  const running = state.daemon && state.daemon.running;
  const btn = document.getElementById('btn-daemon');
  btn.disabled = true;
  api('POST', running ? '/api/daemon/stop' : '/api/daemon/start')
    .then(r => { toast(r.output, r.ok); btn.disabled = false; })
    .catch(() => { btn.disabled = false; });
}

function resetBreaker(project) {
  api('POST', '/api/breaker/reset', {project})
    .then(r => toast(r.output, r.ok));
}

function toggleProject(project) {
  api('POST', '/api/projects/toggle', {project})
    .then(r => { if (!r.ok) toast(r.output, false); });
}

function removeTask(id) {
  api('POST', '/api/queue/remove', {id})
    .then(r => toast(r.output, r.ok));
}

// ---- Modals ----
function openModal(id) {
  document.getElementById('modal-' + id).classList.add('open');
  if (id === 'add-task') populateProjectSelect('at-project');
}
function closeModals() { document.querySelectorAll('.modal-bg').forEach(m => m.classList.remove('open')); }

function populateProjectSelect(elId) {
  const sel = document.getElementById(elId);
  sel.innerHTML = '';
  (state.projects || []).forEach(p => {
    const o = document.createElement('option');
    o.value = p._name || '';
    o.textContent = p._name || 'unknown';
    sel.appendChild(o);
  });
}

function addTask() {
  const project = document.getElementById('at-project').value;
  const title = document.getElementById('at-title').value.trim();
  const description = document.getElementById('at-desc').value.trim();
  const priority = parseInt(document.getElementById('at-priority').value) || 50;
  if (!title) return toast('Title required', false);
  api('POST', '/api/queue/add', {project, title, description, priority})
    .then(r => { toast(r.output, r.ok); if (r.ok) closeModals(); });
}

function createProject() {
  const name = document.getElementById('np-name').value.trim();
  const type = document.getElementById('np-type').value;
  const description = document.getElementById('np-desc').value.trim();
  const visibility = document.getElementById('np-vis').value;
  const org = document.getElementById('np-org').value.trim();
  if (!name) return toast('Name required', false);
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = 'Creating...';
  api('POST', '/api/project/create', {name, type, description, visibility, org})
    .then(r => { toast(r.ok ? 'Project created!' : r.output, r.ok); if (r.ok) closeModals(); })
    .finally(() => { btn.disabled = false; btn.textContent = 'Create'; });
}

// ---- Render: Header ----
function renderHeader(s) {
  const pill = document.getElementById('status-pill');
  const label = document.getElementById('status-label');
  const btn = document.getElementById('btn-daemon');
  if (s.daemon && s.daemon.running) {
    pill.className = 'status-pill on';
    label.textContent = 'Running';
    btn.textContent = 'Stop'; btn.className = 'btn-red';
  } else {
    pill.className = 'status-pill off';
    label.textContent = 'Offline';
    btn.textContent = 'Start'; btn.className = 'btn-green';
  }
  const pb = document.getElementById('pause-banner');
  if (s.daemon_status && s.daemon_status.paused) {
    pb.style.display = 'flex';
    document.getElementById('pause-remaining').textContent = Math.ceil((s.daemon_status.remaining_s || 0) / 60) + ' min';
  } else {
    pb.style.display = 'none';
  }
}

// ---- Render: Sidebar ----
function renderSidebar(s) {
  const activeProjects = s.active_projects || [];
  const pl = document.getElementById('projects-list');
  if (s.projects && s.projects.length) {
    pl.innerHTML = s.projects.map(p => {
      const name = p._name || 'unknown';
      const isActive = activeProjects.includes(name);
      const cb = (s.circuit_breakers || {})[name] || {};
      let extra = '';
      if (cb.is_locked) {
        extra = '<span class="project-cb" style="color:var(--red)">Locked ' + Math.ceil(cb.remaining_lockout/60) + 'm ' +
          '<button style="font-size:9px;padding:1px 5px;border-radius:4px" onclick="resetBreaker(\'' + esc(name) + '\')">reset</button></span>';
      } else if ((cb.consecutive_failures||0) > 0) {
        extra = '<span class="project-cb" style="color:var(--orange)">' + cb.consecutive_failures + ' fail</span>';
      }
      return '<div class="project-row">' +
        '<label class="toggle"><input type="checkbox" ' + (isActive ? 'checked' : '') + ' onchange="toggleProject(\'' + esc(name) + '\')"><span class="slider"></span></label>' +
        '<span class="project-name' + (isActive ? '' : ' inactive') + '">' + esc(name) + '</span>' + extra + '</div>';
    }).join('');
  } else {
    pl.innerHTML = '<span class="empty">No projects</span>';
  }
  const apBtn = document.getElementById('btn-auto-populate');
  if (activeProjects.length > 0) {
    selectedProject = (s.current_task && s.current_task.project) || activeProjects[0];
    apBtn.style.display = 'block';
    apBtn.disabled = false;
  } else {
    apBtn.style.display = 'none';
  }
  const m = s.metrics || {};
  const u = (s.tokens && s.tokens.usage) || {};
  document.getElementById('s-completed').textContent = m.completed || 0;
  document.getElementById('s-failed').textContent = m.failed || 0;
  document.getElementById('s-cost').textContent = '$' + (u.total_cost || 0).toFixed(2);
  document.getElementById('s-prs').textContent = m.prs_created || 0;
  const pct = s.tokens ? s.tokens.pct : 0;
  document.getElementById('tok-pct').textContent = Math.round(pct) + '%';
  const bar = document.getElementById('tok-bar');
  bar.style.width = Math.min(pct, 100) + '%';
  bar.className = 'progress-inner ' + (pct > 90 ? 'pct-danger' : pct > 70 ? 'pct-warn' : 'pct-ok');
}

// ---- Render: Hero ----
function renderHero(s) {
  const hero = document.getElementById('hero');
  const proj = document.getElementById('hero-project');
  const title = document.getElementById('hero-title');
  const meta = document.getElementById('hero-meta');
  if (s.current_task && s.current_task.title) {
    hero.className = 'hero active';
    proj.textContent = s.current_task.project || '';
    title.textContent = s.current_task.title;
    title.className = 'hero-title';
    let h = '';
    if (s.current_task.elapsed_s != null) h += '<span class="hero-timer">' + fmtTimer(s.current_task.elapsed_s) + '</span>';
    if (s.current_task.source) h += '<span class="src-badge src-' + s.current_task.source + '">' + esc(s.current_task.source) + '</span>';
    meta.innerHTML = h;
  } else {
    hero.className = 'hero';
    proj.textContent = '';
    title.textContent = 'Waiting for next task...';
    title.className = 'hero-title idle';
    meta.innerHTML = '';
  }
}

// ---- Render: Pipeline ----
function renderPipeline(pipeline) {
  const el = document.getElementById('pipeline');
  const stages = ['discover','implement','build','test','review','push','done'];
  const icons = {discover:'\uD83D\uDD0D',implement:'\uD83D\uDCBB',build:'\uD83D\uDD27',test:'\uD83E\uDDEA',review:'\uD83D\uDC41',push:'\uD83D\uDE80',done:'\u2714'};
  const labels = {discover:'Discover',implement:'Code',build:'Build',test:'Test',review:'Review',push:'Push',done:'Done'};
  const hasStage = !!(pipeline && pipeline.stage);
  const completed = (pipeline && pipeline.completed) || [];
  const active = (pipeline && pipeline.stage) || null;
  const isPatch = active === 'patch';
  const isFailed = active === 'failed';
  const isIdle = !hasStage;

  let html = '<div class="pipeline' + (isIdle ? ' pipeline-idle' : '') + '">';
  for (const s of stages) {
    const isDone = completed.includes(s);
    const isActiveStep = (s === active) || (s === 'test' && isPatch);
    let cc = 'step-circle', lc = 'step-label';
    let icon = icons[s], label = labels[s];
    if (s === 'test' && isPatch) { cc += ' patch'; lc += ' active-label'; icon = '\u21BB'; label = 'Patch' + (pipeline.detail ? ' ' + esc(pipeline.detail) : ''); }
    else if (isFailed && isDone) { cc += ' done'; lc += ' done-label'; icon = '\u2713'; }
    else if (isFailed && !isDone && s === stages[completed.length]) { cc += ' failed'; lc += ' failed-label'; icon = '\u2717'; }
    else if (isDone) { cc += ' done'; lc += ' done-label'; icon = '\u2713'; }
    else if (isActiveStep && !isIdle) { cc += ' active'; lc += ' active-label'; }
    html += '<div class="pipeline-step' + (isDone ? ' done' : '') + '"><div class="step-inner"><div class="' + cc + '">' + icon + '</div><span class="' + lc + '">' + label + '</span></div></div>';
  }
  html += '</div>';
  el.innerHTML = html;
}

// ---- Render: Queue ----
function renderQueue(queue) {
  const qq = document.getElementById('task-queue');
  const countEl = document.getElementById('queue-count');
  const items = queue || [];
  countEl.textContent = items.length ? items.length : '';
  if (items.length) {
    qq.innerHTML = items.map(t =>
      '<div class="q-item">' +
      '<span class="q-dot ' + (t.source||'manual') + '"></span>' +
      '<span class="q-title">' + esc(t.title||'Untitled') + '</span>' +
      '<span class="q-project">' + esc(t.project||'') + '</span>' +
      '<button class="q-remove" onclick="removeTask(\'' + esc(t.id||'') + '\')">&times;</button>' +
      '</div>'
    ).join('');
  } else {
    qq.innerHTML = '<span class="empty">No tasks queued</span>';
  }
}

// ---- Render: Completed ----
function renderCompleted(completed) {
  const el = document.getElementById('completed-tasks');
  const countEl = document.getElementById('done-count');
  const items = completed || [];
  countEl.textContent = items.length ? items.length : '';
  if (items.length) {
    el.innerHTML = items.slice().reverse().map(t => {
      const ok = t.status === 'success';
      let time = '';
      try { time = new Date(t.timestamp).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}); } catch(e) {}
      return '<div class="done-item">' +
        '<span class="done-icon ' + (ok ? 'ok' : 'fail') + '">' + (ok ? '\u2714' : '\u2718') + '</span>' +
        '<span class="done-title">' + esc(t.title||'') + '</span>' +
        '<span class="done-project">' + esc(t.project||'') + '</span>' +
        (t.pr_url && t.pr_url !== 'direct-push' ? '<a class="done-pr" href="' + esc(t.pr_url) + '" target="_blank">PR</a>' : '') +
        '<span class="done-time">' + time + '</span>' +
        '</div>';
    }).join('');
  } else {
    el.innerHTML = '<span class="empty">No completed tasks today</span>';
  }
}

// ---- Main render ----
function renderState(s) {
  state = s;
  renderHeader(s);
  renderSidebar(s);
  renderHero(s);
  renderPipeline(s.pipeline);
  renderQueue(s.queue || []);
  renderCompleted(s.completed || []);
}

// ---- WebSocket ----
function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws');
  const ind = document.getElementById('ws-indicator');
  ind.className = 'ws-indicator';
  ws.onopen = () => { ind.className = 'ws-indicator live'; if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; } };
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'state') renderState(msg.data);
      if (msg.type === 'log') { addActivity(msg.lines); appendRawLog(msg.lines); }
    } catch(e) {}
  };
  ws.onclose = () => { ind.className = 'ws-indicator dead'; reconnectTimer = setTimeout(connect, 3000); };
  ws.onerror = () => ws.close();
}

// ---- Auto Populate ----
function autoPopulate() {
  if (!selectedProject) return;
  const btn = document.getElementById('btn-auto-populate');
  btn.disabled = true;
  btn.textContent = 'Discovering...';
  api('POST', '/api/tasks/auto-populate', {project: selectedProject})
    .then(r => {
      if (!r.ok) { toast(r.error || 'Failed', false); btn.disabled = false; btn.textContent = 'Auto Populate'; return; }
      autoPopulatePolling = setInterval(pollAutoPopulate, 2000);
    })
    .catch(() => { btn.disabled = false; btn.textContent = 'Auto Populate'; });
}

function pollAutoPopulate() {
  api('GET', '/api/tasks/auto-populate/status')
    .then(r => {
      if (r.running) return;
      clearInterval(autoPopulatePolling); autoPopulatePolling = null;
      const btn = document.getElementById('btn-auto-populate');
      btn.textContent = 'Auto Populate'; btn.disabled = false;
      if (r.error) { toast('Discovery failed: ' + r.error, false); return; }
      toast('Found ' + (r.tasks||[]).length + ' tasks for ' + r.project, true);
    });
}

// ---- Init ----
fetch('/api/state').then(r=>r.json()).then(renderState).catch(()=>{});
fetch('/api/logs/daemon?lines=60').then(r=>r.json()).then(d=>{ if(d.lines) { addActivity(d.lines); appendRawLog(d.lines); } }).catch(()=>{});
connect();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return DASHBOARD_HTML


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="TARS Dashboard")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("TARS_DASHBOARD_PORT", "8420")))
    args = parser.parse_args()
    print(f"TARS Dashboard: http://localhost:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
