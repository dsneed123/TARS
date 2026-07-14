"""TARS self-improvement loop.

Continuously evolves a project toward a stated goal: each round, analyze the
repo + the history of previous improvement rounds, generate the next 1-N
improvement tasks, and persist them to the project's queue file so the normal
daemon/worker pipeline implements them. When those finish, the next round
builds on the result — a closed loop.

Enable per project in config/projects/<name>.yaml:

    self_improve:
      enabled: true
      goal: "make the dashboard genuinely useful for day trading"
      interval: 3600      # seconds between rounds
      max_queued: 2       # tasks queued per round (waits until they drain)

The daemon calls run_all() once per cycle; every gate (disabled, tasks still
pending, interval not elapsed) is cheap, so idle ticks cost nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

import yaml

from lib.config_loader import load_project, list_projects, QUEUES_DIR
from lib.task_manager import _repo_snapshot, _parse_suggestion_array

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))
STATE_FILE = TARS_HOME / "state" / "improvement_loop.json"

SOURCE = "self-improve"
HISTORY_LIMIT = 20


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(STATE_FILE)


def _read_queue_raw(project: str) -> list[dict]:
    """All tasks (any status) from the project's queue file."""
    path = QUEUES_DIR / f"{project}.yaml"
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return (yaml.safe_load(f) or {}).get("tasks", []) or []
    except (OSError, yaml.YAMLError):
        return []


def _append_queue_tasks(project: str, tasks: list[dict]) -> None:
    """Atomically append tasks to the project's queue file (mirrors the
    controller's _write_queue_tasks — kept here so lib/ stays Flask-free)."""
    QUEUES_DIR.mkdir(parents=True, exist_ok=True)
    path = QUEUES_DIR / f"{project}.yaml"
    existing = _read_queue_raw(project)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        yaml.dump({"tasks": existing + tasks}, f, default_flow_style=False, sort_keys=False)
    tmp.rename(path)


def _history_lines(project: str) -> str:
    """Summarize previous improvement rounds from the queue file — what was
    tried and how it ended — so the model iterates instead of repeating."""
    lines = []
    for t in _read_queue_raw(project):
        if t.get("source") != SOURCE:
            continue
        status = t.get("status", "pending")
        lines.append(f"- [{status}] {t.get('title', '')}")
    if not lines:
        return "(no previous improvement rounds — this is round one)"
    return "\n".join(lines[-HISTORY_LIMIT:])


def _smoke_status(cfg: dict, work_dir: str) -> str:
    """Actually try to build/import the project and report the result, so the
    round prompt has hard evidence of whether the thing runs — a model judging
    completeness from a code snapshot alone misses broken wiring."""
    cmd = (cfg.get("build", {}) or {}).get("command")
    if not cmd:
        for entry in ("main.py", "app.py", "run.py"):
            if (Path(work_dir) / entry).exists():
                cmd = f'python3 -c "import {entry[:-3]}"'
                break
    if not cmd:
        return ("UNKNOWN — no build command configured and no obvious entry "
                "point found. If this project should be runnable, that gap "
                "itself needs building.")
    try:
        r = subprocess.run(cmd, shell=True, cwd=work_dir, capture_output=True,
                           text=True, timeout=180)
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"FAIL — `{cmd}` did not finish: {e}"
    if r.returncode == 0:
        return f"PASS — `{cmd}` succeeded; the project builds/imports cleanly."
    tail = ((r.stderr or "") + "\n" + (r.stdout or "")).strip()[-600:]
    return f"FAIL — `{cmd}` exited {r.returncode}. Output:\n{tail}"


def run_cycle(project: str, force: bool = False) -> dict:
    """Run one improvement round for a project.

    Returns {"skipped": reason} when a gate blocks, {"queued": [tasks]} on
    success, or {"error": msg}. force=True bypasses the enabled/interval
    gates (manual trigger) but still respects the pending-tasks gate so a
    trigger-happy user can't flood the queue.
    """
    try:
        cfg = load_project(project)
    except FileNotFoundError:
        return {"error": f"unknown project: {project}"}

    si = cfg.get("self_improve", {}) or {}
    if not force and not si.get("enabled", False):
        return {"skipped": "disabled"}

    pending = [t for t in _read_queue_raw(project)
               if t.get("source") == SOURCE and t.get("status", "pending") == "pending"]
    if pending:
        return {"skipped": f"{len(pending)} improvement task(s) still pending"}

    state = _load_state()
    proj_state = state.get(project, {})
    interval = int(si.get("interval", 3600))
    if not force and time.time() - proj_state.get("last_run", 0) < interval:
        return {"skipped": "interval not elapsed"}

    repo = cfg.get("repo", "")
    if not repo:
        return {"error": "project has no repo configured"}

    goal = (si.get("goal") or cfg.get("description") or
            "make this project meaningfully better with each iteration")
    max_queued = max(1, int(si.get("max_queued", 2)))

    from lib.git_manager import GitManager
    from lib.claude_runner import ClaudeRunner

    try:
        gm = GitManager(repo)
        work_dir = str(gm.ensure_cloned())
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not prepare repo: {e}"}

    snapshot = _repo_snapshot(work_dir)
    ollama = os.environ.get("TARS_LLM_PROVIDER", "").lower() == "ollama"
    default_model = "qwen2.5-coder:32b" if ollama else cfg.get("claude", {}).get("model", "sonnet")
    model = os.environ.get("OLLAMA_DISCOVER_MODEL", default_model)

    runner = ClaudeRunner(model=model)
    try:
        result = runner.run_with_prompt_file(
            "self_improve.md",
            variables={
                "REPO_NAME": repo,
                "GOAL": goal,
                "ROUND": str(proj_state.get("rounds", 0) + 1),
                "BUILD_STATUS": _smoke_status(cfg, work_dir),
                "HISTORY": _history_lines(project),
                "MAX_TASKS": str(max_queued),
                "REPO_SNAPSHOT": snapshot or "(repository is empty — no files yet)",
            },
            cwd=work_dir,
            max_turns=5,
            timeout=int(os.environ.get("TARS_DISCOVER_TIMEOUT", "420")),
        )
    except Exception as e:  # noqa: BLE001
        return {"error": f"model call failed: {e}"}

    suggestions = _parse_suggestion_array(result.get("result", ""))
    if not suggestions:
        return {"error": "model returned no parseable suggestions"}

    # Don't re-queue something already in the queue file under any source.
    existing_titles = {(t.get("title") or "").strip().lower()
                       for t in _read_queue_raw(project)}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    queued = []
    for s in suggestions[:max_queued]:
        title = (s.get("title") or "").strip()
        if not title or title.lower() in existing_titles:
            continue
        queued.append({
            "id": f"imp-{uuid.uuid4().hex[:8]}",
            "title": title,
            "description": s.get("description", ""),
            "project": project,
            "priority": int(si.get("priority", 45)),
            "status": "pending",
            "source": SOURCE,
            "goal": goal,
            "created_at": now,
        })

    if queued:
        _append_queue_tasks(project, queued)

    state[project] = {
        "last_run": time.time(),
        "rounds": proj_state.get("rounds", 0) + 1,
        "last_queued": [t["title"] for t in queued],
    }
    _save_state(state)

    if not queued:
        return {"error": "all suggestions were duplicates of existing tasks"}
    return {"queued": queued}


def run_all() -> list[str]:
    """One improvement tick across every enabled project. Returns log lines
    for anything noteworthy; silent (empty) when every gate skips."""
    lines = []
    for proj in list_projects():
        name = proj["_name"]
        if not (proj.get("self_improve", {}) or {}).get("enabled", False):
            continue
        result = run_cycle(name)
        if "queued" in result:
            titles = ", ".join(t["title"] for t in result["queued"])
            lines.append(f"Self-improve [{name}]: queued {len(result['queued'])} task(s): {titles}")
        elif "error" in result:
            lines.append(f"Self-improve [{name}]: error: {result['error']}")
        # skips are the normal idle case — stay quiet
    return lines


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(json.dumps(run_cycle(sys.argv[1], force="--force" in sys.argv), indent=2))
    else:
        for line in run_all():
            print(line)
