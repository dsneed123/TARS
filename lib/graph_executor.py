"""TARS graph executor — runs a task through the typed node DAG.

The pipeline is a graph of nodes (intake → plan → implement → verify → review →
integrate) declared in config/graph.yaml, overridable per project. Nodes
exchange small structured artifacts — never raw transcripts — and the executor
records per-node timing, LLM-call counts, and tokens to state/runs/<task_id>.json.

Usage (what bin/tars-worker.sh calls):
    python3 -m lib.graph_executor <project> <task_json_file>

Exit code 0 = task completed and integrated; non-zero = failed (reason on
stderr and in the run state file).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from typing import Optional

import yaml

from lib import repo_map as repomap
from lib.config_loader import load_project, load_yaml, CONFIG_DIR
from lib.llm import LLM, extract_json, load_roles

logger = logging.getLogger("tars.graph")

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))
PROMPTS_DIR = TARS_HOME / "prompts"
RUNS_DIR = TARS_HOME / "state" / "runs"
GIT_CMD = os.environ.get("GIT_CMD", "git")

PERSONA = os.environ.get(
    "TARS_PERSONA",
    "You are TARS, an autonomous coding agent built by Davis Sneed.",
)

DIFF_MAX_CHARS = 14_000
ERROR_TAIL_CHARS = 3_000


def load_graph(project_cfg: dict) -> dict:
    """Default graph merged with the project's `graph:` override."""
    base = load_yaml(CONFIG_DIR / "graph.yaml")
    override = project_cfg.get("graph") or {}
    merged = {
        "models": {**base.get("models", {}), **override.get("models", {})},
        "nodes": {**base.get("nodes", {}), **override.get("nodes", {})},
    }
    return merged


def prompt(name: str, **vars) -> str:
    text = (PROMPTS_DIR / name).read_text()
    for k, v in vars.items():
        text = text.replace(f"{{{{{k}}}}}", str(v))
    return text


def _git(cwd: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([GIT_CMD, *args], cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)


class GraphRun:
    """One task's trip through the graph. Holds artifacts + node records."""

    def __init__(self, project: str, task: dict, graph: dict, work_dir: str,
                 project_cfg: dict):
        self.project = project
        self.task = task
        self.graph = graph
        self.work_dir = work_dir
        self.project_cfg = project_cfg
        self.llm = LLM(load_roles(graph))
        self.artifacts: dict = {"task": task}
        self.nodes: dict = {}          # name -> record dict
        self.started = time.time()
        self.review_rounds = 0
        self.state_path = RUNS_DIR / f"{task.get('id', 'unknown')}.json"

    # ------------------------------------------------------------- state file
    def save(self, status: str = "running", error: str = ""):
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        doc = {
            "task": {k: self.task.get(k) for k in ("id", "title", "source")},
            "project": self.project,
            "status": status,
            "error": error or None,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started)),
            "wall_s": round(time.time() - self.started, 1),
            "llm_calls": sum(r.get("llm_calls", 0) for r in self.nodes.values()),
            "tokens_in": sum(r.get("tokens_in", 0) for r in self.nodes.values()),
            "tokens_out": sum(r.get("tokens_out", 0) for r in self.nodes.values()),
            "nodes": self.nodes,
            "artifacts": {k: v for k, v in self.artifacts.items()
                          if k in ("intake", "verify", "review", "integrate")},
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2, default=str))
        tmp.rename(self.state_path)

    def _record(self, name: str, started: float, res: Optional[dict] = None,
                skipped: bool = False, error: str = "", summary: str = ""):
        self.nodes[name] = {
            "status": "skipped" if skipped else ("failed" if error else "ok"),
            "duration_ms": int((time.time() - started) * 1000),
            "llm_calls": (res or {}).get("calls", 0),
            "tokens_in": (res or {}).get("tokens_in", 0),
            "tokens_out": (res or {}).get("tokens_out", 0),
            "summary": summary or (res or {}).get("result", "")[:300],
            "error": error or None,
        }
        self.save()

    # ---------------------------------------------------------------- running
    def run(self) -> bool:
        """Walk the DAG. Independent ready nodes run in parallel."""
        nodes = self.graph["nodes"]
        done: set[str] = set()
        pending = dict(nodes)
        self.save()

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {}
            while pending or futures:
                ready = [n for n, cfg in pending.items()
                         if all(d in done for d in (cfg.get("needs") or []))]
                for name in ready:
                    cfg = pending.pop(name)
                    futures[pool.submit(self._run_node, name, cfg)] = name
                if not futures:
                    stuck = ", ".join(pending)
                    self.save("failed", f"graph deadlock — unrunnable nodes: {stuck}")
                    return False
                finished, _ = wait(futures, return_when=FIRST_COMPLETED)
                for fut in finished:
                    name = futures.pop(fut)
                    ok = fut.result()   # exceptions propagate loudly
                    if not ok:
                        self.save("failed", self.nodes.get(name, {}).get("error")
                                  or f"node {name} failed")
                        return False
                    done.add(name)
        self.save("completed")
        return True

    def _run_node(self, name: str, cfg: dict) -> bool:
        started = time.time()
        kind = cfg.get("kind", name)
        runner = getattr(self, f"node_{kind}", None)
        if runner is None:
            self._record(name, started, error=f"unknown node kind: {kind}")
            return False

        if self._should_skip(cfg):
            self._record(name, started, skipped=True,
                         summary=f"skipped ({self.artifacts.get('intake', {}).get('complexity')})")
            return True

        logger.info("[%s] node %s (%s) start", self.task.get("id"), name, kind)
        try:
            runner(name, cfg, started)
            rec = self.nodes.get(name, {})
            logger.info("[%s] node %s done in %.1fs (%d calls)",
                        self.task.get("id"), name,
                        rec.get("duration_ms", 0) / 1000, rec.get("llm_calls", 0))
            return rec.get("status") != "failed"
        except Exception as e:  # noqa: BLE001 - record, fail the run loudly
            logger.exception("[%s] node %s crashed", self.task.get("id"), name)
            self._record(name, started, error=f"{type(e).__name__}: {e}")
            return False

    def _should_skip(self, cfg: dict) -> bool:
        skip_for = cfg.get("skip_for") or []
        intake = self.artifacts.get("intake") or {}
        if intake.get("complexity") in skip_for:
            return True
        if "docs_only" in skip_for and intake.get("docs_only"):
            return True
        return False

    # ------------------------------------------------------------------ nodes
    def node_intake(self, name: str, cfg: dict, started: float):
        rmap = repomap.render(self.work_dir, max_chars=2500)
        self.artifacts["repo_map"] = repomap.render(self.work_dir)
        res = self.llm.text(cfg.get("model", "router"), prompt(
            "node_intake.md",
            TASK_TITLE=self.task.get("title", ""),
            TASK_DESCRIPTION=self.task.get("description", ""),
            REPO_MAP=rmap,
        ), timeout=int(cfg.get("timeout", 120)))
        verdict = extract_json(res.get("result", "")) or {}
        intake = {
            "complexity": verdict.get("complexity", "standard"),
            "docs_only": bool(verdict.get("docs_only", False)),
            "reason": verdict.get("reason", ""),
        }
        if intake["complexity"] not in ("trivial", "standard", "complex"):
            intake["complexity"] = "standard"
        self.artifacts["intake"] = intake
        self._record(name, started, res, summary=json.dumps(intake))

    def node_plan(self, name: str, cfg: dict, started: float):
        res = self.llm.text(cfg.get("model", "planner"), prompt(
            "node_plan.md",
            TASK_TITLE=self.task.get("title", ""),
            TASK_DESCRIPTION=self.task.get("description", ""),
            REPO_MAP=self.artifacts.get("repo_map", ""),
        ), timeout=int(cfg.get("timeout", 300)))
        plan = res.get("result", "").strip()
        self.artifacts["plan"] = plan or "(no plan — implement the task directly)"
        self._record(name, started, res, summary=f"plan: {len(plan)} chars")

    def node_implement(self, name: str, cfg: dict, started: float):
        feedback = self.artifacts.get("review_feedback", "")
        fb_section = (
            f"A previous pass was rejected. Fix every point below without "
            f"regressing what works:\n{feedback}\n\n" if feedback else ""
        )
        system = (
            f"{PERSONA} You work autonomously inside `{self.work_dir}`. Use the "
            "tools to inspect and change files and run commands. Never ask "
            "questions. Paths are relative to the working directory."
        )
        res = self.llm.agent(
            cfg.get("model", "coder"),
            prompt("node_implement.md",
                   TASK_TITLE=self.task.get("title", ""),
                   TASK_DESCRIPTION=self.task.get("description", ""),
                   PLAN=self.artifacts.get("plan", "(none — implement directly)"),
                   FEEDBACK_SECTION=fb_section,
                   REPO_MAP=self.artifacts.get("repo_map", "")),
            system=system,
            cwd=self.work_dir,
            max_turns=int(cfg.get("max_turns", 25)),
            timeout=int(cfg.get("timeout", 900)),
        )
        self.artifacts["implement"] = {
            "summary": res.get("result", ""),
            "changed_files": res.get("changed_files", []),
            "stop_reason": res.get("stop_reason", ""),
        }
        err = ""
        if res.get("is_error"):
            err = f"implement stopped ({res.get('stop_reason')}) with no changes"
        self._record(name, started, res, error=err,
                     summary=f"{res.get('stop_reason')}; changed: "
                             f"{', '.join(res.get('changed_files', [])[:8]) or '(none per tools)'}")

    # ---- verify: deterministic checks + distilled fix loop
    def node_verify(self, name: str, cfg: dict, started: float):
        base = self.project_cfg.get("git", {}).get("base_branch", "main")
        build_cmd = (self.project_cfg.get("build") or {}).get("command")
        test_cmd = (self.project_cfg.get("test") or {}).get("command")

        totals = {"calls": 0, "tokens_in": 0, "tokens_out": 0}
        attempts = 0
        max_fix = int(cfg.get("max_fix_attempts", 2))

        while True:
            report = self._run_checks(base, build_cmd, test_cmd)
            self.artifacts["verify"] = report
            if report["ok"] or not report["has_changes"]:
                break
            if attempts >= max_fix + 1:   # coder retries exhausted + escalation
                self._record(name, started, totals,
                             error="verification still failing after fix loop + escalation",
                             summary=report["summary"])
                return
            model = cfg.get("fix_model", "coder") if attempts < max_fix \
                else cfg.get("escalation_model", "escalation")
            attempts += 1
            logger.info("[%s] verify failed — fix attempt %d (%s)",
                        self.task.get("id"), attempts, model)
            res = self.llm.agent(
                model,
                prompt("node_fix.md",
                       TASK_TITLE=self.task.get("title", ""),
                       ERROR_TAIL=report["error_tail"]),
                system=(f"{PERSONA} You fix build/test failures inside "
                        f"`{self.work_dir}`. Minimal changes only."),
                cwd=self.work_dir,
                max_turns=15,
                timeout=int(cfg.get("timeout", 600)),
            )
            for k in totals:
                totals[k] += res.get(k if k != "calls" else "calls", 0)

        report["fix_attempts"] = attempts
        err = "" if (report["ok"] or not report["has_changes"]) else "unverified"
        # No changes at all is a failure of the implement step worth surfacing:
        if not report["has_changes"]:
            self._record(name, started, totals,
                         error="no changes produced (nothing to verify)",
                         summary="empty diff")
            return
        self._record(name, started, totals, error=err, summary=report["summary"])

    def _run_checks(self, base: str, build_cmd: Optional[str],
                    test_cmd: Optional[str]) -> dict:
        cwd = self.work_dir
        dirty = bool(_git(cwd, "status", "--porcelain").stdout.strip())
        ahead = _git(cwd, "rev-list", "--count", f"{base}..HEAD").stdout.strip()
        has_changes = dirty or (ahead.isdigit() and int(ahead) > 0)

        diff_stat = _git(cwd, "diff", f"{base}...HEAD", "--stat").stdout
        diff_stat += _git(cwd, "diff", "HEAD", "--stat").stdout
        diff_text = _git(cwd, "diff", f"{base}...HEAD").stdout
        diff_text += _git(cwd, "diff", "HEAD").stdout
        self.artifacts["diff"] = {
            "stat": diff_stat.strip()[:2000],
            "text": diff_text[:DIFF_MAX_CHARS],
        }

        problems = []
        syntax_ok, syntax_detail = _syntax_check(cwd, _changed_files(cwd, base))
        if not syntax_ok:
            problems.append(f"SYNTAX:\n{syntax_detail}")
        for label, cmd in (("BUILD", build_cmd), ("TEST", test_cmd)):
            if not cmd:
                continue
            try:
                r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                                   text=True, timeout=300)
                if r.returncode != 0:
                    tail = ((r.stdout or "") + (r.stderr or ""))[-ERROR_TAIL_CHARS:]
                    problems.append(f"{label} `{cmd}` exit {r.returncode}:\n{tail}")
            except subprocess.TimeoutExpired:
                problems.append(f"{label} `{cmd}` timed out (300s)")

        ok = not problems
        return {
            "ok": ok,
            "has_changes": has_changes,
            "summary": ("PASS" if ok else "FAIL") +
                       f" (dirty={dirty}, ahead={ahead or 0}); " +
                       (diff_stat.strip().splitlines()[-1] if diff_stat.strip() else "no diff"),
            "error_tail": "\n\n".join(problems)[-ERROR_TAIL_CHARS:],
        }

    def node_review(self, name: str, cfg: dict, started: float):
        max_rounds = int(cfg.get("max_review_rounds", 1))
        res = self.llm.text(cfg.get("model", "reviewer"), prompt(
            "node_review.md",
            TASK_TITLE=self.task.get("title", ""),
            TASK_DESCRIPTION=self.task.get("description", ""),
            PLAN=(self.artifacts.get("plan") or "(planning was skipped)")[:3000],
            VERIFY_REPORT=self.artifacts.get("verify", {}).get("summary", "(none)"),
            DIFF=self.artifacts.get("diff", {}).get("text", "(no diff captured)"),
        ), timeout=int(cfg.get("timeout", 300)))
        verdict = extract_json(res.get("result", "")) or {"approved": True}
        verdict.setdefault("approved", True)
        self.artifacts["review"] = verdict

        if not verdict["approved"] and self.review_rounds < max_rounds:
            self.review_rounds += 1
            gaps = verdict.get("gaps") or []
            self.artifacts["review_feedback"] = (
                (verdict.get("feedback") or "") +
                ("\n- " + "\n- ".join(str(g) for g in gaps) if gaps else "")
            ).strip() or "The reviewer rejected the change; make it complete and correct."
            logger.info("[%s] review rejected (round %d) — one rebuild pass",
                        self.task.get("id"), self.review_rounds)
            # One bounded rebuild: implement → verify → review again.
            self._record(name, started, res,
                         summary=f"rejected round {self.review_rounds}; rebuilding")
            impl_cfg = self.graph["nodes"].get("implement", {})
            verify_cfg = self.graph["nodes"].get("verify", {})
            self.node_implement("implement", impl_cfg, time.time())
            if self.nodes["implement"]["status"] == "failed":
                self.nodes[name]["status"] = "failed"
                self.nodes[name]["error"] = "rebuild implement failed"
                self.save()
                return
            self.node_verify("verify", verify_cfg, time.time())
            if self.nodes["verify"]["status"] == "failed":
                self.nodes[name]["status"] = "failed"
                self.nodes[name]["error"] = "rebuild verify failed"
                self.save()
                return
            self.node_review(name, cfg, time.time())
            return

        err = "" if verdict["approved"] else \
            f"review rejected after {self.review_rounds} rebuild round(s): " \
            f"{verdict.get('feedback', '')[:200]}"
        self._record(name, started, res, error=err,
                     summary=f"approved={verdict['approved']} score={verdict.get('score')}")

    def node_integrate(self, name: str, cfg: dict, started: float):
        from lib.git_manager import GitManager
        base = self.project_cfg.get("git", {}).get("base_branch", "main")
        strategy = self.project_cfg.get("git", {}).get("strategy", "branch-pr")
        cwd = self.work_dir

        if _git(cwd, "status", "--porcelain").stdout.strip():
            _git(cwd, "add", "-A")
            _git(cwd, "commit", "-m", self.task.get("title", "TARS task"))

        branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        gm = GitManager(self.project_cfg.get("repo", ""), base_branch=base,
                        strategy=strategy)
        pr_url = gm.push_and_pr(branch, self.task.get("title", ""),
                                self.task.get("description", ""))
        self.artifacts["integrate"] = {"branch": branch, "pr_url": pr_url or "direct-push"}
        self._record(name, started, summary=f"pushed {branch} → {pr_url or 'direct-push'}")


# Paths whose contents the task gate must never judge — vendored/generated
# code fails `node --check` legitimately (tars-test's committed node_modules
# sent verify into a 3-round fix loop it could never win).
_VENDORED = ("node_modules/", "vendor/", "dist/", "build/", ".venv/", "venv/",
             "__pycache__/", ".tars/")


def _changed_files(work_dir: str, base: str) -> list[str]:
    """Files this task touched (committed over base + working tree), minus
    vendored paths — the only files verify should parse-check."""
    files: set[str] = set()
    files.update(_git(work_dir, "diff", "--name-only", f"{base}...HEAD").stdout.split())
    for line in _git(work_dir, "status", "--porcelain").stdout.splitlines():
        if line.strip():
            files.add(line[3:].strip())
    return [f for f in sorted(files)
            if f and not any(v in f for v in _VENDORED)]


def _syntax_check(work_dir: str, files: list[str]) -> tuple[bool, str]:
    """Parse-check the given py/js files. Cheap ground truth for 'done'."""
    problems = []
    base = Path(work_dir)
    py = [f for f in files if f.endswith(".py") and (base / f).is_file()]
    if py:
        r = subprocess.run([sys.executable, "-m", "py_compile", *py], cwd=work_dir,
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            problems.append(r.stderr.strip()[-1500:])
    js = [f for f in files if f.endswith(".js") and (base / f).is_file()]
    if js and __import__("shutil").which("node"):
        for f in js[:50]:
            r = subprocess.run(["node", "--check", f], cwd=work_dir,
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                problems.append(r.stderr.strip()[-500:])
    return (not problems), "\n\n".join(problems)


def _notify_website(task: dict, status: str, **fields):
    """Best-effort live status callback to the website for website tasks."""
    survey_id = task.get("survey_task_id")
    url = os.environ.get("TARS_WEBSITE_URL", "").rstrip("/")
    key = os.environ.get("TARS_API_KEY", "")
    if not (survey_id and url and key):
        return
    import urllib.request
    body = json.dumps({"status": status, **fields}).encode()
    req = urllib.request.Request(
        f"{url}/api/tasks/{survey_id}/status", data=body, method="POST",
        headers={"X-API-Key": key, "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=8)
    except Exception:  # noqa: BLE001 - never let the website block a task
        pass


# ------------------------------------------------------------------ lifecycle
def run_task(project: str, task: dict) -> bool:
    """Full lifecycle: workspace prep → graph → Discord. Returns success."""
    from lib.git_manager import GitManager
    from lib.discord_logger import DiscordLogger

    cfg = load_project(project)
    graph = load_graph(cfg)
    task_id = task.get("id", "unknown")

    _notify_website(task, "in_progress")
    gm = GitManager(cfg["repo"], base_branch=cfg["git"]["base_branch"],
                    strategy=cfg["git"]["strategy"])
    work_dir = str(gm.ensure_cloned())
    gm.create_branch(task_id)

    run = GraphRun(project, task, graph, work_dir, cfg)
    ok = run.run()

    try:
        from lib.token_tracker import TokenTracker
        TokenTracker().record(
            sum(r.get("tokens_in", 0) for r in run.nodes.values()),
            sum(r.get("tokens_out", 0) for r in run.nodes.values()), 0.0)
    except Exception:  # noqa: BLE001
        pass

    try:
        from lib.error_analyzer import ErrorAnalyzer
        if ok:
            ErrorAnalyzer().reset_failures(project)
        else:
            ErrorAnalyzer().record_failure(project, task_id)
    except Exception:  # noqa: BLE001
        pass

    if ok:
        _notify_website(task, "completed",
                        pr_url=run.artifacts.get("integrate", {}).get("pr_url", ""),
                        branch_name=run.artifacts.get("integrate", {}).get("branch", ""))
    else:
        _notify_website(task, "failed", error_message=(
            json.loads(run.state_path.read_text()).get("error") or "failed")
            if run.state_path.exists() else "failed")

    # One Discord message per task with the numbers that matter.
    doc = json.loads(run.state_path.read_text()) if run.state_path.exists() else {}
    stats = (f"{doc.get('wall_s', 0):.0f}s, {doc.get('llm_calls', 0)} LLM calls, "
             f"{doc.get('tokens_in', 0)}/{doc.get('tokens_out', 0)} tok")
    try:
        d = DiscordLogger()
        if ok:
            d.log_success(f"{task.get('title')} ({stats})",
                          pr_url=run.artifacts.get("integrate", {}).get("pr_url", ""),
                          project=project)
        else:
            d.log_error(f"{task.get('title')} — {doc.get('error', 'failed')} ({stats})",
                        project=project)
    except Exception:  # noqa: BLE001 - Discord must never fail a task
        logger.warning("Discord notify failed", exc_info=True)
    return ok


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if len(sys.argv) != 3:
        print("usage: python3 -m lib.graph_executor <project> <task_json_file>",
              file=sys.stderr)
        sys.exit(2)
    project = sys.argv[1]
    task = json.loads(Path(sys.argv[2]).read_text())
    sys.exit(0 if run_task(project, task) else 1)


if __name__ == "__main__":
    main()
