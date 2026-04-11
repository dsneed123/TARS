"""TARS config loader — parse YAML configs with defaults and validation."""

import os
import yaml
from pathlib import Path
from typing import Any

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))
CONFIG_DIR = TARS_HOME / "config"
PROJECTS_DIR = CONFIG_DIR / "projects"

DEFAULT_PROJECT = {
    "description": "",
    "type": "generic",
    "enabled": True,
    "git": {
        "strategy": "branch-pr",
        "base_branch": "main",
        "pr_labels": ["tars-auto"],
    },
    "build": {
        "type": "generic",
        "command": None,
    },
    "test": {
        "command": None,
    },
    "issues": {
        "enabled": False,
        "labels": ["tars"],
    },
    "auto_discover": {
        "enabled": False,
        "interval": 86400,
        "focus_areas": [],
    },
    "claude": {
        "model": "sonnet",
        "max_turns": 20,
    },
}

DEFAULT_TOKEN_BUDGET = {
    "daily_limit": 1_000_000,
    "peak_hours": {"start": 9, "end": 17},
    "peak_multiplier": 0.5,
    "rate_limit_backoff": 60,
    "warning_threshold": 0.8,
}

DEFAULT_DISCORD = {
    "webhook_url": "",
    "username": "TARS",
    "avatar_url": "",
    "notify_on": ["task_complete", "task_failed", "circuit_breaker", "daily_summary"],
    "chat_model": "sonnet",
    "chat_max_tokens": 4096,
    "servers": {},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base recursively."""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def load_yaml(path: Path) -> dict:
    """Load a YAML file, return empty dict if missing."""
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_project(name: str) -> dict:
    """Load a project config by name, merged with defaults."""
    path = PROJECTS_DIR / f"{name}.yaml"
    raw = load_yaml(path)
    if not raw:
        raise FileNotFoundError(f"Project config not found: {path}")
    return _deep_merge(DEFAULT_PROJECT, raw)


def list_projects() -> list[dict]:
    """List all enabled project configs."""
    projects = []
    if not PROJECTS_DIR.exists():
        return projects
    for path in sorted(PROJECTS_DIR.glob("*.yaml")):
        try:
            cfg = load_project(path.stem)
            if cfg.get("enabled", True):
                cfg["_name"] = path.stem
                projects.append(cfg)
        except Exception:
            continue
    return projects


def load_token_budget() -> dict:
    """Load token budget config."""
    raw = load_yaml(CONFIG_DIR / "token_budget.yaml")
    return _deep_merge(DEFAULT_TOKEN_BUDGET, raw)


def load_discord_config() -> dict:
    """Load Discord webhook config."""
    raw = load_yaml(CONFIG_DIR / "discord.yaml")
    return _deep_merge(DEFAULT_DISCORD, raw)


QUEUES_DIR = CONFIG_DIR / "queues"


def load_queue(project: str = "") -> list[dict]:
    """Load task queue. If project given, load only that project's queue."""
    tasks = []
    if project:
        raw = load_yaml(QUEUES_DIR / f"{project}.yaml")
        for t in raw.get("tasks", []):
            t.setdefault("project", project)
            tasks.append(t)
    else:
        # Load all per-project queues
        if QUEUES_DIR.exists():
            for p in sorted(QUEUES_DIR.glob("*.yaml")):
                raw = load_yaml(p)
                for t in raw.get("tasks", []):
                    t.setdefault("project", p.stem)
                    tasks.append(t)
        # Fallback: also read legacy queue.yaml if it has tasks not yet migrated
        legacy = load_yaml(CONFIG_DIR / "queue.yaml")
        legacy_tasks = legacy.get("tasks", [])
        existing_ids = {t.get("id") for t in tasks}
        for t in legacy_tasks:
            if t.get("id") not in existing_ids:
                tasks.append(t)
    return [t for t in tasks if t.get("status", "pending") == "pending"]


def get_project_config(name: str, key: str, default: Any = None) -> Any:
    """Get a nested config value using dot notation. e.g. 'git.strategy'"""
    cfg = load_project(name)
    keys = key.split(".")
    val = cfg
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k)
        else:
            return default
    return val if val is not None else default
