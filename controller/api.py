"""
TARS Controller API

Lightweight Flask API running on the brain node that receives
task commands from the Railway-hosted Django website (tarsai.dev).

Reads/writes TARS state files (JSON) and queue config (YAML).
Auth via X-API-Key header, key read from TARS_API_KEY env var.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import yaml
from flask import Flask, abort, g, jsonify, render_template_string, request
from flask_cors import CORS

logger = logging.getLogger("tars.controller")

# Make lib/ importable (controller runs from controller/, lib is a sibling).
import hmac
import re
import sys

_TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).resolve().parent.parent))
if str(_TARS_HOME) not in sys.path:
    sys.path.insert(0, str(_TARS_HOME))
from lib import api_keys  # noqa: E402


def _notify_django(survey_task_id, status: str, **extra) -> None:
    """Best-effort callback to the Django website to update a task's status.

    Requires TARS_WEBSITE_URL and TARS_API_KEY in the environment.  Fails
    silently on any network or auth error — task flow must not be blocked.
    """
    if not survey_task_id:
        return
    website_url = os.environ.get("TARS_WEBSITE_URL", "").rstrip("/")
    api_key = os.environ.get("TARS_API_KEY", "")
    if not website_url or not api_key:
        return
    payload = {"status": status}
    for k, v in extra.items():
        if v is not None:
            payload[k] = v
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{website_url}/api/tasks/{survey_task_id}/status",
        data=body,
        method="POST",
        headers={
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.warning("Failed to notify Django for task %s: %s", survey_task_id, e)

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


def _identify(provided: str):
    """Resolve a presented key to an identity dict, or None.

    The master TARS_API_KEY is always admin. Otherwise look it up in the
    per-user key store. Both comparisons are constant-time.
    """
    if not provided:
        return None
    if API_KEY and hmac.compare_digest(provided, API_KEY):
        return {"user": "admin", "is_admin": True, "id": "master", "scopes": ["*"]}
    rec = api_keys.lookup(provided)
    if rec:
        return {
            "user": rec.get("user", "unknown"),
            "is_admin": bool(rec.get("is_admin")),
            "id": rec.get("id"),
            "scopes": rec.get("scopes", []),
            "projects": rec.get("projects", []),
        }
    return None


def require_api_key(fn):
    """Decorator: require any valid key (master or per-user). Sets g.identity."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not API_KEY:
            # If no master key is configured, reject everything so we never run open.
            return jsonify({"error": "API key not configured on server"}), 500
        identity = _identify(request.headers.get("X-API-Key", ""))
        if not identity:
            return jsonify({"error": "Unauthorized"}), 401
        g.identity = identity
        return fn(*args, **kwargs)

    return wrapper


def require_admin(fn):
    """Decorator: require an admin key (master or a key flagged is_admin)."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not API_KEY:
            return jsonify({"error": "API key not configured on server"}), 500
        identity = _identify(request.headers.get("X-API-Key", ""))
        if not identity:
            return jsonify({"error": "Unauthorized"}), 401
        if not identity.get("is_admin"):
            return jsonify({"error": "Admin access required"}), 403
        g.identity = identity
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


def _ensure_project_config(project: str) -> str:
    """
    Resolve an incoming project identifier (short name or owner/repo) to an
    enabled TARS project config. If no matching config exists, create one with
    sensible defaults so newly-added website projects are immediately runnable.

    Returns the short name the scheduler will match against.
    """
    projects_dir = CONFIG_DIR / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    for path in projects_dir.glob("*.yaml"):
        try:
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
        except yaml.YAMLError:
            continue
        if cfg.get("repo") == project or path.stem == project:
            return path.stem

    short_name = project.split("/", 1)[1] if "/" in project else project
    repo = project if "/" in project else ""
    default_cfg = {
        "repo": repo,
        "description": f"Auto-created from website task for {project}",
        "type": "generic",
        "enabled": True,
        "git": {"strategy": "branch-pr", "base_branch": "main", "pr_labels": []},
        "build": {"type": "generic", "command": None},
        "test": {"command": None},
        "issues": {"enabled": False, "labels": ["tars"]},
        "auto_discover": {"enabled": False, "interval": 86400, "focus_areas": []},
        "claude": {"model": "sonnet", "max_turns": 20},
    }
    new_path = projects_dir / f"{short_name}.yaml"
    with open(new_path, "w") as f:
        yaml.dump(default_cfg, f, default_flow_style=False, sort_keys=False)
    return short_name


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


@app.route("/api/whoami", methods=["GET"])
@require_api_key
def whoami():
    """Return the identity for the presented key (used by the dashboard)."""
    return jsonify(g.identity)


# ---------------------------------------------------------------------------
# Routes — Admin: API key management (admin-only)
# ---------------------------------------------------------------------------


@app.route("/api/admin/keys", methods=["GET"])
@require_admin
def admin_list_keys():
    """List all issued keys (hashes never returned)."""
    return jsonify({"keys": api_keys.list_keys()})


@app.route("/api/admin/keys", methods=["POST"])
@require_admin
def admin_create_key():
    """Generate a new per-user key. Body: {user, scopes?, is_admin?}.
    The plaintext key is returned ONCE here and never again."""
    data = request.get_json(silent=True) or {}
    user = (data.get("user") or "").strip()
    if not user:
        return jsonify({"error": "Missing required field: user"}), 400
    scopes = data.get("scopes")
    if scopes is not None and not isinstance(scopes, list):
        return jsonify({"error": "scopes must be a list"}), 400
    rec = api_keys.generate_key(user, scopes=scopes, is_admin=bool(data.get("is_admin")))
    logger.info("Issued API key %s for user %s", rec["id"], user)
    return jsonify(rec), 201


@app.route("/api/admin/keys/<key_id>/revoke", methods=["POST"])
@require_admin
def admin_revoke_key(key_id):
    """Revoke a key by id."""
    if api_keys.revoke_key(key_id):
        logger.info("Revoked API key %s", key_id)
        return jsonify({"ok": True, "revoked": key_id})
    return jsonify({"error": "Key not found"}), 404


# ---------------------------------------------------------------------------
# Routes — Projects (onboarding, settings, fresh creation)
# ---------------------------------------------------------------------------

PROJECTS_DIR = CONFIG_DIR / "projects"
QUEUES_DIR = CONFIG_DIR / "queues"


def _parse_repo(raw: str):
    """Parse a GitHub URL or owner/repo into (owner/repo, short_name) or None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.search(r"github\.com[:/]+([\w.-]+)/([\w.-]+)", raw)
    if m:
        owner, name = m.group(1), m.group(2)
    elif re.match(r"^[\w.-]+/[\w.-]+$", raw):
        owner, name = raw.split("/", 1)
    else:
        return None
    name = re.sub(r"\.git$", "", name)
    short = re.sub(r"[^\w.-]", "-", name)
    return (f"{owner}/{name}", short)


def _write_project_config(short: str, repo: str, opts: dict, owner_key_id=None) -> str:
    """Write a project YAML with sensible defaults. Returns the short name."""
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    auto_merge = bool(opts.get("auto_merge"))
    cfg = {
        "repo": repo,
        "description": opts.get("description", ""),
        "type": "generic",
        "enabled": True,
        "git": {
            "strategy": "direct-main" if auto_merge else "branch-pr",
            "base_branch": opts.get("base_branch", "main"),
            "pr_labels": ["tars-auto"],
            "auto_merge": auto_merge,
        },
        "build": {"type": "generic", "command": opts.get("build") or None},
        "test": {"command": opts.get("test") or None},
        "issues": {"enabled": False, "labels": ["tars"]},
        "auto_discover": {"enabled": False, "interval": 86400, "focus_areas": []},
        "claude": {"model": "sonnet", "max_turns": 20},
    }
    path = PROJECTS_DIR / f"{short}.yaml"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
    tmp.rename(path)
    if owner_key_id and owner_key_id != "master":
        api_keys.add_project(owner_key_id, short)
    return short


def _owns_project(identity: dict, name: str) -> bool:
    return bool(identity.get("is_admin")) or name in (identity.get("projects") or [])


def _write_queue_tasks(project: str, tasks: list, append: bool = True) -> None:
    QUEUES_DIR.mkdir(parents=True, exist_ok=True)
    path = QUEUES_DIR / f"{project}.yaml"
    existing = []
    if append:
        try:
            with open(path) as f:
                existing = (yaml.safe_load(f) or {}).get("tasks", []) or []
        except (FileNotFoundError, yaml.YAMLError):
            existing = []
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        yaml.dump({"tasks": existing + tasks}, f, default_flow_style=False, sort_keys=False)
    tmp.rename(path)


@app.route("/api/projects", methods=["GET"])
@require_api_key
def list_projects_route():
    """List projects visible to the caller (admin = all, user = their own)."""
    ident = g.identity
    out = []
    if PROJECTS_DIR.exists():
        for p in sorted(PROJECTS_DIR.glob("*.yaml")):
            try:
                with open(p) as f:
                    cfg = yaml.safe_load(f) or {}
            except yaml.YAMLError:
                continue
            if not _owns_project(ident, p.stem):
                continue
            out.append({
                "name": p.stem,
                "repo": cfg.get("repo", ""),
                "enabled": cfg.get("enabled", True),
                "auto_merge": cfg.get("git", {}).get("auto_merge", False),
                "build": cfg.get("build", {}).get("command"),
                "test": cfg.get("test", {}).get("command"),
            })
    return jsonify({"projects": out})


@app.route("/api/projects", methods=["POST"])
@require_api_key
def add_project_route():
    """Onboard an existing public repo. Body: {repo_url|repo, build?, test?, auto_merge?}."""
    data = request.get_json(silent=True) or {}
    parsed = _parse_repo(data.get("repo_url") or data.get("repo"))
    if not parsed:
        return jsonify({"error": "Provide a valid GitHub repo URL or owner/repo"}), 400
    repo, short = parsed
    if (PROJECTS_DIR / f"{short}.yaml").exists():
        return jsonify({"error": f"Project '{short}' already exists"}), 409
    _write_project_config(short, repo, data, owner_key_id=g.identity.get("id"))
    logger.info("Onboarded %s (%s) by %s", short, repo, g.identity.get("user"))
    return jsonify({"ok": True, "name": short, "repo": repo}), 201


@app.route("/api/projects/<name>/discover", methods=["POST"])
@require_api_key
def discover_tasks_route(name):
    """Auto-populate: analyze the repo and return suggested improvement tasks
    (does NOT queue them — caller persists). Clones the repo + runs the model."""
    if not _owns_project(g.identity, name):
        return jsonify({"error": "No access to that project"}), 403
    if not (PROJECTS_DIR / f"{name}.yaml").exists():
        return jsonify({"error": "Project not found"}), 404
    try:
        from lib.task_manager import TaskManager
        tasks = TaskManager().get_auto_discovered_tasks(name, force=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("discover failed for %s: %s", name, e)
        return jsonify({"error": f"Discovery failed: {e}"}), 502
    return jsonify({"ok": True, "tasks": [
        {"title": t.get("title", ""), "description": t.get("description", "")}
        for t in tasks
    ]})


@app.route("/api/projects/<name>/settings", methods=["POST"])
@require_api_key
def project_settings_route(name):
    """Update per-project settings (auto_merge toggle, build/test cmds, enabled)."""
    if not _owns_project(g.identity, name):
        return jsonify({"error": "No access to that project"}), 403
    path = PROJECTS_DIR / f"{name}.yaml"
    if not path.exists():
        return jsonify({"error": "Project not found"}), 404
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    data = request.get_json(silent=True) or {}
    if "auto_merge" in data:
        am = bool(data["auto_merge"])
        cfg.setdefault("git", {})["auto_merge"] = am
        cfg["git"]["strategy"] = "direct-main" if am else "branch-pr"
    if "enabled" in data:
        cfg["enabled"] = bool(data["enabled"])
    if "build" in data:
        cfg.setdefault("build", {})["command"] = data["build"] or None
    if "test" in data:
        cfg.setdefault("test", {})["command"] = data["test"] or None
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
    tmp.rename(path)
    return jsonify({"ok": True, "name": name, "auto_merge": cfg.get("git", {}).get("auto_merge", False)})


@app.route("/api/projects/fresh", methods=["POST"])
@require_api_key
def fresh_project_route():
    """Create a NEW repo and generate a task list from design docs.
    Body: {name, design_docs, visibility?, build?, test?, auto_merge?}."""
    data = request.get_json(silent=True) or {}
    name = re.sub(r"[^\w.-]", "-", (data.get("name") or "").strip())
    docs = (data.get("design_docs") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if (PROJECTS_DIR / f"{name}.yaml").exists():
        return jsonify({"error": f"Project '{name}' already exists"}), 409
    try:
        from lib.git_manager import create_repo, GitError
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"git module unavailable: {e}"}), 500
    try:
        repo = create_repo(name, description=data.get("description", ""),
                           visibility=data.get("visibility", "private"))
    except GitError as e:
        return jsonify({"error": f"Repo creation failed: {e}"}), 502
    _write_project_config(name, repo, data, owner_key_id=g.identity.get("id"))
    # Design docs are optional — only generate a task list if they were given.
    tasks = _tasks_from_docs(name, docs, g.identity.get("user")) if docs else []
    # auto_queue defaults True (controller's own SPA). The website passes false
    # and persists tasks itself (as Django Tasks) so it owns the status link.
    if tasks and data.get("auto_queue", True):
        _write_queue_tasks(name, tasks, append=False)
    logger.info("Fresh project %s (%s): %d tasks", name, repo, len(tasks))
    return jsonify({"ok": True, "name": name, "repo": repo,
                    "tasks": [{"title": t["title"], "description": t.get("description", "")}
                              for t in tasks]}), 201


def _tasks_from_docs(project: str, docs: str, user: str) -> list:
    """Use the planning model to turn design docs into a task list."""
    try:
        from lib.ollama_runner import OllamaRunner
    except Exception:  # noqa: BLE001
        return []
    prompt = (
        "Break the following project design document into a concise, ordered list "
        "of concrete implementation tasks. Respond ONLY with a JSON array of "
        '{"title": "...", "description": "..."} objects, most important first.\n\n'
        "DESIGN DOCUMENT:\n" + docs
    )
    try:
        res = OllamaRunner().run(prompt, role="plan", max_turns=1)
    except Exception as e:  # noqa: BLE001
        logger.warning("Task generation failed: %s", e)
        return []
    text = res.get("result", "") or ""
    m = re.search(r"\[.*\]", text, re.DOTALL)
    items = []
    if m:
        try:
            items = json.loads(m.group(0))
        except json.JSONDecodeError:
            items = []
    tasks = []
    for i, it in enumerate(items[:25]):
        if isinstance(it, dict) and it.get("title"):
            tasks.append({
                "id": "fresh-" + uuid.uuid4().hex[:8],
                "title": str(it["title"])[:120],
                "description": str(it.get("description", "")),
                "project": project,
                "priority": max(1, 100 - i),
                "status": "pending",
                "source": "design-docs",
                "user": user,
            })
    return tasks


# ---------------------------------------------------------------------------
# Routes — Chat (talk to a model OR queue a task)
# ---------------------------------------------------------------------------

CHAT_DIR = STATE_DIR / "chat"


def _chat_path(cid: str) -> Path:
    return CHAT_DIR / (re.sub(r"[^\w-]", "", cid)[:40] + ".json")


def _enforce_single_model(keep: str) -> None:
    """Website policy: only ONE model spun up at a time. Unload everything except
    `keep` so chat usage can't pile models into memory."""
    if not keep:
        return
    try:
        c = _ollama_client()
        for m in c.loaded_models():
            if m and m != keep:
                try:
                    c.unload_model(m)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass


# Models that chat with NO system framing at all — fully open, not a "coding
# agent", no forced TARS identity (e.g. the uncensored model for explicit chat).
# Override with OLLAMA_FREEFORM_MODELS (comma-separated tags or base names).
FREEFORM_MODELS = {
    m.strip() for m in os.environ.get(
        "OLLAMA_FREEFORM_MODELS", "dolphin-mistral,dolphin-mixtral,dolphin-llama3,dolphin3"
    ).split(",")
    if m.strip()
}


def _is_freeform(model: str) -> bool:
    if not model:
        return False
    free_bases = {f.split(":")[0] for f in FREEFORM_MODELS}
    return model in FREEFORM_MODELS or model.split(":")[0] in free_bases


def _chat_reply(message: str, history: list, model: str = None) -> str:
    """Provider-aware single reply (delegates to Ollama or Claude via ChatEngine).
    An explicit Ollama model tag overrides the default chat model.

    Freeform models get no system prompt (open chat); other chat models get a
    light, NON-coding assistant persona — none are framed as coding agents."""
    # One model at a time: free other models before this reply loads its own.
    effective = model or os.environ.get("OLLAMA_CHAT_MODEL", "qwen2.5:7b")
    _enforce_single_model(effective)
    freeform = _is_freeform(effective)
    num_predict = int(os.environ.get("OLLAMA_CHAT_NUM_PREDICT", "768"))

    if os.environ.get("TARS_LLM_PROVIDER", "").lower() == "ollama":
        # Proper role-based messages → the model produces ONE assistant turn and
        # stops, instead of continuing the whole dialogue (writing both sides).
        from lib.ollama_runner import OllamaRunner
        persona = os.environ.get("TARS_PERSONA", "You are TARS, an autonomous coding AI developed by Davis Sneed.")
        system = None if freeform else persona + " Be helpful and concise; answer directly."
        msgs = [{"role": m.get("role", "user"), "content": m.get("content", "")}
                for m in history[-20:]]
        msgs.append({"role": "user", "content": message})
        res = OllamaRunner(model=effective, max_turns=1).chat_messages(
            msgs, model=effective, system=system, num_predict=num_predict
        )
        if res.get("is_error"):
            raise RuntimeError(res.get("result", "chat failed"))
        return res.get("result", "")

    # Fallback (claude provider): single text prompt via ChatEngine.
    from lib.chat_engine import ChatEngine
    eng = ChatEngine(model=model) if model else ChatEngine()
    parts = ["<system>", "You are TARS, a helpful, concise assistant.", "</system>"]
    for m in history[-20:]:
        who = "User" if m.get("role") == "user" else "TARS"
        parts.append(f"[{who}]: {m.get('content', '')}")
    parts.append(f"[User]: {message}")
    return eng._run_claude("\n".join(parts))


@app.route("/api/chat", methods=["POST"])
@require_api_key
def chat_route():
    """Chat with a model (mode=chat) or queue a task (mode=task).
    Body: {message, mode?, conversation_id?, project?}."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    if data.get("mode") == "task":
        project = (data.get("project") or "").strip()
        if not project:
            return jsonify({"error": "Select a project to queue a task"}), 400
        if not _owns_project(g.identity, project):
            return jsonify({"error": "No access to that project"}), 403
        task = {
            "id": "chat-" + uuid.uuid4().hex[:8],
            "title": message.split("\n")[0][:80],
            "description": message,
            "project": project,
            "priority": 50,
            "status": "pending",
            "source": "chat",
            "user": g.identity.get("user"),
        }
        _write_queue_tasks(project, [task], append=True)
        logger.info("Queued task %s on %s via chat", task["id"], project)
        return jsonify({"type": "task", "task_id": task["id"],
                        "project": project, "title": task["title"]})

    # Chat mode
    cid = data.get("conversation_id") or ("c-" + uuid.uuid4().hex[:10])
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    conv = _read_json(_chat_path(cid), {"messages": []})
    history = conv.get("messages", [])
    try:
        reply = _chat_reply(message, history, (data.get("model") or "").strip() or None)
    except Exception as e:  # noqa: BLE001
        logger.warning("Chat failed: %s", e)
        return jsonify({"error": f"Chat failed: {e}"}), 500
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": reply})
    conv["messages"] = history[-40:]
    conv["updated"] = int(time.time())
    conv["user"] = g.identity.get("user")
    _write_json(_chat_path(cid), conv)
    return jsonify({"type": "chat", "conversation_id": cid, "reply": reply})


@app.route("/api/chat/generate", methods=["POST"])
@require_api_key
def chat_generate_route():
    """Stateless chat completion. Body: {messages:[{role,content}], model?}.
    For clients (e.g. the tars-survey site) that own conversation history per
    user themselves — the controller just runs the model and returns a reply."""
    data = request.get_json(silent=True) or {}
    messages = data.get("messages") or []
    if not messages:
        return jsonify({"error": "messages is required"}), 400
    last = messages[-1]
    history = messages[:-1]
    try:
        reply = _chat_reply(last.get("content", ""), history,
                            (data.get("model") or "").strip() or None)
    except Exception as e:  # noqa: BLE001
        logger.warning("chat/generate failed: %s", e)
        return jsonify({"error": f"Chat failed: {e}"}), 500
    return jsonify({"reply": reply})


# ---------------------------------------------------------------------------
# Routes — Conversations (list / fetch / clear)
# ---------------------------------------------------------------------------


def _owns_chat(identity: dict, conv: dict) -> bool:
    return bool(identity.get("is_admin")) or conv.get("user") == identity.get("user")


@app.route("/api/chats", methods=["GET"])
@require_api_key
def list_chats_route():
    """List the caller's conversations (admin sees all)."""
    out = []
    if CHAT_DIR.exists():
        for p in sorted(CHAT_DIR.glob("*.json")):
            conv = _read_json(p, {})
            if not _owns_chat(g.identity, conv):
                continue
            msgs = conv.get("messages", [])
            out.append({
                "id": p.stem,
                "updated": conv.get("updated"),
                "count": len(msgs),
                "preview": (msgs[0]["content"][:60] if msgs else ""),
            })
    out.sort(key=lambda c: c.get("updated") or 0, reverse=True)
    return jsonify({"chats": out})


@app.route("/api/chats/<cid>", methods=["GET"])
@require_api_key
def get_chat_route(cid):
    """Fetch a single conversation's messages."""
    conv = _read_json(_chat_path(cid), {})
    if not conv or not _owns_chat(g.identity, conv):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"id": cid, "messages": conv.get("messages", []),
                    "updated": conv.get("updated")})


@app.route("/api/chats/<cid>", methods=["DELETE"])
@require_api_key
def clear_chat_route(cid):
    """Delete (clear) a conversation."""
    path = _chat_path(cid)
    conv = _read_json(path, {})
    if conv and not _owns_chat(g.identity, conv):
        return jsonify({"error": "No access"}), 403
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return jsonify({"ok": True, "cleared": cid})


# ---------------------------------------------------------------------------
# Routes — Models (spin up / spin down for chat)
# ---------------------------------------------------------------------------


def _ollama_client():
    from lib.ollama_client import OllamaClient
    return OllamaClient()


@app.route("/api/models", methods=["GET"])
@require_api_key
def list_models_route():
    """List available local models and which are currently loaded in memory."""
    try:
        c = _ollama_client()
        loaded = {m.get("name"): m for m in c.ps()}
        avail = c.list_models()
    except Exception as e:  # noqa: BLE001 — Ollama may be down; report gracefully
        return jsonify({"models": [], "loaded": [], "error": str(e)})
    return jsonify({
        "models": [{"name": m, "loaded": m in loaded} for m in avail],
        "loaded": list(loaded.keys()),
    })


@app.route("/api/models/load", methods=["POST"])
@require_api_key
def load_model_route():
    """Spin a model UP (load into memory). Enforces ONE model loaded at a time:
    any other spun-up model is unloaded first so memory can't pile up."""
    model = ((request.get_json(silent=True) or {}).get("model") or "").strip()
    if not model:
        return jsonify({"error": "model is required"}), 400
    try:
        client = _ollama_client()
        evicted = []
        for m in client.loaded_models():
            if m and m != model:
                try:
                    client.unload_model(m)
                    evicted.append(m)
                except Exception:  # noqa: BLE001
                    pass
        client.load_model(model)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502
    logger.info("Loaded model %s (evicted %s) for %s", model, evicted, g.identity.get("user"))
    return jsonify({"ok": True, "loaded": model, "evicted": evicted})


@app.route("/api/models/unload", methods=["POST"])
@require_api_key
def unload_model_route():
    """Spin a model DOWN (unload from memory to free VRAM)."""
    model = ((request.get_json(silent=True) or {}).get("model") or "").strip()
    if not model:
        return jsonify({"error": "model is required"}), 400
    try:
        _ollama_client().unload_model(model)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502
    logger.info("Unloaded model %s (requested by %s)", model, g.identity.get("user"))
    return jsonify({"ok": True, "unloaded": model})


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
    project = _ensure_project_config(data["project"])
    survey_task_id = data.get("survey_task_id")

    task = {
        "id": task_id,
        "title": title,
        "description": data["description"],
        "project": project,
        "task_type": data["task_type"],
        "priority": priority,
        "status": "pending",
        "user_id": data.get("user_id"),
        "survey_task_id": survey_task_id,
        "source": "website",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Append to queue.yaml
    queue = _read_queue()
    queue["tasks"].append(task)

    # Keep tasks sorted by priority descending so TARS picks highest first.
    queue["tasks"].sort(key=lambda t: t.get("priority", 0), reverse=True)

    _write_queue(queue)

    # Tell the website the task is now queued on the brain.
    _notify_django(survey_task_id, "queued")

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
  } catch(e) { return null; }
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
        (canCancel ? '<button class="ctrl-btn cancel-btn" onclick="cancelTask(&quot;' + t.id + '&quot;)">&#10005;</button>' : '') +
        '</div>';
      }).join('');
    }
  }

  if (metrics) {
    var t = metrics.today || {};
    var m = metrics.totals || {};
    document.getElementById('completed-today').textContent = t.completed || 0;
    document.getElementById('failed-today').textContent = (t.failed || 0) + ' failed';
    document.getElementById('total-completed').textContent = m.completed || 0;
    document.getElementById('total-prs').textContent = (m.prs_created || 0) + ' PRs created';
    document.getElementById('m-completed').textContent = m.completed || 0;
    document.getElementById('m-failed').textContent = m.failed || 0;
    document.getElementById('m-prs').textContent = m.prs_created || 0;
    document.getElementById('m-cost').textContent = '$' + (m.cost_usd || 0).toFixed(2);
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


# NOTE: DASHBOARD_HTML above is the legacy dashboard (embedded the master key).
# It is superseded by APP_HTML below, which key-gates in the browser and embeds
# no secret. Kept defined for reference/rollback; no route serves it.

APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>TARS</title>
<style>
  :root{
    --bg:#0d0e10; --panel:#16181c; --panel2:#1c1f24; --border:#2a2e35;
    --text:#e6e7e9; --muted:#9aa0a8; --accent:#c96442; --accent2:#e0795a;
    --good:#3fb950; --bad:#f85149; --radius:12px;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{background:var(--bg);color:var(--text);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;}
  button{font:inherit;cursor:pointer}
  input,select,textarea{font:inherit}
  a{color:var(--accent2);text-decoration:none}
  .hidden{display:none !important}

  /* Gate */
  #gate{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;background:var(--bg)}
  #gate .card{width:380px;max-width:90vw;background:var(--panel);border:1px solid var(--border);
    border-radius:16px;padding:32px;text-align:center}
  #gate h1{margin:0 0 4px;font-size:26px;letter-spacing:.5px}
  #gate p{color:var(--muted);margin:0 0 22px;font-size:13px}
  .field{display:flex;flex-direction:column;gap:8px;text-align:left}
  input,select,textarea{background:var(--panel2);border:1px solid var(--border);color:var(--text);
    border-radius:10px;padding:11px 13px;width:100%;outline:none}
  input:focus,select:focus,textarea:focus{border-color:var(--accent)}
  .btn{background:var(--accent);color:#fff;border:none;border-radius:10px;padding:11px 16px;font-weight:600}
  .btn:hover{background:var(--accent2)}
  .btn.ghost{background:transparent;border:1px solid var(--border);color:var(--text)}
  .btn.ghost:hover{border-color:var(--accent)}
  .btn.sm{padding:6px 11px;font-size:12px;border-radius:8px}
  .btn.danger{background:transparent;border:1px solid var(--bad);color:var(--bad)}
  .err{color:var(--bad);font-size:12px;min-height:16px;margin-top:10px}

  /* App shell */
  #app{display:flex;height:100vh}
  #side{width:230px;flex-shrink:0;background:var(--panel);border-right:1px solid var(--border);
    display:flex;flex-direction:column;padding:16px 12px}
  .brand{font-size:20px;font-weight:700;letter-spacing:3px;padding:8px 12px 18px}
  .brand small{display:block;font-size:10px;letter-spacing:1px;color:var(--muted);font-weight:500;margin-top:2px}
  .nav{display:flex;flex-direction:column;gap:2px;flex:1}
  .nav button{display:flex;align-items:center;gap:10px;background:transparent;border:none;color:var(--muted);
    padding:10px 12px;border-radius:9px;text-align:left;width:100%}
  .nav button:hover{background:var(--panel2);color:var(--text)}
  .nav button.active{background:var(--panel2);color:var(--text)}
  .nav .ico{width:18px;text-align:center}
  .who{border-top:1px solid var(--border);padding-top:12px;margin-top:8px;font-size:12px;color:var(--muted)}
  .who b{color:var(--text)}
  #main{flex:1;overflow:auto;padding:28px 34px}
  h2.title{margin:0 0 4px;font-size:22px}
  .sub{color:var(--muted);margin:0 0 22px;font-size:13px}

  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:14px;margin-bottom:26px}
  .stat{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px}
  .stat .n{font-size:28px;font-weight:700}
  .stat .l{color:var(--muted);font-size:12px;margin-top:2px}

  .panel{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:20px;margin-bottom:20px}
  .panel h3{margin:0 0 14px;font-size:15px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--border)}
  th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
  td code{background:var(--panel2);padding:2px 6px;border-radius:5px;font-size:12px}
  .pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:600}
  .pill.ok{background:rgba(63,185,80,.15);color:var(--good)}
  .pill.off{background:rgba(248,81,73,.15);color:var(--bad)}
  .pill.adm{background:rgba(201,100,66,.18);color:var(--accent2)}
  .row{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}
  .row .field{flex:1;min-width:160px}
  .keybox{background:#0f2417;border:1px solid var(--good);border-radius:10px;padding:14px;margin-bottom:16px}
  .keybox code{display:block;font-size:14px;color:#9fe6ab;word-break:break-all;margin:6px 0 10px}
  .muted{color:var(--muted)}
  .soon{text-align:center;color:var(--muted);padding:60px 20px}
  .soon .big{font-size:40px;margin-bottom:10px}
  label.chk{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px}
  label.chk input{width:auto}
  textarea{resize:vertical}

  /* Chat */
  .chatwrap{display:flex;flex-direction:column;height:calc(100vh - 56px)}
  .chathead{display:flex;align-items:center;gap:12px;margin-bottom:14px}
  .modes{display:flex;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:3px}
  .modebtn{background:transparent;border:none;color:var(--muted);padding:7px 15px;border-radius:8px;font-weight:600}
  .modebtn.active{background:var(--accent);color:#fff}
  #chatProj{max-width:210px}
  .thread{flex:1;overflow:auto;display:flex;flex-direction:column;gap:12px;padding:6px 2px}
  .msg{display:flex}
  .msg.user{justify-content:flex-end}
  .msg .b{max-width:74%;padding:11px 14px;border-radius:14px;background:var(--panel);border:1px solid var(--border);white-space:pre-wrap;word-wrap:break-word}
  .msg.user .b{background:var(--accent);border-color:var(--accent);color:#fff}
  .msg.sys .b{background:transparent;border:1px dashed var(--border);color:var(--muted);font-size:13px;max-width:100%}
  .composer{display:flex;gap:10px;align-items:flex-end;padding-top:12px;border-top:1px solid var(--border)}
  .composer textarea{flex:1;resize:none;max-height:140px}
</style>
</head>
<body>

<div id="gate">
  <div class="card">
    <h1>TARS</h1>
    <p>Task Automation &amp; Repository Steward</p>
    <div class="field">
      <input id="gateKey" type="password" placeholder="Enter your API key" autocomplete="off"/>
      <button class="btn" onclick="signIn()">Continue</button>
    </div>
    <div class="err" id="gateErr"></div>
  </div>
</div>

<div id="app" class="hidden">
  <aside id="side">
    <div class="brand">TARS<small>CONTROL</small></div>
    <nav class="nav" id="nav"></nav>
    <div class="who" id="who"></div>
    <button class="btn ghost sm" style="margin-top:10px" onclick="signOut()">Sign out</button>
  </aside>
  <main id="main"></main>
</div>

<script>
const LS='tars_key';
let ME=null;
const $=s=>document.querySelector(s);
function key(){return localStorage.getItem(LS)||''}
async function api(path,opts){
  opts=opts||{};
  const r=await fetch(path,{method:opts.method||'GET',
    headers:{'X-API-Key':key(),'Content-Type':'application/json'},
    body:opts.body?JSON.stringify(opts.body):undefined});
  if(r.status===401){signOut();throw new Error('Unauthorized')}
  const j=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(j.error||('HTTP '+r.status));
  return j;
}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function ago(ts){if(!ts)return '—';const d=Math.floor(Date.now()/1000)-ts;
  if(d<60)return d+'s ago';if(d<3600)return Math.floor(d/60)+'m ago';
  if(d<86400)return Math.floor(d/3600)+'h ago';return Math.floor(d/86400)+'d ago'}

async function signIn(){
  const k=$('#gateKey').value.trim();
  if(!k){$('#gateErr').textContent='Enter a key';return}
  localStorage.setItem(LS,k);
  try{ME=await api('/api/whoami');boot()}
  catch(e){localStorage.removeItem(LS);$('#gateErr').textContent='Invalid key'}
}
function signOut(){localStorage.removeItem(LS);ME=null;
  $('#app').classList.add('hidden');$('#gate').classList.remove('hidden');$('#gateKey').value=''}

const VIEWS=[
  {id:'overview',label:'Overview',ico:'▦'},
  {id:'projects',label:'Projects',ico:'❏'},
  {id:'chat',label:'Chat',ico:'✦'},
  {id:'admin',label:'Admin',ico:'⚙',admin:true},
];
function boot(){
  $('#gate').classList.add('hidden');$('#app').classList.remove('hidden');
  const nav=$('#nav');nav.innerHTML='';
  VIEWS.filter(v=>!v.admin||ME.is_admin).forEach(v=>{
    const b=document.createElement('button');b.dataset.id=v.id;
    b.innerHTML='<span class="ico">'+v.ico+'</span>'+v.label;
    b.onclick=()=>show(v.id);nav.appendChild(b);
  });
  $('#who').innerHTML='Signed in as <b>'+esc(ME.user)+'</b>'+(ME.is_admin?' <span class="pill adm">admin</span>':'');
  show('overview');
}
function show(id){
  document.querySelectorAll('#nav button').forEach(b=>b.classList.toggle('active',b.dataset.id===id));
  ({overview:viewOverview,projects:viewProjects,chat:viewChat,admin:viewAdmin}[id])();
}

async function viewOverview(){
  $('#main').innerHTML='<h2 class="title">Overview</h2><p class="sub">Live cluster status</p><div id="ov">Loading…</div>';
  try{
    const s=await api('/api/status');
    const ct=s.current_task;
    $('#ov').innerHTML=
      '<div class="cards">'+
      stat(s.queue_length,'Queued tasks')+
      stat(s.workers_online+'/'+s.workers_total,'Workers online')+
      stat((s.active_projects||[]).length,'Active projects')+
      stat(ct?'1':'0','Running now')+
      '</div>'+
      '<div class="panel"><h3>Current task</h3>'+
      (ct?('<b>'+esc(ct.title)+'</b><div class="muted">project: '+esc(ct.project)+' · started '+ago(ct.started)+'</div>')
          :'<span class="muted">Idle — nothing running.</span>')+'</div>';
  }catch(e){$('#ov').innerHTML='<span class="muted">'+esc(e.message)+'</span>'}
}
function stat(n,l){return '<div class="stat"><div class="n">'+esc(n)+'</div><div class="l">'+esc(l)+'</div></div>'}

async function viewProjects(){
  $('#main').innerHTML=
    '<h2 class="title">Projects</h2><p class="sub">Repos TARS works on</p>'+
    '<div class="panel"><h3>Add an existing repo</h3><div id="addMsg"></div>'+
      '<div class="row">'+
        '<div class="field" style="flex:2"><label class="muted">Public GitHub URL or owner/repo</label><input id="pRepo" placeholder="https://github.com/owner/repo"/></div>'+
        '<div class="field"><label class="muted">Build cmd (optional)</label><input id="pBuild" placeholder="npm run build"/></div>'+
        '<div class="field"><label class="muted">Test cmd (optional)</label><input id="pTest" placeholder="pytest"/></div>'+
      '</div>'+
      '<div class="row" style="margin-top:12px">'+
        '<label class="chk"><input type="checkbox" id="pAuto"/> Auto-merge (no PR)</label>'+
        '<button class="btn" onclick="addProject()">Add project</button>'+
      '</div></div>'+
    '<div class="panel"><h3>Start a fresh project from design docs</h3><div id="freshMsg"></div>'+
      '<div class="row"><div class="field"><label class="muted">Project name</label><input id="fName" placeholder="my-new-app"/></div></div>'+
      '<div class="field" style="margin-top:10px"><label class="muted">Design documents</label>'+
        '<textarea id="fDocs" rows="5" placeholder="Describe what to build — TARS creates the repo and turns this into a task list."></textarea></div>'+
      '<div class="row" style="margin-top:12px"><button class="btn" onclick="freshProject()">Create repo &amp; generate tasks</button></div></div>'+
    '<div class="panel"><h3>Your projects</h3><div id="projList">Loading…</div></div>';
  loadProjects();
}
async function loadProjects(){
  try{
    const {projects}=await api('/api/projects');
    if(!projects.length){$('#projList').innerHTML='<span class="muted">No projects yet.</span>';return}
    let h='<table><tr><th>Project</th><th>Repo</th><th>Auto-merge</th><th>Build / Test</th></tr>';
    projects.forEach(p=>{
      h+='<tr><td><b>'+esc(p.name)+'</b></td><td><code>'+esc(p.repo||'—')+'</code></td>'+
        '<td><label class="chk"><input type="checkbox" '+(p.auto_merge?'checked':'')+' onchange="toggleAuto(\''+esc(p.name)+'\',this.checked)"/> '+(p.auto_merge?'on':'off')+'</label></td>'+
        '<td class="muted">'+esc(p.build||'—')+' / '+esc(p.test||'—')+'</td></tr>';
    });
    $('#projList').innerHTML=h+'</table>';
  }catch(e){$('#projList').innerHTML='<span class="muted">'+esc(e.message)+'</span>'}
}
async function addProject(){
  const repo=$('#pRepo').value.trim();const box=$('#addMsg');
  if(!repo){box.innerHTML='<div class="err">Enter a repo URL</div>';return}
  try{
    const r=await api('/api/projects',{method:'POST',body:{repo_url:repo,build:$('#pBuild').value.trim(),test:$('#pTest').value.trim(),auto_merge:$('#pAuto').checked}});
    box.innerHTML='<div class="keybox">Added <b>'+esc(r.name)+'</b> ('+esc(r.repo)+'). TARS will pick it up.</div>';
    $('#pRepo').value='';$('#pBuild').value='';$('#pTest').value='';$('#pAuto').checked=false;loadProjects();
  }catch(e){box.innerHTML='<div class="err">'+esc(e.message)+'</div>'}
}
async function toggleAuto(name,on){
  try{await api('/api/projects/'+name+'/settings',{method:'POST',body:{auto_merge:on}})}
  catch(e){alert(e.message)}
  loadProjects();
}
async function freshProject(){
  const name=$('#fName').value.trim();const docs=$('#fDocs').value.trim();const box=$('#freshMsg');
  if(!name||!docs){box.innerHTML='<div class="err">Name and design docs required</div>';return}
  box.innerHTML='<div class="muted">Creating repo and generating tasks… this can take a moment.</div>';
  try{
    const r=await api('/api/projects/fresh',{method:'POST',body:{name,design_docs:docs}});
    box.innerHTML='<div class="keybox">Created <b>'+esc(r.repo)+'</b> with '+r.tasks.length+' tasks:<br><span class="muted">'+r.tasks.map(function(t){return esc(t.title||t)}).join('<br>')+'</span></div>';
    $('#fName').value='';$('#fDocs').value='';loadProjects();
  }catch(e){box.innerHTML='<div class="err">'+esc(e.message)+'</div>'}
}

let CONV=null,MODE='chat',THREAD=[];
async function viewChat(){
  let projs=[];try{projs=(await api('/api/projects')).projects}catch(e){}
  const opts=projs.map(p=>'<option value="'+esc(p.name)+'">'+esc(p.name)+'</option>').join('');
  $('#main').innerHTML=
    '<div class="chatwrap"><div class="chathead">'+
      '<div class="modes"><button id="mChat" class="modebtn active" onclick="setMode(\'chat\')">✦ Chat</button>'+
      '<button id="mTask" class="modebtn" onclick="setMode(\'task\')">➤ Queue task</button></div>'+
      '<select id="chatProj" class="hidden">'+(opts||'<option value="">No projects</option>')+'</select>'+
      '<div style="flex:1"></div><button class="btn ghost sm" onclick="newConv()">New chat</button>'+
    '</div><div id="thread" class="thread"></div>'+
    '<div class="composer"><textarea id="cInput" rows="1" placeholder="Message TARS…" onkeydown="chatKey(event)"></textarea>'+
    '<button class="btn" onclick="sendChat()">Send</button></div></div>';
  setMode(MODE);
  if(!CONV)newConv(); else renderThread();
}
function setMode(m){MODE=m;
  const a=$('#mChat'),b=$('#mTask');if(a)a.classList.toggle('active',m==='chat');if(b)b.classList.toggle('active',m==='task');
  const sel=$('#chatProj');if(sel)sel.classList.toggle('hidden',m!=='task');
  const inp=$('#cInput');if(inp)inp.placeholder=(m==='task')?'Describe a task to queue…':'Message TARS…';
}
function newConv(){CONV='c-'+Math.random().toString(36).slice(2,12);THREAD=[];renderThread()}
function renderThread(){
  const t=$('#thread');if(!t)return;
  t.innerHTML=THREAD.length?THREAD.map(m=>'<div class="msg '+m.role+'"><div class="b">'+esc(m.text)+'</div></div>').join('')
    :'<div class="soon"><div class="big">✦</div>Ask anything — or switch to <b>Queue task</b> to add work to a project.</div>';
  t.scrollTop=t.scrollHeight;
}
function chatKey(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat()}}
async function sendChat(){
  const inp=$('#cInput');const msg=inp.value.trim();if(!msg)return;inp.value='';
  THREAD.push({role:'user',text:msg});
  if(MODE==='task'){
    const project=$('#chatProj').value;
    if(!project){THREAD.push({role:'sys',text:'No project selected — add one in Projects first.'});renderThread();return}
    renderThread();
    try{const r=await api('/api/chat',{method:'POST',body:{message:msg,mode:'task',project}});
      THREAD.push({role:'sys',text:'✓ Queued “'+r.title+'” on '+r.project+' ('+r.task_id+')'});}
    catch(e){THREAD.push({role:'sys',text:'Error: '+e.message})}
    renderThread();return;
  }
  THREAD.push({role:'assistant',text:'…'});renderThread();
  try{const r=await api('/api/chat',{method:'POST',body:{message:msg,mode:'chat',conversation_id:CONV}});
    CONV=r.conversation_id;THREAD[THREAD.length-1]={role:'assistant',text:r.reply};}
  catch(e){THREAD[THREAD.length-1]={role:'assistant',text:'Error: '+e.message};}
  renderThread();
}

async function viewAdmin(){
  $('#main').innerHTML=
    '<h2 class="title">Admin</h2><p class="sub">Issue and manage API keys for your friends</p>'+
    '<div class="panel"><h3>Generate a key</h3>'+
      '<div id="newKey"></div>'+
      '<div class="row">'+
        '<div class="field"><label class="muted">User / friend name</label><input id="kUser" placeholder="e.g. jordan"/></div>'+
        '<label class="chk"><input type="checkbox" id="kAdmin"/> Admin</label>'+
        '<button class="btn" onclick="genKey()">Generate</button>'+
      '</div></div>'+
    '<div class="panel"><h3>Issued keys</h3><div id="keys">Loading…</div></div>';
  loadKeys();
}
async function genKey(){
  const user=$('#kUser').value.trim();const box=$('#newKey');
  if(!user){box.innerHTML='<div class="err">Enter a user name</div>';return}
  try{
    const r=await api('/api/admin/keys',{method:'POST',body:{user,is_admin:$('#kAdmin').checked}});
    box.innerHTML='<div class="keybox"><b>Key for '+esc(user)+'</b> — copy it now, it won\'t be shown again:'+
      '<code>'+esc(r.key)+'</code>'+
      '<button class="btn sm" onclick="navigator.clipboard.writeText(\''+r.key+'\')">Copy</button></div>';
    $('#kUser').value='';$('#kAdmin').checked=false;loadKeys();
  }catch(e){box.innerHTML='<div class="err">'+esc(e.message)+'</div>'}
}
async function loadKeys(){
  try{
    const {keys}=await api('/api/admin/keys');
    if(!keys.length){$('#keys').innerHTML='<span class="muted">No keys yet.</span>';return}
    let h='<table><tr><th>User</th><th>Key</th><th>Scopes</th><th>Created</th><th>Status</th><th></th></tr>';
    keys.forEach(k=>{
      h+='<tr><td>'+esc(k.user)+(k.is_admin?' <span class="pill adm">admin</span>':'')+'</td>'+
        '<td><code>'+esc(k.key_prefix)+'…</code></td>'+
        '<td class="muted">'+esc((k.scopes||[]).join(', '))+'</td>'+
        '<td class="muted">'+ago(k.created)+'</td>'+
        '<td>'+(k.revoked?'<span class="pill off">revoked</span>':'<span class="pill ok">active</span>')+'</td>'+
        '<td>'+(k.revoked?'':'<button class="btn sm danger" onclick="revoke(\''+k.id+'\')">Revoke</button>')+'</td></tr>';
    });
    $('#keys').innerHTML=h+'</table>';
  }catch(e){$('#keys').innerHTML='<span class="muted">'+esc(e.message)+'</span>'}
}
async function revoke(id){
  if(!confirm('Revoke this key? The friend loses access immediately.'))return;
  try{await api('/api/admin/keys/'+id+'/revoke',{method:'POST'});loadKeys()}
  catch(e){alert(e.message)}
}

// Auto-resume a saved session.
(async()=>{
  if(key()){try{ME=await api('/api/whoami');boot()}catch(e){signOut()}}
  $('#gateKey').addEventListener('keydown',e=>{if(e.key==='Enter')signIn()});
})();
</script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def app_home():
    """Friend-facing TARS app. Key-gated in the browser — embeds no secret."""
    from flask import Response
    return Response(APP_HTML, mimetype="text/html")


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
