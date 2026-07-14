#!/usr/bin/env python3
"""tars — one conversational entry point for TARS.

No subcommands, no mode flags: you just type. A fast local router model
classifies each message — chat / create-task / status / cancel — and acts.
Chat streams instantly; task creation confirms with a one-liner and can show
live per-node pipeline progress as the graph executes.

    ./tars                     interactive REPL
    ./tars what's running?     one-shot: route, act, exit

Talks to the controller REST API (X-API-Key auth) for tasks/status/projects,
and to Ollama directly for routing + streaming chat when reachable (falls
back to the controller's /api/chat/generate when not).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import requests

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))

# ── config ───────────────────────────────────────────────────────────────────

def _conf_defaults() -> dict:
    """tars.conf is bash; source it to get its defaults."""
    conf = TARS_HOME / "tars.conf"
    if not conf.exists():
        return {}
    try:
        out = subprocess.run(
            ["bash", "-c",
             f"source {shlex.quote(str(conf))} 2>/dev/null && "
             "printf '%s\\n%s\\n%s\\n%s\\n' \"$TARS_API_KEY\" "
             "\"$TARS_CONTROLLER_PORT\" \"$OLLAMA_HOST\" \"$OLLAMA_CHAT_MODEL\""],
            capture_output=True, text=True, timeout=5,
        )
        lines = out.stdout.splitlines() + ["", "", "", ""]
        return {"key": lines[0], "port": lines[1] or "8420",
                "ollama": lines[2] or "http://localhost:11434",
                "chat_model": lines[3] or "qwen2.5:7b"}
    except Exception:
        return {}


_D = _conf_defaults()
API_KEY = os.environ.get("TARS_API_KEY") or _D.get("key", "")
CONTROLLER = (os.environ.get("TARS_CONTROLLER_URL")
              or f"http://localhost:{os.environ.get('TARS_CONTROLLER_PORT') or _D.get('port', '8420')}")
OLLAMA = os.environ.get("OLLAMA_HOST") or _D.get("ollama", "http://localhost:11434")
ROUTER_MODEL = os.environ.get("TARS_ROUTER_MODEL") or _D.get("chat_model", "qwen2.5:7b")

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


class ApiError(Exception):
    pass


def api(method: str, path: str, timeout: int = 30, **kwargs):
    url = CONTROLLER.rstrip("/") + path
    headers = {"X-API-Key": API_KEY}
    try:
        resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    except requests.exceptions.ConnectionError as e:
        raise ApiError(f"Can't reach the controller at {CONTROLLER} — is it running?") from e
    except requests.exceptions.Timeout as e:
        raise ApiError(f"{path} timed out") from e
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400:
        raise ApiError(data.get("error") or f"HTTP {resp.status_code} from {path}")
    return data


# ── session state ────────────────────────────────────────────────────────────

class Session:
    def __init__(self):
        self.project = ""              # current project ("" = ask/infer)
        self.history: list[dict] = []  # rolling chat history
        self._status = {}
        self._status_at = 0.0
        self._projects: list[str] = []

    # cached cluster context (5s TTL — cheap prompts, fresh answers)
    def status(self) -> dict:
        if time.time() - self._status_at > 5:
            try:
                self._status = api("GET", "/api/status", timeout=10)
            except ApiError:
                self._status = {}
            self._status_at = time.time()
        return self._status

    def projects(self, refresh: bool = False) -> list[str]:
        if not self._projects or refresh:
            try:
                data = api("GET", "/api/projects", timeout=10)
                self._projects = [p.get("name", "") for p in data.get("projects", [])
                                  if p.get("enabled", True)]
            except ApiError:
                self._projects = []
        return self._projects

    def context_line(self) -> str:
        s = self.status()
        cur = s.get("current_task") or {}
        bits = [f"{s.get('queue_length', '?')} queued"]
        if cur:
            bits.append(f"running: {cur.get('title', '?')[:40]} [{cur.get('project')}]")
        if self.project:
            bits.append(f"project: {self.project}")
        return " · ".join(bits)

    def system_prompt(self) -> str:
        s = self.status()
        cur = s.get("current_task") or {}
        return (
            "You are TARS, an autonomous coding system built by Davis Sneed. "
            "You manage projects, queue coding tasks, and answer questions in a "
            "terminal. Be concise — a few sentences unless asked for more.\n"
            f"Live context: projects={', '.join(self.projects()) or 'none'}; "
            f"queue_length={s.get('queue_length', 0)}; "
            f"running_task={json.dumps(cur) if cur else 'none'}; "
            f"current_project={self.project or 'unset'}."
        )


# ── routing ──────────────────────────────────────────────────────────────────

ROUTE_PROMPT = """You route one user message for TARS, a coding automation system.
Projects: {projects}. Current project: {project}.
{context}Message: {msg}

Reply with ONLY this JSON:
{{"intent": "chat|create_task|task_status|cancel_task", "project": "", "task_ref": "", "title": ""}}

- create_task: the user wants code/work DONE on a project (imperative request
  like "add X", "fix Y", "build Z"). Set project (from message or current) and
  title (short imperative summary).
- task_status: asking what's running/queued/progress/done.
- cancel_task: asking to stop/cancel a task; set task_ref if an id is given.
- chat: everything else (questions, discussion, greetings).
"""


def ollama_json(prompt: str, timeout: int = 20) -> dict:
    r = requests.post(f"{OLLAMA}/api/chat", json={
        "model": ROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False, "format": "json", "keep_alive": "60m",
        "options": {"num_ctx": 4096, "num_predict": 150, "temperature": 0},
    }, timeout=timeout)
    r.raise_for_status()
    return json.loads(r.json().get("message", {}).get("content") or "{}")


_STATUS_RE = re.compile(
    r"^(status|what'?s? (is )?(running|queued|happening|going on)|queue|tasks)\??$", re.I)
_CANCEL_RE = re.compile(r"^(cancel|stop|kill)\s+(?P<id>[\w-]+)\??$", re.I)


def route(sess: Session, msg: str) -> dict:
    """Classify a message. Regex fast-paths first (0ms), then the router model."""
    if _STATUS_RE.match(msg.strip()):
        return {"intent": "task_status"}
    m = _CANCEL_RE.match(msg.strip())
    if m:
        return {"intent": "cancel_task", "task_ref": m.group("id")}
    recent = ""
    if sess.history:
        last = sess.history[-2:]
        recent = "Recent exchange: " + " | ".join(
            f"{h['role']}: {h['content'][:80]}" for h in last) + "\n"
    try:
        verdict = ollama_json(ROUTE_PROMPT.format(
            projects=", ".join(sess.projects()) or "(none)",
            project=sess.project or "(unset)",
            context=recent, msg=json.dumps(msg)))
    except Exception:
        # Router model unreachable → conservative default: chat.
        return {"intent": "chat"}
    if verdict.get("intent") not in ("chat", "create_task", "task_status", "cancel_task"):
        verdict["intent"] = "chat"
    return verdict


# ── actions ──────────────────────────────────────────────────────────────────

def stream_chat(sess: Session, msg: str) -> None:
    """Stream a chat reply token-by-token from Ollama; controller fallback."""
    sess.history.append({"role": "user", "content": msg})
    messages = ([{"role": "system", "content": sess.system_prompt()}]
                + sess.history[-12:])
    reply = ""
    try:
        with requests.post(f"{OLLAMA}/api/chat", json={
            "model": ROUTER_MODEL, "messages": messages, "stream": True,
            "keep_alive": "60m",
            "options": {"num_ctx": 8192, "num_predict": 700, "temperature": 0.4},
        }, stream=True, timeout=120) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                tok = chunk.get("message", {}).get("content", "")
                if tok:
                    print(tok, end="", flush=True)
                    reply += tok
                if chunk.get("done"):
                    break
        print()
    except Exception:
        # Remote CLI / Ollama down → controller does the completion.
        try:
            data = api("POST", "/api/chat/generate", timeout=120,
                       json={"messages": messages[1:],
                             "model": ROUTER_MODEL})
            reply = data.get("reply", "")
            print(reply)
        except ApiError as e:
            print(f"{RED}chat failed: {e}{RESET}")
            sess.history.pop()
            return
    sess.history.append({"role": "assistant", "content": reply})


def create_task(sess: Session, msg: str, verdict: dict) -> None:
    project = verdict.get("project") or sess.project
    projects = sess.projects()
    if project not in projects:
        # Router picked something unknown, or nothing — ask once, inline.
        default = sess.project or (projects[0] if len(projects) == 1 else "")
        hint = f" [{default}]" if default else ""
        try:
            answer = input(f"{YELLOW}Which project?{hint} ({', '.join(projects)}): {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print("(dropped)")
            return
        project = answer or default
        if project not in projects:
            print(f"{RED}Unknown project '{project}' — task not queued.{RESET}")
            return
    title = verdict.get("title") or msg.splitlines()[0][:80]
    try:
        data = api("POST", "/api/tasks", json={
            "project": project, "task_type": "tars-code",
            "description": msg, "title": title})
    except ApiError as e:
        print(f"{RED}{e}{RESET}")
        return
    task = data.get("task", {})
    sess.project = project
    print(f"{GREEN}✓ queued{RESET} {task.get('id')} on {BOLD}{project}{RESET}: {title}")
    print(f"{DIM}  watch {task.get('id')} for live progress · it runs when the daemon picks it up{RESET}")


NODE_ORDER = ["intake", "plan", "implement", "verify", "review", "integrate"]
_GLYPH = {"ok": f"{GREEN}✓{RESET}", "failed": f"{RED}✗{RESET}",
          "skipped": f"{DIM}∅{RESET}"}


def _render_run(doc: dict) -> str:
    parts = []
    nodes = doc.get("nodes", {})
    for name in NODE_ORDER + [n for n in nodes if n not in NODE_ORDER]:
        rec = nodes.get(name)
        if not rec:
            parts.append(f"{DIM}{name}{RESET}")
            continue
        glyph = _GLYPH.get(rec.get("status"), "⋯")
        dur = rec.get("duration_ms", 0) / 1000
        parts.append(f"{name} {glyph}{DIM}{dur:.0f}s{RESET}" if dur >= 1
                     else f"{name} {glyph}")
    return "  ".join(parts)


def watch_task(sess: Session, task_ref: str) -> None:
    """Live per-node progress; Ctrl-C detaches (the task keeps running)."""
    print(f"{DIM}watching {task_ref} — Ctrl-C to detach{RESET}")
    last = ""
    try:
        while True:
            try:
                doc = api("GET", f"/api/runs/{task_ref}", timeout=10)
            except ApiError:
                doc = {}
            if doc:
                line = (f"[{doc.get('status')}] {_render_run(doc)} "
                        f"{DIM}{doc.get('wall_s', 0):.0f}s · "
                        f"{doc.get('llm_calls', 0)} calls{RESET}")
                if line != last:
                    print("\r\033[K" + line, end="", flush=True)
                    last = line
                if doc.get("status") in ("completed", "failed"):
                    print()
                    art = doc.get("artifacts", {})
                    pr = (art.get("integrate") or {}).get("pr_url")
                    if pr:
                        print(f"{GREEN}→ {pr}{RESET}")
                    if doc.get("error"):
                        print(f"{RED}{doc['error']}{RESET}")
                    return
            else:
                print(f"\r\033[K{DIM}queued — waiting for the daemon…{RESET}",
                      end="", flush=True)
            time.sleep(2)
    except KeyboardInterrupt:
        print(f"\n{DIM}detached — task keeps running{RESET}")


def show_status(sess: Session) -> None:
    s = sess.status()
    cur = s.get("current_task") or {}
    if cur:
        started = cur.get("started", 0)
        mins = (time.time() - started) / 60 if started else 0
        print(f"{BOLD}running:{RESET} {cur.get('title')} [{cur.get('project')}] "
              f"{DIM}{mins:.0f}m · id {cur.get('id')}{RESET}")
    else:
        print("nothing running")
    try:
        tasks = api("GET", "/api/tasks?status=pending", timeout=10).get("tasks", [])
    except ApiError:
        tasks = []
    if tasks:
        print(f"{BOLD}queued ({len(tasks)}):{RESET}")
        for t in tasks[:10]:
            print(f"  {DIM}{t.get('id')}{RESET} [{t.get('project')}] {t.get('title', '')[:60]}")
    elif not cur:
        print(f"{DIM}queue is empty{RESET}")


def cancel_task(sess: Session, task_ref: str) -> None:
    if not task_ref:
        print(f"{YELLOW}cancel which task? (see: status){RESET}")
        return
    try:
        api("POST", f"/api/tasks/{task_ref}/cancel")
        print(f"{GREEN}✓ cancelled {task_ref}{RESET}")
    except ApiError as e:
        print(f"{RED}{e}{RESET}")


HELP = f"""{BOLD}TARS{RESET} — just type. Chat, ask what's running, or ask for work:
  {DIM}what's the fastest way to cache the repo map?{RESET}     → chat (streams)
  {DIM}add input validation to the login form in tars-test{RESET} → queues a task
  {DIM}status{RESET} / {DIM}what's running?{RESET}                → live cluster status
  {DIM}cancel <task-id>{RESET}                                  → cancel a queued task
Extras: {DIM}use <project>{RESET} set default · {DIM}watch <task-id>{RESET} live node progress ·
        {DIM}projects{RESET} list · {DIM}clear{RESET} reset chat · {DIM}exit{RESET}
"""


# ── REPL ─────────────────────────────────────────────────────────────────────

def handle(sess: Session, msg: str) -> None:
    """One message → route → act."""
    low = msg.strip().lower()
    if low in ("help", "?"):
        print(HELP)
        return
    if low == "projects":
        print(", ".join(sess.projects(refresh=True)) or "(none)")
        return
    if low == "clear":
        sess.history.clear()
        print(f"{DIM}chat history cleared{RESET}")
        return
    if low.startswith("use "):
        name = msg.strip()[4:].strip()
        if name in sess.projects():
            sess.project = name
            print(f"{GREEN}✓ project set to {name}{RESET}")
        else:
            print(f"{RED}unknown project '{name}' — one of: {', '.join(sess.projects())}{RESET}")
        return
    if low.startswith("watch "):
        watch_task(sess, msg.strip()[6:].strip())
        return

    verdict = route(sess, msg)
    intent = verdict["intent"]
    if intent == "task_status":
        show_status(sess)
    elif intent == "cancel_task":
        cancel_task(sess, verdict.get("task_ref", ""))
    elif intent == "create_task":
        create_task(sess, msg, verdict)
    else:
        stream_chat(sess, msg)


def main():
    sess = Session()
    args = sys.argv[1:]
    if args:                                # one-shot: ./tars what's running?
        handle(sess, " ".join(args))
        return

    try:
        s = sess.status()
        online = s.get("status") == "online"
    except Exception:
        online = False
    dot = f"{GREEN}●{RESET}" if online else f"{RED}●{RESET}"
    print(f"{dot} {BOLD}TARS{RESET} {DIM}· {sess.context_line() or 'controller offline'}"
          f" · type help for hints{RESET}")

    while True:
        try:
            msg = input(f"{CYAN}tars ▸ {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not msg:
            continue
        if msg.lower() in ("exit", "quit", "q"):
            break
        try:
            handle(sess, msg)
        except Exception as e:  # noqa: BLE001 - REPL must survive anything
            print(f"{RED}error: {e}{RESET}")


if __name__ == "__main__":
    main()
