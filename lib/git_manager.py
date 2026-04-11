"""TARS git operations — clone, branch, commit, push, PR creation."""

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tars.git_manager")

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))
REPOS_DIR = TARS_HOME / "repos"


class GitError(Exception):
    """Raised on git operation failure."""
    pass


def create_repo(
    name: str,
    description: str = "",
    visibility: str = "public",
    org: str = "",
    gh_cmd: str = "gh",
) -> str:
    """Create a new GitHub repository via gh CLI.

    Returns the full repo slug (e.g. 'user/repo-name').
    """
    args = [gh_cmd, "repo", "create"]

    if org:
        args.append(f"{org}/{name}")
    else:
        args.append(name)

    args.append(f"--{visibility}")

    if description:
        # Sanitize: GitHub rejects control characters in descriptions
        clean_desc = " ".join(description.split())
        args.extend(["--description", clean_desc])

    # Source code only, clone handled separately
    args.append("--clone=false")

    logger.info("Creating repo: %s (visibility=%s, org=%s)", name, visibility, org or "personal")

    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"gh repo create timed out for {name}")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        # If repo already exists, just return the slug
        if "already exists" in stderr.lower():
            logger.info("Repo %s already exists, continuing", name)
            if org:
                return f"{org}/{name}"
            try:
                user_result = subprocess.run(
                    [gh_cmd, "api", "user", "-q", ".login"],
                    capture_output=True, text=True, timeout=30,
                )
                return f"{user_result.stdout.strip()}/{name}"
            except Exception:
                return name
        raise GitError(f"gh repo create failed: {stderr}")

    # Extract repo slug from output or construct it
    output = result.stdout.strip()
    if "/" in output:
        # gh usually outputs the URL; extract owner/name
        slug = output.rstrip("/").split("github.com/")[-1] if "github.com" in output else output
    else:
        # Fetch the authenticated user if no org
        if org:
            slug = f"{org}/{name}"
        else:
            try:
                user_result = subprocess.run(
                    [gh_cmd, "api", "user", "-q", ".login"],
                    capture_output=True, text=True, timeout=30,
                )
                username = user_result.stdout.strip()
                slug = f"{username}/{name}"
            except Exception:
                slug = name

    logger.info("Created repo: %s", slug)
    return slug


class GitManager:
    """Manages git operations for a single repository."""

    def __init__(
        self,
        repo: str,
        base_branch: str = "main",
        strategy: str = "branch-pr",
        pr_labels: Optional[list[str]] = None,
        git_cmd: str = "git",
        gh_cmd: str = "gh",
    ):
        self.repo = repo  # e.g. "username/repo-name"
        self.repo_name = repo.split("/")[-1]
        self.base_branch = base_branch
        self.strategy = strategy
        self.pr_labels = pr_labels or ["tars-auto"]
        self.git_cmd = git_cmd
        self.gh_cmd = gh_cmd
        self.work_dir = REPOS_DIR / self.repo_name

    def _run_git(self, args: list[str], cwd: Optional[Path] = None) -> str:
        """Run a git command, return stdout."""
        cmd = [self.git_cmd] + args
        cwd = cwd or self.work_dir
        logger.debug("git %s (cwd=%s)", " ".join(args), cwd)
        try:
            result = subprocess.run(
                cmd, cwd=str(cwd), capture_output=True, text=True, timeout=120
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"git command timed out: {' '.join(args)}")

        if result.returncode != 0:
            raise GitError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def _run_gh(self, args: list[str]) -> str:
        """Run a gh CLI command, return stdout."""
        cmd = [self.gh_cmd] + args
        logger.debug("gh %s", " ".join(args))
        try:
            result = subprocess.run(
                cmd, cwd=str(self.work_dir), capture_output=True, text=True, timeout=120
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"gh command timed out: {' '.join(args)}")

        if result.returncode != 0:
            raise GitError(f"gh {args[0]} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def ensure_cloned(self) -> Path:
        """Clone repo if not already present, otherwise fetch."""
        REPOS_DIR.mkdir(parents=True, exist_ok=True)

        if self.work_dir.exists():
            logger.info("Fetching %s", self.repo)
            self._run_git(["fetch", "origin"])
            self._run_git(["checkout", self.base_branch])
            self._run_git(["pull", "origin", self.base_branch])
        else:
            logger.info("Cloning %s", self.repo)
            subprocess.run(
                [self.gh_cmd, "repo", "clone", self.repo, str(self.work_dir)],
                check=True, capture_output=True, text=True, timeout=300,
            )

        return self.work_dir

    def create_branch(self, task_id: str, prefix: str = "tars") -> str:
        """Create and checkout a new branch for a task."""
        # Sanitize task_id for branch name
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '-', task_id)[:50]
        branch = f"{prefix}/{safe_id}"

        self._run_git(["checkout", self.base_branch])
        self._run_git(["pull", "origin", self.base_branch])

        # Delete branch if it already exists locally
        try:
            self._run_git(["branch", "-D", branch])
        except GitError:
            pass

        self._run_git(["checkout", "-b", branch])
        logger.info("Created branch: %s", branch)
        return branch

    def get_diff(self) -> str:
        """Get the diff of staged + unstaged changes."""
        staged = self._run_git(["diff", "--cached"])
        unstaged = self._run_git(["diff"])
        return (staged + "\n" + unstaged).strip()

    def get_diff_from_base(self) -> str:
        """Get diff from base branch."""
        return self._run_git(["diff", f"origin/{self.base_branch}...HEAD"])

    def has_changes(self) -> bool:
        """Check if there are uncommitted changes."""
        status = self._run_git(["status", "--porcelain"])
        return bool(status)

    def commit_all(self, message: str) -> str:
        """Stage all changes and commit."""
        self._run_git(["add", "-A"])
        self._run_git(["commit", "-m", message])
        sha = self._run_git(["rev-parse", "HEAD"])
        logger.info("Committed: %s (%s)", message[:60], sha[:8])
        return sha

    def push(self, branch: str) -> None:
        """Push branch to origin (force-push to handle retries)."""
        self._run_git(["push", "--force-with-lease", "-u", "origin", branch])
        logger.info("Pushed branch: %s", branch)

    def create_pr(
        self,
        title: str,
        body: str,
        branch: str,
        draft: bool = False,
    ) -> str:
        """Create a pull request, return the PR URL."""
        args = [
            "pr", "create",
            "--title", title,
            "--body", body,
            "--base", self.base_branch,
            "--head", branch,
        ]

        if draft:
            args.append("--draft")

        try:
            # Try with labels first, fall back without if labels don't exist
            if self.pr_labels:
                args_with_labels = list(args)
                for label in self.pr_labels:
                    args_with_labels.extend(["--label", label])
                try:
                    result = self._run_gh(args_with_labels)
                    pr_url = result.strip().split("\n")[-1]
                    logger.info("Created PR: %s", pr_url)
                    return pr_url
                except GitError as e:
                    if "not found" in str(e).lower():
                        logger.warning("Labels not found on repo, creating PR without labels")
                    else:
                        raise

            result = self._run_gh(args)
            pr_url = result.strip().split("\n")[-1]
            logger.info("Created PR: %s", pr_url)
            return pr_url
        except GitError as e:
            # If PR already exists for this branch, return the existing URL
            err_msg = str(e)
            if "already exists" in err_msg:
                existing_url = re.search(r'https://github\.com/\S+', err_msg)
                if existing_url:
                    logger.info("PR already exists: %s", existing_url.group())
                    return existing_url.group()
            raise

    def push_and_pr(
        self,
        branch: str,
        title: str,
        body: str,
        draft: bool = False,
    ) -> Optional[str]:
        """Push and create PR based on strategy."""
        self.push(branch)

        if self.strategy == "branch-pr":
            return self.create_pr(title, body, branch, draft=draft)
        elif self.strategy == "auto-merge":
            pr_url = self.create_pr(title, body, branch)
            # Merge immediately via squash, fall back to --auto if branch protection exists
            try:
                self._run_gh(["pr", "merge", "--squash", "--delete-branch", pr_url])
                logger.info("Merged PR: %s", pr_url)
            except GitError as e:
                if "auto-merge" in str(e).lower() or "protected" in str(e).lower():
                    try:
                        self._run_gh(["pr", "merge", "--auto", "--squash", pr_url])
                        logger.info("Auto-merge enabled for: %s", pr_url)
                    except GitError as e2:
                        logger.warning("Could not enable auto-merge: %s", e2)
                else:
                    logger.warning("Could not merge PR: %s", e)
            return pr_url
        elif self.strategy == "direct-main":
            self._run_git(["checkout", self.base_branch])
            self._run_git(["merge", branch])
            self._run_git(["push", "origin", self.base_branch])
            logger.info("Direct-pushed to %s", self.base_branch)
            return None

        return None

    def get_open_issues(
        self,
        labels: Optional[list[str]] = None,
        limit: int = 20,
    ) -> list[dict]:
        """Fetch open issues from GitHub."""
        args = ["issue", "list", "--state", "open", "--json",
                "number,title,body,labels,assignees", "--limit", str(limit)]

        if labels:
            args.extend(["--label", ",".join(labels)])

        result = self._run_gh(args)
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return []

    def cleanup_branch(self, branch: str) -> None:
        """Delete a local branch after PR is created."""
        self._run_git(["checkout", self.base_branch])
        try:
            self._run_git(["branch", "-D", branch])
        except GitError:
            pass
