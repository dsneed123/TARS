"""TARS project creator — scaffold new repos end-to-end."""

import logging
import os
import subprocess
import yaml
from pathlib import Path
from typing import Optional

from lib.git_manager import create_repo, GitManager, GitError, REPOS_DIR
from lib.claude_runner import ClaudeRunner

logger = logging.getLogger("tars.project_creator")

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))
CONFIG_DIR = TARS_HOME / "config"
PROJECTS_DIR = CONFIG_DIR / "projects"


def create_project(
    name: str,
    project_type: str,
    description: str = "",
    visibility: str = "public",
    org: str = "",
    model: str = "sonnet",
) -> dict:
    """Create a new project: GitHub repo, scaffold, push, and generate TARS config.

    Returns dict with: repo_slug, repo_url, local_path, config_path
    """
    logger.info(
        "Creating project: %s (type=%s, visibility=%s)", name, project_type, visibility
    )

    # 1. Create GitHub repo
    repo_slug = create_repo(
        name=name,
        description=description,
        visibility=visibility,
        org=org,
    )
    repo_url = f"https://github.com/{repo_slug}"
    logger.info("Repo created: %s", repo_url)

    # 2. Clone to repos/
    local_path = REPOS_DIR / name
    REPOS_DIR.mkdir(parents=True, exist_ok=True)

    if local_path.exists():
        logger.warning("Local path already exists, removing: %s", local_path)
        import shutil
        shutil.rmtree(local_path)

    git_cmd = os.environ.get("GIT_CMD", "git")
    clone_url = f"https://github.com/{repo_slug}.git"
    try:
        subprocess.run(
            [git_cmd, "clone", clone_url, str(local_path)],
            check=True, capture_output=True, text=True, timeout=120,
        )
    except subprocess.CalledProcessError as e:
        raise GitError(f"Failed to clone {repo_slug}: {e.stderr}")

    logger.info("Cloned to: %s", local_path)

    # 3. Run Claude to scaffold the project
    runner = ClaudeRunner(model=model)
    result = runner.run_with_prompt_file(
        "create_project.md",
        variables={
            "PROJECT_NAME": name,
            "PROJECT_TYPE": project_type,
            "PROJECT_DESCRIPTION": description or f"A {project_type} project",
        },
        cwd=str(local_path),
        timeout=900,
    )
    logger.info("Scaffold complete (tokens: %d in, %d out)",
                result.get("tokens_in", 0), result.get("tokens_out", 0))

    # 4. Commit and push initial code
    git_cmd = os.environ.get("GIT_CMD", "git")

    def _git(args: list[str]):
        subprocess.run(
            [git_cmd] + args,
            cwd=str(local_path),
            check=True, capture_output=True, text=True, timeout=60,
        )

    # Check if there are changes to commit
    status = subprocess.run(
        [git_cmd, "status", "--porcelain"],
        cwd=str(local_path), capture_output=True, text=True,
    )
    if status.stdout.strip():
        _git(["add", "-A"])
        _git(["commit", "-m", f"Initial {project_type} project setup"])
        _git(["push", "origin", "HEAD"])
        logger.info("Initial commit pushed")
    else:
        logger.info("No scaffold changes to commit")

    # 5. Generate TARS project config
    config_path = PROJECTS_DIR / f"{name}.yaml"
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    config = {
        "repo": repo_slug,
        "description": description or f"A {project_type} project",
        "type": project_type,
        "enabled": True,
        "git": {
            "strategy": "branch-pr",
            "base_branch": "main",
            "pr_labels": [],
        },
        "build": {
            "type": "generic",
            "command": _guess_build_command(project_type),
        },
        "test": {
            "command": _guess_test_command(project_type),
        },
        "issues": {
            "enabled": True,
            "labels": ["bug", "enhancement"],
        },
        "auto_discover": {
            "enabled": False,
            "interval": 86400,
            "focus_areas": ["test coverage", "error handling", "code cleanup"],
        },
        "claude": {
            "model": "sonnet",
            "max_turns": 20,
        },
    }

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    logger.info("Config written: %s", config_path)

    return {
        "repo_slug": repo_slug,
        "repo_url": repo_url,
        "local_path": str(local_path),
        "config_path": str(config_path),
    }


def _guess_build_command(project_type: str) -> Optional[str]:
    """Guess a build command based on project type."""
    mapping = {
        "python": None,
        "node": "npm run build",
        "nodejs": "npm run build",
        "typescript": "npm run build",
        "react": "npm run build",
        "next": "npm run build",
        "nextjs": "npm run build",
        "rust": "cargo build",
        "go": "go build ./...",
        "java": "mvn compile",
        "swift": "swift build",
    }
    return mapping.get(project_type.lower())


def _guess_test_command(project_type: str) -> Optional[str]:
    """Guess a test command based on project type."""
    mapping = {
        "python": "pytest",
        "node": "npm test",
        "nodejs": "npm test",
        "typescript": "npm test",
        "react": "npm test",
        "next": "npm test",
        "nextjs": "npm test",
        "rust": "cargo test",
        "go": "go test ./...",
        "java": "mvn test",
        "swift": "swift test",
    }
    return mapping.get(project_type.lower())
