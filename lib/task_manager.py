"""TARS task manager — three task sources + priority scoring."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from lib.config_loader import load_queue, list_projects, load_project
from lib.git_manager import GitManager

logger = logging.getLogger("tars.task_manager")

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))


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
            tasks.append({
                "id": task_id,
                "title": item.get("title", "Untitled"),
                "description": item.get("description", ""),
                "project": project,
                "source": "manual",
                "priority": item.get("priority", PRIORITY_MANUAL),
            })
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

    def complete_task(self, task_id: str):
        """Mark a task as completed."""
        self._mark_completed(task_id)
        logger.info("Task completed: %s", task_id)
