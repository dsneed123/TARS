"""TARS git operations — clone, branch, commit, push, PR creation."""
from __future__ import annotations

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
    gh_cmd: str = "",
) -> str:
    """Create a new GitHub repository via gh CLI.

    Returns the full repo slug (e.g. 'user/repo-name').
    """
    gh_cmd = gh_cmd or os.environ.get("GH_CMD", "gh")
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
    except FileNotFoundError:
        raise GitError(f"gh CLI not found at '{gh_cmd}' — set GH_CMD env var to the full path")
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
        git_cmd: str = "",
        gh_cmd: str = "",
        author_name: str = "",
        author_email: str = "",
    ):
        self.repo = repo  # e.g. "username/repo-name"
        self.repo_name = repo.split("/")[-1]
        self.base_branch = base_branch
        self.strategy = strategy
        self.pr_labels = pr_labels or ["tars-auto"]
        self.git_cmd = git_cmd or os.environ.get("GIT_CMD", "git")
        self.gh_cmd = gh_cmd or os.environ.get("GH_CMD", "gh")
        self.work_dir = REPOS_DIR / self.repo_name
        # Commit identity for autonomous commits. Defaults to a bot identity so
        # TARS-authored work stays visibly separate from human commits; a
        # project's `git.author_name`/`git.author_email` config can override
        # this to attribute commits to a real GitHub account instead (the
        # email must be a verified email on that account for GitHub to link
        # the avatar/profile).
        self.author_name = author_name or "TARS Bot"
        self.author_email = author_email or "tars@usetars.dev"

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
            # These are TARS's own disposable clones (never a user's working
            # tree). A task killed mid-flight leaves tracked files modified,
            # which makes the checkout/pull below abort ("Please move or
            # remove them before you merge") and bricks every later task on
            # this project. Discard leftovers; untracked files (venvs, caches)
            # are kept.
            status = self._run_git(["status", "--porcelain"])
            if any(line and not line.startswith("??") for line in status.splitlines()):
                logger.warning("Discarding leftover changes in %s from a previous run", self.repo)
                self._run_git(["reset", "--hard", "HEAD"])
        else:
            logger.info("Cloning %s", self.repo)
            subprocess.run(
                [self.gh_cmd, "repo", "clone", self.repo, str(self.work_dir)],
                check=True, capture_output=True, text=True, timeout=300,
            )

        self._ensure_identity()

        # A freshly auto-created repo has no commits and no base branch yet, so
        # `git checkout <base>` would fail ("pathspec 'main' did not match...").
        # Seed an initial commit on the base branch so all later git ops work.
        if not self._remote_has_base_branch():
            self._bootstrap_base_branch()
        else:
            self._run_git(["checkout", self.base_branch])
            # Hard-sync to origin, never `pull`: these clones are disposable
            # mirrors, and a diverged local base (e.g. from a killed task that
            # committed to it) makes `pull` abort and brick every later task.
            self._run_git(["reset", "--hard", f"origin/{self.base_branch}"])

        return self.work_dir

    def _remote_has_base_branch(self) -> bool:
        """True if origin already has the configured base branch."""
        out = self._run_git(["ls-remote", "--heads", "origin", self.base_branch])
        return bool(out.strip())

    def _ensure_identity(self) -> None:
        """Set the repo-local commit identity to the configured author (bot
        default, or a project's git.author_name/author_email override).
        Applied unconditionally — not just on first clone — so a config
        change takes effect on the next run against an existing clone too."""
        self._run_git(["config", "user.name", self.author_name])
        self._run_git(["config", "user.email", self.author_email])

    def _bootstrap_base_branch(self) -> None:
        """Initialize an empty repo with a first commit on the base branch."""
        logger.info("Repo %s is empty — bootstrapping base branch '%s'",
                    self.repo, self.base_branch)
        self._ensure_identity()
        # Create/reset the local base branch (the clone leaves an unborn branch
        # whose name may differ from the configured base, e.g. master vs main).
        self._run_git(["checkout", "-B", self.base_branch])
        has_files = any(p.name != ".git" for p in self.work_dir.iterdir())
        if not has_files:
            (self.work_dir / "README.md").write_text(f"# {self.repo_name}\n")
        self._run_git(["add", "-A"])
        self._run_git(["commit", "-m", "Initial commit (TARS bootstrap)"])
        self._run_git(["push", "-u", "origin", self.base_branch])
        logger.info("Bootstrapped base branch '%s' for %s", self.base_branch, self.repo)

    def create_branch(self, task_id: str, prefix: str = "tars") -> str:
        """Create and checkout a new branch for a task."""
        # Sanitize task_id for branch name
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '-', task_id)[:50]
        branch = f"{prefix}/{safe_id}"

        self._run_git(["checkout", self.base_branch])
        self._run_git(["fetch", "origin", self.base_branch])
        self._run_git(["reset", "--hard", f"origin/{self.base_branch}"])

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

    # Vendored/generated dirs the worker must never commit. The model is told
    # not to, but this stage runs `git add -A` on whatever the workspace holds,
    # so enforce it here: a single committed node_modules/venv adds hundreds of
    # thousands of junk lines that poison the repo and every context digest.
    _NEVER_COMMIT = ("node_modules/", ".venv/", "venv/", "__pycache__/",
                     "dist/", "build/", "*.pyc", ".pytest_cache/")

    def _ensure_gitignore(self) -> None:
        path = self.work_dir / ".gitignore"
        existing = path.read_text().splitlines() if path.exists() else []
        missing = [p for p in self._NEVER_COMMIT if p not in existing]
        if missing:
            path.write_text("\n".join(existing + missing) + "\n")

    def _untrack_vendored(self) -> None:
        """Untrack _NEVER_COMMIT paths that slipped into the index — .gitignore
        alone doesn't stop already-tracked files, and git's `**/` pathspec
        glob silently misses nested dirs, so filter ls-files ourselves."""
        dirs = {p.rstrip("/") for p in self._NEVER_COMMIT if p.endswith("/")}
        bad = [f for f in self._run_git(["ls-files"]).splitlines()
               if f.endswith(".pyc") or dirs.intersection(f.split("/")[:-1])]
        for i in range(0, len(bad), 500):
            self._run_git(["rm", "--cached", "-q", "--", *bad[i:i + 500]])

    def commit_all(self, message: str) -> str:
        """Stage all changes and commit (vendored/generated dirs excluded)."""
        self._ensure_gitignore()
        self._untrack_vendored()
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
