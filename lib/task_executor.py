"""TARS task executor — plan → build → quality-review loop.

Wraps the bare implement step with two quality levers the single ClaudeRunner
call lacked:

  1. PLAN   — expand a (usually terse) task into a detailed implementation brief
              before any code is written, so vague tasks get built out fully.
  2. REVIEW — after building, a reviewer judges whether the result is complete
              and detailed; if not, the coder gets the gaps as feedback and
              tries again (up to TARS_MAX_REVIEW_ROUNDS times).

Returns the same result-dict shape the worker expects from ClaudeRunner, with
token counts aggregated across every sub-call, so the worker's jq parsing and
token tracking keep working unchanged. Every step is best-effort: if planning or
review fails, we fall back to shipping the build rather than blocking the task.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

try:
    from claude_runner import ClaudeRunner
except ImportError:  # imported as lib.task_executor (PYTHONPATH=TARS_HOME)
    from lib.claude_runner import ClaudeRunner

logger = logging.getLogger("tars.task_executor")

MAX_REVIEW_ROUNDS = int(os.environ.get("TARS_MAX_REVIEW_ROUNDS", "2"))
SNAPSHOT_MAX_CHARS = int(os.environ.get("TARS_SNAPSHOT_MAX_CHARS", "16000"))
SNAPSHOT_PER_FILE = int(os.environ.get("TARS_SNAPSHOT_PER_FILE", "6000"))
GIT_CMD = os.environ.get("GIT_CMD", "git")


class TaskExecutor:
    """Plan → build → review-loop around the underlying coding runner."""

    def __init__(self, model: str = "sonnet", max_turns: int = 20, base_branch: str = "main"):
        self.model = model
        self.max_turns = max_turns
        self.base_branch = base_branch
        self.runner = ClaudeRunner(model=model, max_turns=max_turns)
        self._tokens_in = 0
        self._tokens_out = 0
        self._cost = 0.0

    # --------------------------------------------------------------- public
    def execute(self, title: str, description: str, repo: str, cwd: str,
                timeout: int = 900) -> dict:
        plan = self._plan(title, description, repo, cwd, timeout)

        feedback = ""
        last = self._build(title, description, repo, plan, feedback, cwd, timeout)

        for rnd in range(1, MAX_REVIEW_ROUNDS + 1):
            verdict = self._review(title, description, plan, cwd, timeout)
            if verdict.get("sufficient", True):
                logger.info("Quality review round %d: sufficient (score=%s)",
                            rnd, verdict.get("score"))
                break
            logger.info("Quality review round %d: insufficient (score=%s) — rebuilding. Gaps: %s",
                        rnd, verdict.get("score"), verdict.get("gaps"))
            feedback = self._format_feedback(verdict)
            last = self._build(title, description, repo, plan, feedback, cwd, timeout)

        return {
            "result": last.get("result", ""),
            "cost_usd": self._cost,
            "duration_ms": last.get("duration_ms", 0),
            "tokens_in": self._tokens_in,
            "tokens_out": self._tokens_out,
            "is_error": last.get("is_error", False),
            "session_warning": last.get("session_warning", False),
        }

    # --------------------------------------------------------------- steps
    def _plan(self, title, description, repo, cwd, timeout) -> str:
        try:
            res = self._track(self.runner.run_with_prompt_file(
                "plan_task.md",
                variables={"TASK_TITLE": title, "TASK_DESCRIPTION": description,
                           "REPO_NAME": repo},
                cwd=cwd, timeout=timeout,
            ))
            plan = (res.get("result") or "").strip()
            if plan:
                logger.info("Plan generated (%d chars)", len(plan))
                return plan
        except Exception as e:  # noqa: BLE001 - planning is best-effort
            logger.warning("Planning failed (%s); proceeding without a plan", e)
        return "(no plan was generated — build the task out fully and to a high standard.)"

    def _build(self, title, description, repo, plan, feedback, cwd, timeout) -> dict:
        return self._track(self.runner.run_with_prompt_file(
            "implement_task.md",
            variables={
                "TASK_TITLE": title,
                "TASK_DESCRIPTION": description,
                "REPO_NAME": repo,
                "TASK_PLAN": plan,
                "REVIEW_FEEDBACK": feedback or "(none — this is the first attempt.)",
            },
            cwd=cwd, timeout=timeout,
        ))

    def _review(self, title, description, plan, cwd, timeout) -> dict:
        snapshot = self._snapshot(cwd)
        if not snapshot.strip():
            return {"sufficient": True}  # nothing was produced; let the worker decide
        try:
            res = self._track(self.runner.run_with_prompt_file(
                "review_quality.md",
                variables={
                    "TASK_TITLE": title,
                    "TASK_DESCRIPTION": description,
                    "TASK_PLAN": plan,
                    "IMPLEMENTATION": snapshot,
                },
                cwd=cwd, timeout=timeout,
            ))
            return self._parse_verdict(res.get("result", ""))
        except Exception as e:  # noqa: BLE001 - never block shipping on review
            logger.warning("Quality review failed (%s); accepting build", e)
            return {"sufficient": True}

    # --------------------------------------------------------------- helpers
    def _track(self, res: dict) -> dict:
        self._tokens_in += int(res.get("tokens_in", 0) or 0)
        self._tokens_out += int(res.get("tokens_out", 0) or 0)
        self._cost += float(res.get("cost_usd", 0) or 0)
        return res

    @staticmethod
    def _parse_verdict(text: str) -> dict:
        if not text:
            return {"sufficient": True}
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return {"sufficient": True}
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {"sufficient": True}
        return {
            "sufficient": bool(data.get("sufficient", True)),
            "score": data.get("score"),
            "gaps": data.get("gaps", []),
            "feedback": data.get("feedback", ""),
        }

    @staticmethod
    def _format_feedback(verdict: dict) -> str:
        parts = []
        if verdict.get("feedback"):
            parts.append(str(verdict["feedback"]))
        gaps = verdict.get("gaps") or []
        if gaps:
            parts.append("Specific gaps to fix:\n" + "\n".join(f"- {g}" for g in gaps))
        return "\n\n".join(parts) or "Make the implementation more complete and detailed."

    def _snapshot(self, cwd: str) -> str:
        """Collect the current implementation (committed-vs-base + working-tree
        changes) so the text-mode reviewer can judge actual file contents."""
        cwd = str(cwd)
        files: list[str] = []
        try:
            committed = subprocess.run(
                [GIT_CMD, "diff", "--name-only", f"{self.base_branch}...HEAD"],
                cwd=cwd, capture_output=True, text=True, timeout=30,
            ).stdout.split()
            status = subprocess.run(
                [GIT_CMD, "status", "--porcelain"],
                cwd=cwd, capture_output=True, text=True, timeout=30,
            ).stdout.splitlines()
            changed = [line[3:].strip() for line in status if line.strip()]
            for f in committed + changed:
                if f and f not in files:
                    files.append(f)
        except Exception as e:  # noqa: BLE001
            logger.debug("snapshot listing failed: %s", e)

        out: list[str] = []
        total = 0
        for rel in files:
            p = Path(cwd) / rel
            if not p.is_file():
                continue
            try:
                content = p.read_text(errors="replace")
            except OSError:
                continue
            if len(content) > SNAPSHOT_PER_FILE:
                content = content[:SNAPSHOT_PER_FILE] + "\n...[truncated]..."
            block = f"=== {rel} ===\n{content}\n"
            if total + len(block) > SNAPSHOT_MAX_CHARS:
                out.append("...[more files omitted]...")
                break
            out.append(block)
            total += len(block)
        return "\n".join(out)
