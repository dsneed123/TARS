"""TARS task manager — three task sources + priority scoring."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

import yaml

from lib.config_loader import load_queue, list_projects, load_project, QUEUES_DIR, CONFIG_DIR
from lib.git_manager import GitManager

logger = logging.getLogger("tars.task_manager")

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))


_SNAPSHOT_SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf", ".zip",
    ".gz", ".lock", ".map", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3",
}


def _repo_snapshot(work_dir: str, max_chars: int = 16000, per_file: int = 4000) -> str:
    """Build a compact text snapshot of a repo (file tree + readable file
    contents) so a single-completion model can analyze it without file tools.
    READMEs and smaller files are included first until the char budget runs out."""
    import subprocess

    git = os.environ.get("GIT_CMD", "git")
    try:
        listing = subprocess.run(
            [git, "ls-files"], cwd=work_dir, capture_output=True, text=True, timeout=30
        ).stdout
    except Exception:  # noqa: BLE001
        listing = ""
    files = [f for f in listing.splitlines() if f.strip()]
    if not files:
        return ""

    out = [f"FILE TREE ({len(files)} files):\n" + "\n".join(files[:300]) + "\n"]
    total = len(out[0])

    def _key(f: str):
        return (0 if "readme" in f.lower() else 1, len(f))

    for rel in sorted(files, key=_key):
        p = Path(work_dir) / rel
        if not p.is_file() or p.suffix.lower() in _SNAPSHOT_SKIP_EXT:
            continue
        try:
            content = p.read_text(errors="replace")
        except OSError:
            continue
        if len(content) > per_file:
            content = content[:per_file] + "\n...[truncated]..."
        block = f"\n=== {rel} ===\n{content}\n"
        if total + len(block) > max_chars:
            break
        out.append(block)
        total += len(block)
    return "".join(out)


def _parse_suggestion_array(text: str) -> list[dict]:
    """Parse a JSON array of suggestions from Claude's response text.

    Tries code-fence extraction first, then falls back to finding
    [...] blocks that contain objects (dicts).
    """
    import re

    def _is_valid(parsed) -> bool:
        """Check parsed value is a non-empty list of dicts."""
        return isinstance(parsed, list) and parsed and all(isinstance(x, dict) for x in parsed)

    # 1) Try to extract from ```json ... ``` code fence
    fence_match = re.search(r'```(?:json)?\s*(\[[\s\S]*?\])\s*```', text)
    if fence_match:
        try:
            parsed = json.loads(fence_match.group(1))
            if _is_valid(parsed):
                return parsed
        except json.JSONDecodeError:
            pass

    # 2) Find bracket-balanced [...] blocks starting from each '['
    for i, ch in enumerate(text):
        if ch != '[':
            continue
        depth = 0
        in_str = False
        escape = False
        for j in range(i, len(text)):
            c = text[j]
            if escape:
                escape = False
                continue
            if c == '\\' and in_str:
                escape = True
                continue
            if c == '"' and not escape:
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == '[':
                depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    candidate = text[i:j + 1]
                    try:
                        parsed = json.loads(candidate)
                        if _is_valid(parsed):
                            return parsed
                    except json.JSONDecodeError:
                        pass
                    break

    return []
STATE_DIR = TARS_HOME / "state"

# Priority scores by source
PRIORITY_MANUAL = 100
PRIORITY_ISSUE = 80
PRIORITY_AUTO = 40


class TaskManager:
    """Discovers and prioritizes tasks from multiple sources."""

    def __init__(self):
        self.state_file = STATE_DIR / "task_state.json"
        self.state = self._load_state()

    def _load_state(self) -> dict:
        if self.state_file.exists():
            with open(self.state_file) as f:
                return json.load(f)
        return {"completed": [], "last_discovery": {}}

    def _save_state(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=2)

    def _is_completed(self, task_id: str) -> bool:
        return task_id in self.state.get("completed", [])

    def _mark_completed(self, task_id: str):
        if task_id not in self.state["completed"]:
            self.state["completed"].append(task_id)
            # Keep only last 500 completed task IDs
            self.state["completed"] = self.state["completed"][-500:]
            self._save_state()

    def get_manual_tasks(self) -> list[dict]:
        """Get tasks from the manual queue (config/queue.yaml)."""
        enabled = list_projects()
        enabled_names = {p["_name"] for p in enabled}
        # Map owner/repo form back to the project's short name so
        # website-submitted tasks (which use github_repo) resolve correctly.
        repo_to_name = {p["repo"]: p["_name"] for p in enabled if p.get("repo")}

        tasks = []
        for item in load_queue():
            raw_project = item.get("project", "")
            project = repo_to_name.get(raw_project, raw_project)
            if project and project not in enabled_names:
                continue
            task_id = f"manual-{item.get('id', hash(item.get('title', '')))}".replace(" ", "-")
            if self._is_completed(task_id):
                continue
            task = {
                "id": task_id,
                # The task's own id inside its queue YAML file (queue.yaml or
                # queues/<project>.yaml) — distinct from the "manual-"
                # prefixed dedupe id above. Needed to write the status back
                # once the task finishes (see complete_task()).
                "queue_id": item.get("id"),
                "title": item.get("title", "Untitled"),
                "description": item.get("description", ""),
                "project": project,
                # Preserve the real source ("website") so the worker knows to
                # report live status; fall back to "manual" for hand-added items.
                "source": item.get("source", "manual"),
                "priority": item.get("priority", PRIORITY_MANUAL),
            }
            # Carry the website's task id through so the worker's notify_django
            # callback can drive the progress bar on tarsai.dev.
            if item.get("survey_task_id") is not None:
                task["survey_task_id"] = item["survey_task_id"]
            tasks.append(task)
        return tasks

    def get_github_issues(self, project_name: str) -> list[dict]:
        """Get tasks from GitHub issues for a project."""
        tasks = []
        try:
            cfg = load_project(project_name)
        except FileNotFoundError:
            return tasks

        if not cfg.get("issues", {}).get("enabled", False):
            return tasks

        repo = cfg.get("repo", "")
        labels = cfg.get("issues", {}).get("labels", ["tars"])

        try:
            gm = GitManager(repo)
            issues = gm.get_open_issues(labels=labels, limit=10)
        except Exception as e:
            logger.warning("Failed to fetch issues for %s: %s", project_name, e)
            return tasks

        for issue in issues:
            task_id = f"issue-{project_name}-{issue['number']}"
            if self._is_completed(task_id):
                continue
            tasks.append({
                "id": task_id,
                "title": issue.get("title", ""),
                "description": issue.get("body", ""),
                "project": project_name,
                "source": "github",
                "priority": PRIORITY_ISSUE,
                "issue_number": issue["number"],
            })

        return tasks

    def get_auto_discovered_tasks(self, project_name: str, force: bool = False) -> list[dict]:
        """Get auto-discovered improvement tasks via model analysis.
        force=True bypasses the enabled/interval gates (user-triggered button)."""
        tasks = []
        try:
            cfg = load_project(project_name)
        except FileNotFoundError:
            return tasks

        auto_cfg = cfg.get("auto_discover", {})
        if not force and not auto_cfg.get("enabled", False):
            return tasks

        # Check if enough time has passed since last discovery
        import time
        last_run = self.state.get("last_discovery", {}).get(project_name, 0)
        interval = auto_cfg.get("interval", 86400)
        if not force and time.time() - last_run < interval:
            return tasks

        # Run discovery via Claude
        try:
            from lib.claude_runner import ClaudeRunner
            repo = cfg.get("repo", "")
            focus = ", ".join(auto_cfg.get("focus_areas", ["code quality"]))
            description = cfg.get("description", "")
            project_type = cfg.get("type", cfg.get("build", {}).get("type", "generic"))

            gm = GitManager(repo)
            work_dir = gm.ensure_cloned()

            # The discover prompt runs as a single text completion (no file
            # tools), so feed it the actual repo contents — otherwise it suggests
            # tasks blind. Use the 32B coder (fast, often already warm) instead of
            # the 70B reasoner so the user-facing button returns in time.
            snapshot = _repo_snapshot(str(work_dir))
            ollama = os.environ.get("TARS_LLM_PROVIDER", "").lower() == "ollama"
            default_model = "qwen2.5-coder:32b" if ollama else cfg.get("claude", {}).get("model", "sonnet")
            discover_model = os.environ.get("OLLAMA_DISCOVER_MODEL", default_model)

            runner = ClaudeRunner(model=discover_model)
            result = runner.run_with_prompt_file(
                "discover_improvements.md",
                variables={
                    "REPO_NAME": repo,
                    "FOCUS_AREAS": focus,
                    "PROJECT_DESCRIPTION": description or f"A {project_type} project",
                    "PROJECT_TYPE": project_type,
                    "REPO_SNAPSHOT": snapshot or "(repository is empty — no files yet)",
                },
                cwd=str(work_dir),
                max_turns=5,
                timeout=int(os.environ.get("TARS_DISCOVER_TIMEOUT", "420")),
            )

            # Parse suggestions from Claude's response
            suggestions = self._parse_suggestions(result.get("result", ""))
            for i, s in enumerate(suggestions):
                task_id = f"auto-{project_name}-{int(time.time())}-{i}"
                tasks.append({
                    "id": task_id,
                    "title": s.get("title", "Improvement"),
                    "description": s.get("description", ""),
                    "project": project_name,
                    "source": "auto",
                    "priority": self._score_auto_priority(s),
                })

            # Update last discovery time
            self.state.setdefault("last_discovery", {})[project_name] = time.time()
            self._save_state()

        except Exception as e:
            logger.warning("Auto-discovery failed for %s: %s", project_name, e)

        return tasks

    def _parse_suggestions(self, text: str) -> list[dict]:
        """Parse JSON array of suggestions from Claude's response."""
        return _parse_suggestion_array(text)

    def _score_auto_priority(self, suggestion: dict) -> int:
        """Score an auto-discovered suggestion."""
        base = PRIORITY_AUTO
        priority = suggestion.get("priority", "medium")
        effort = suggestion.get("effort", "medium")

        if priority == "high":
            base += 15
        elif priority == "low":
            base -= 10

        if effort == "small":
            base += 10
        elif effort == "large":
            base -= 15

        return max(10, base)

    def get_all_tasks(self) -> list[dict]:
        """Get all tasks from all sources, sorted by priority."""
        tasks = []

        # Manual queue (not project-specific)
        tasks.extend(self.get_manual_tasks())

        # Per-project sources
        for project in list_projects():
            name = project["_name"]
            tasks.extend(self.get_github_issues(name))
            tasks.extend(self.get_auto_discovered_tasks(name))

        # Sort by priority (highest first)
        tasks.sort(key=lambda t: t.get("priority", 0), reverse=True)
        return tasks

    def get_next_task(self) -> Optional[dict]:
        """Get the highest-priority pending task."""
        tasks = self.get_all_tasks()
        return tasks[0] if tasks else None

    def complete_task(self, task_id: str, project: Optional[str] = None,
                       queue_id: Optional[str] = None, status: str = "completed"):
        """Mark a task as completed (or failed/abandoned).

        Records the id in the dedupe list so the scheduler never re-picks it
        (existing behavior), and — when project/queue_id are known, i.e. this
        was a manual/website task with a real queue-file entry — also
        rewrites that entry's `status` field so the controller API and CLI
        (which read the queue YAML directly, not this dedupe list) stop
        showing it as pending.
        """
        self._mark_completed(task_id)
        if project and queue_id:
            self._update_queue_status(project, queue_id, status)
        logger.info("Task completed: %s", task_id)

    def _update_queue_status(self, project: str, queue_id: str, status: str) -> bool:
        """Find queue_id in the project's queue file (falling back to the
        legacy global queue.yaml) and set its status, mirroring what the
        controller's /api/tasks/<id>/cancel endpoint does. Returns True if a
        matching task was found and updated."""
        import time as _time

        candidates = [QUEUES_DIR / f"{project}.yaml", CONFIG_DIR / "queue.yaml"]
        for path in candidates:
            if not path.exists():
                continue
            try:
                with open(path) as f:
                    raw = yaml.safe_load(f) or {}
            except (OSError, yaml.YAMLError):
                continue
            found = False
            for t in raw.get("tasks", []) or []:
                if t.get("id") == queue_id:
                    t["status"] = status
                    t[f"{status}_at"] = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
                    found = True
                    break
            if found:
                tmp = path.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
                tmp.rename(path)
                return True
        return False
