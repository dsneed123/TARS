"""TARS Go — autonomous goal-directed project execution with a self-improvement loop.

Flow per session:
  1. Plan   — model reads description + repo, emits ordered task list (JSON)
  2. Execute — each task runs through OllamaRunner as a coding agent
  3. Review  — model scores completeness (0-100); done if score >= DONE_SCORE
  4. Refine  — if not done, adds new tasks and loops back to Execute
  5. Stop    — when done or MAX_ITERATIONS reached
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tars.go_runner")

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))
STATE_DIR = TARS_HOME / "state" / "go-sessions"
MAX_ITERATIONS = int(os.environ.get("TARS_GO_MAX_ITERATIONS", "5"))
DONE_SCORE = int(os.environ.get("TARS_GO_DONE_SCORE", "90"))
# A task whose title is this similar (or more) to an already done/failed task
# is treated as a repeat attempt, not a new task (see _title_attempt_count).
STUCK_TITLE_SIMILARITY = float(os.environ.get("TARS_GO_STUCK_SIMILARITY", "0.6"))
# How many prior attempts at a similar-titled task before the NEXT one is
# refused and (if it's the review's only proposal) the session stops instead
# of cycling on the same unresolved issue indefinitely.
STUCK_ATTEMPT_LIMIT = int(os.environ.get("TARS_GO_STUCK_ATTEMPTS", "2"))


# ── Session state helpers ────────────────────────────────────────────────────

def create_session(project: str, description: str) -> dict:
    session_id = uuid.uuid4().hex[:12]
    state = {
        "id": session_id,
        "project": project,
        "description": description,
        "status": "planning",
        "iteration": 0,
        "tasks": [],
        "review_history": [],
        "started_at": _now(),
        "finished_at": None,
        "final_score": None,
        "error": None,
    }
    _save(session_id, state)
    return state


def load_session(session_id: str) -> Optional[dict]:
    path = STATE_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def list_sessions(project: str) -> list[dict]:
    """Return summaries of recent sessions for a project (newest first)."""
    if not STATE_DIR.exists():
        return []
    sessions = []
    for p in sorted(STATE_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            s = json.loads(p.read_text())
            if s.get("project") == project:
                sessions.append({
                    "id": s["id"],
                    "status": s.get("status"),
                    "started_at": s.get("started_at"),
                    "final_score": s.get("final_score"),
                    "task_count": len(s.get("tasks", [])),
                })
        except Exception:
            continue
    return sessions[:20]


def _save(session_id: str, state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{session_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(path)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── Background runner ────────────────────────────────────────────────────────

def run_go_session(session_id: str, project_cfg: dict, work_dir: str):
    """Spawn the execution loop in a daemon thread."""
    t = threading.Thread(
        target=_go_loop,
        args=(session_id, project_cfg, work_dir),
        daemon=True,
        name=f"go-{session_id}",
    )
    t.start()


def _go_loop(session_id: str, project_cfg: dict, work_dir: str):
    from lib.ollama_runner import OllamaRunner
    from lib.task_manager import _repo_snapshot

    state = load_session(session_id)
    runner = OllamaRunner()
    project = state["project"]
    test_cmd = (project_cfg.get("test") or {}).get("command", "")
    base_branch = (project_cfg.get("git") or {}).get("base_branch", "main")

    try:
        for iteration in range(1, MAX_ITERATIONS + 1):
            state["iteration"] = iteration

            # ── Plan (first iteration only; subsequent plans come from review) ──
            if iteration == 1:
                state["status"] = "planning"
                _save(session_id, state)
                logger.info("[go:%s] planning (iter %d)", session_id, iteration)

                snapshot = _repo_snapshot(work_dir)
                plan_result = runner.run_with_prompt_file(
                    "go_plan.md",
                    variables={
                        "PROJECT_NAME": project,
                        "DESCRIPTION": state["description"],
                        "REPO_SNAPSHOT": snapshot or "(empty repository — no files yet)",
                    },
                    cwd=work_dir,
                    max_turns=3,
                    timeout=300,
                )
                if plan_result.get("is_error"):
                    _fail(session_id, state, f"Planning model error: {plan_result.get('result','')}")
                    return
                raw_tasks = _parse_task_array(plan_result.get("result", ""))
                if not raw_tasks:
                    _fail(session_id, state, f"Planning returned no parseable tasks. Model output: {plan_result.get('result','')[:300]}")
                    return

                for i, t in enumerate(raw_tasks):
                    state["tasks"].append(_make_task(session_id, iteration, i, t))
                _save(session_id, state)

            # ── Execute queued tasks for this iteration ───────────────────────
            state["status"] = "working"
            _save(session_id, state)

            queued = [t for t in state["tasks"]
                      if t["status"] == "queued" and t["iteration"] == iteration]
            logger.info("[go:%s] executing %d tasks (iter %d)", session_id, len(queued), iteration)

            for task in queued:
                task["status"] = "in_progress"
                task["started_at"] = _now()
                _save(session_id, state)

                prompt = (
                    f"Task: {task['title']}\n\n"
                    f"{task['description']}\n\n"
                    "Implement this fully. When done, commit your changes with git "
                    "(`git add -A && git commit -m '...'`)."
                )
                result = runner.run(prompt, cwd=work_dir, role="code", max_turns=25, timeout=600)

                task["summary"] = (result.get("result") or "")[:600]
                task["status"] = "failed" if result.get("is_error") else "done"
                task["finished_at"] = _now()

                # A task is never trusted as "done" just because the model
                # didn't error — that only proves it didn't crash, not that
                # what it wrote actually parses. Verify before accepting.
                if task["status"] == "done":
                    ok, detail = _syntax_check(work_dir)
                    if not ok:
                        task["status"] = "failed"
                        task["summary"] = (
                            (task["summary"] or "") + "\n\nVERIFICATION FAILED (syntax):\n" + detail
                        )[:1500]
                        logger.warning("[go:%s] task failed verification: %s",
                                        session_id, task["title"][:60])
                        _inject_fix_task(
                            state, queued, iteration, task,
                            title=f"Fix syntax errors after: {task['title'][:50]}",
                            description=f"The following files fail to parse:\n\n{detail}\n\nFix all syntax errors.",
                        )
                _save(session_id, state)

                if test_cmd and task["status"] == "done":
                    _run_tests(state, queued, session_id, runner, work_dir, test_cmd, task, iteration)

                _push(session_id, work_dir, base_branch)

            # ── Review ────────────────────────────────────────────────────────
            state["status"] = "reviewing"
            _save(session_id, state)
            logger.info("[go:%s] reviewing (iter %d)", session_id, iteration)

            snapshot = _repo_snapshot(work_dir)
            done_summaries = "\n".join(
                f"- {t['title']}: {t['summary'] or 'completed'}"
                for t in state["tasks"] if t["status"] == "done"
            ) or "(none)"

            # Ground truth for the reviewer: actually run the checks rather
            # than trust the tasks' own self-reported summaries — this is
            # what catches "claims it unified the GUI, repo still has two."
            verify_ok, verify_detail = _syntax_check(work_dir)
            verification_report = "PASS — all files parse cleanly." if verify_ok else (
                "FAIL:\n" + verify_detail
            )
            if test_cmd:
                try:
                    test_result = subprocess.run(
                        test_cmd, shell=True, cwd=work_dir,
                        capture_output=True, text=True, timeout=120,
                    )
                    verification_report += (
                        f"\n\nTest command (`{test_cmd}`): "
                        + ("PASS" if test_result.returncode == 0 else "FAIL")
                        + "\n" + (test_result.stdout + test_result.stderr)[:1500]
                    )
                except subprocess.TimeoutExpired:
                    verification_report += f"\n\nTest command (`{test_cmd}`): TIMED OUT"

            review_result = runner.run_with_prompt_file(
                "go_review.md",
                variables={
                    "PROJECT_NAME": project,
                    "DESCRIPTION": state["description"],
                    "COMPLETED_TASKS": done_summaries,
                    "REPO_SNAPSHOT": snapshot or "(empty)",
                    "ITERATION": str(iteration),
                    "VERIFICATION": verification_report,
                },
                cwd=work_dir,
                max_turns=3,
                timeout=300,
            )
            if review_result.get("is_error"):
                logger.warning("[go:%s] review model error: %s", session_id, review_result.get("result"))
                review = {"score": 50, "done": False, "summary": "Review failed, continuing.", "new_tasks": []}
            else:
                review = _parse_review(review_result.get("result", ""))
            review["iteration"] = iteration
            state["review_history"].append(review)
            _save(session_id, state)

            score = review.get("score", 0)
            logger.info("[go:%s] review score=%d done=%s", session_id, score, review.get("done"))

            if review.get("done") or score >= DONE_SCORE or iteration >= MAX_ITERATIONS:
                state["status"] = "done"
                state["final_score"] = score
                state["finished_at"] = _now()
                _save(session_id, state)
                logger.info("[go:%s] finished — score=%d iter=%d", session_id, score, iteration)
                return

            # Queue new tasks from review for next iteration
            new_tasks_raw = review.get("new_tasks") or []
            if not new_tasks_raw:
                state["status"] = "done"
                state["final_score"] = score
                state["finished_at"] = _now()
                _save(session_id, state)
                return

            # Refuse to re-queue a task the loop has already attempted (done
            # or failed) STUCK_ATTEMPT_LIMIT+ times under a similar title —
            # that's not progress, it's the same fix failing to stick. If
            # EVERY proposed next task is a repeat, the loop isn't
            # converging: stop for human review instead of burning the rest
            # of the iteration budget circling the same unresolved issue.
            next_iter = iteration + 1
            fresh, stuck = [], []
            for i, nt in enumerate(new_tasks_raw):
                if isinstance(nt, str):
                    nt = {"title": nt, "description": ""}
                if _title_attempt_count(state, nt.get("title", "")) >= STUCK_ATTEMPT_LIMIT:
                    stuck.append(nt.get("title", "(untitled)"))
                else:
                    fresh.append(nt)

            if stuck and not fresh:
                state["status"] = "stuck"
                state["final_score"] = score
                state["finished_at"] = _now()
                state["error"] = (
                    "Stopped: repeated attempts to fix the same issue(s) without it "
                    "sticking — " + "; ".join(stuck[:5]) + ". Needs human review."
                )
                _save(session_id, state)
                logger.warning("[go:%s] stuck loop detected, stopping: %s", session_id, stuck)
                return

            if stuck:
                logger.info("[go:%s] skipping %d repeat task(s), already attempted %dx+: %s",
                            session_id, len(stuck), STUCK_ATTEMPT_LIMIT, stuck)

            for i, nt in enumerate(fresh):
                state["tasks"].append(_make_task(session_id, next_iter, i, nt))
            _save(session_id, state)

        # Max iterations exhausted
        state["status"] = "done"
        state["finished_at"] = _now()
        if state["review_history"]:
            state["final_score"] = state["review_history"][-1].get("score", 0)
        _save(session_id, state)

    except Exception as e:
        logger.exception("[go:%s] fatal error: %s", session_id, e)
        _fail(session_id, state, str(e))


def _push(session_id: str, work_dir: str, base_branch: str) -> None:
    """Push whatever's been committed straight to the base branch so progress
    is visible on GitHub as the loop runs, not just at the end."""
    try:
        result = subprocess.run(
            ["git", "push", "origin", base_branch],
            cwd=work_dir, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            logger.warning("[go:%s] push failed: %s", session_id, result.stderr.strip()[:300])
        else:
            logger.info("[go:%s] pushed to origin/%s", session_id, base_branch)
    except subprocess.TimeoutExpired:
        logger.warning("[go:%s] push timed out", session_id)
    except Exception as e:
        logger.warning("[go:%s] push error: %s", session_id, e)


def _run_tests(state, queued, session_id, runner, work_dir, test_cmd, completed_task, iteration):
    """Run test suite; if it fails, inject a fix task into the current iteration."""
    try:
        result = subprocess.run(
            test_cmd, shell=True, cwd=work_dir,
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            output = (result.stdout + result.stderr)[:3000]
            logger.info("[go:%s] tests failed after '%s', injecting fix task",
                        session_id, completed_task["title"][:40])
            _inject_fix_task(
                state, queued, iteration, completed_task,
                title=f"Fix failing tests after: {completed_task['title'][:50]}",
                description=f"The test suite failed:\n\n{output}\n\nFix all failures.",
            )
    except subprocess.TimeoutExpired:
        logger.warning("[go:%s] test suite timed out", session_id)
    except Exception as e:
        logger.warning("[go:%s] test run error: %s", session_id, e)


def _inject_fix_task(state: dict, queued: list, iteration: int, after_task: dict,
                      title: str, description: str) -> None:
    """Queue an immediate fix task after `after_task`. Appended to BOTH
    state["tasks"] (persistence) and `queued` (the list the current
    iteration's execution loop is actively iterating over) — appending only
    to state["tasks"] silently orphans the fix task forever, since it keeps
    the CURRENT iteration number but the execution loop only re-scans for
    the NEXT iteration's tasks. That was a real bug: fix tasks were created
    but never ran."""
    fix_task = {
        "id": f"{after_task['id']}-fix-{uuid.uuid4().hex[:6]}",
        "title": title,
        "description": description,
        "status": "queued",
        "started_at": None,
        "finished_at": None,
        "summary": None,
        "iteration": iteration,
    }
    idx = next((i for i, t in enumerate(state["tasks"]) if t["id"] == after_task["id"]), None)
    if idx is not None:
        state["tasks"].insert(idx + 1, fix_task)
    else:
        state["tasks"].append(fix_task)
    queued.append(fix_task)


def _syntax_check(work_dir: str) -> tuple[bool, str]:
    """Cheap, language-aware syntax check across the whole repo — the baseline
    "did this actually parse" gate. Runs after every task regardless of
    whether the project configured a test command, so a task that leaves
    unparseable code is never trusted as "done" silently."""
    problems = []
    try:
        py_files = subprocess.run(
            ["git", "ls-files", "*.py"], cwd=work_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout.split()
    except Exception:
        py_files = []
    if py_files:
        try:
            result = subprocess.run(
                ["python3", "-m", "py_compile", *py_files],
                cwd=work_dir, capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                problems.append(f"Python syntax errors:\n{result.stderr.strip()[:1500]}")
        except subprocess.TimeoutExpired:
            problems.append("Python syntax check timed out")

    try:
        js_files = subprocess.run(
            ["git", "ls-files", "*.js"], cwd=work_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout.split()
    except Exception:
        js_files = []
    if js_files:
        if shutil.which("node"):
            for f in js_files:
                try:
                    result = subprocess.run(
                        ["node", "--check", f],
                        cwd=work_dir, capture_output=True, text=True, timeout=30,
                    )
                    if result.returncode != 0:
                        problems.append(f"JS syntax error in {f}:\n{result.stderr.strip()[:500]}")
                except subprocess.TimeoutExpired:
                    problems.append(f"JS syntax check timed out: {f}")
        else:
            logger.debug("node not available — skipping JS syntax check")

    return (not problems), "\n\n".join(problems)


def _similar_title(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return False
    if a == b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= STUCK_TITLE_SIMILARITY


def _title_attempt_count(state: dict, title: str) -> int:
    """How many times a task with a similar title has already been attempted
    (done or failed) across all iterations of this session."""
    return sum(
        1 for t in state["tasks"]
        if t["status"] in ("done", "failed") and _similar_title(t["title"], title)
    )


def _make_task(session_id, iteration, index, raw) -> dict:
    return {
        "id": f"{session_id}-{iteration}-{index}",
        "title": raw.get("title", f"Task {index + 1}"),
        "description": raw.get("description", ""),
        "status": "queued",
        "started_at": None,
        "finished_at": None,
        "summary": None,
        "iteration": iteration,
    }


def _fail(session_id, state, message):
    state["status"] = "failed"
    state["error"] = message
    state["finished_at"] = _now()
    _save(session_id, state)


# ── Response parsers ─────────────────────────────────────────────────────────

def _parse_task_array(text: str) -> list[dict]:
    from lib.task_manager import _parse_suggestion_array
    return _parse_suggestion_array(text)


def _parse_review(text: str) -> dict:
    import re
    # Try ```json fence first
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # Try any {...} block
    for m in re.finditer(r"\{[\s\S]*?\}", text):
        try:
            obj = json.loads(m.group(0))
            if "score" in obj:
                return obj
        except Exception:
            continue
    # Fallback: mine score from prose
    sm = re.search(r"\b(\d{1,3})\s*(?:/\s*100|%|out of)", text, re.IGNORECASE)
    score = int(sm.group(1)) if sm else 50
    done = score >= DONE_SCORE or any(w in text.lower() for w in ("complete", "finished", "done"))
    return {"score": score, "done": done, "summary": text[:500], "new_tasks": []}
