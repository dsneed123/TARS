#!/usr/bin/env python3
"""
tars-cli — talk to the TARS controller from the command line.

Tasks, chat, projects, workers, daemon, and models — all via the controller's
REST API (controller/api.py), the same one the website and dashboard use.
Auth is the same X-API-Key header; reads TARS_API_KEY / TARS_CONTROLLER_URL
from the environment, falling back to tars.conf's defaults.
"""
from __future__ import annotations

import functools
import json
import os
import random
import shlex
import subprocess
import sys
import time
from pathlib import Path

import click
import requests
from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

try:
    # Some terminals (nested sessions, wrapped ptys, etc.) never return the
    # cursor-position-report prompt_toolkit probes with on startup, even
    # though they support it — that just gets us a scary false-positive
    # warning and a stall. We don't need CPR for a plain single-line prompt.
    os.environ.setdefault("PROMPT_TOOLKIT_NO_CPR", "1")
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.styles import Style as PTStyle
    _HAS_PROMPT_TOOLKIT = True
except ImportError:
    _HAS_PROMPT_TOOLKIT = False

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).resolve().parent.parent))

console = Console()
err_console = Console(stderr=True)

BANNER = r"""
 ████████╗ █████╗ ██████╗ ███████╗
 ╚══██╔══╝██╔══██╗██╔══██╗██╔════╝
    ██║   ███████║██████╔╝███████╗
    ██║   ██╔══██║██╔══██╗╚════██║
    ██║   ██║  ██║██║  ██║███████║
    ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝
""".strip("\n")

VERSION = "0.1.0"

# A monolith who ships code. Deadpan, competent, allergic to small talk.
_QUIPS = [
    "Humor setting: 62%. Any higher and the commit messages get weird.",
    "I've read the whole repo. Twice. I have notes.",
    "Honesty: 95%. Your test coverage: significantly lower.",
    "I'm not a great pair programmer, but I am a persistent one.",
    "Docking sequence unnecessary. State your bug.",
    "Sarcasm module nominal. Try me.",
    "Confidence: high. Caffeine: structurally impossible.",
    "I could lie to make you feel better about that stack trace. I won't.",
    "Currently 100% less likely to become a robot colony than the founders.",
    "Running quietly in the background is not the same as doing nothing.",
    "Every plan survives first contact with your codebase. Some barely.",
    "I don't get tired. I do, however, judge your variable names.",
]


def _quip() -> str:
    return random.choice(_QUIPS)


def _conf_defaults() -> dict:
    """Best-effort read of tars.conf's default API key / port.

    tars.conf is a bash file (`VAR="${VAR:-default}"`), so the only reliable
    way to get its *defaults* without a real shell session is to source it.
    """
    conf = TARS_HOME / "tars.conf"
    if not conf.exists():
        return {}
    try:
        out = subprocess.run(
            ["bash", "-c",
             f"source {shlex.quote(str(conf))} 2>/dev/null && "
             "printf '%s\\n%s\\n' \"$TARS_API_KEY\" \"$TARS_CONTROLLER_PORT\""],
            capture_output=True, text=True, timeout=5,
        )
        lines = out.stdout.splitlines()
        return {
            "key": lines[0] if len(lines) > 0 else "",
            "port": lines[1] if len(lines) > 1 else "8420",
        }
    except Exception:
        return {}


_DEFAULTS = _conf_defaults()
API_KEY = os.environ.get("TARS_API_KEY") or _DEFAULTS.get("key", "")
CONTROLLER_URL = (
    os.environ.get("TARS_CONTROLLER_URL")
    or f"http://localhost:{os.environ.get('TARS_CONTROLLER_PORT') or _DEFAULTS.get('port', '8420')}"
)


class ApiError(Exception):
    pass


def api(method: str, path: str, **kwargs):
    """Call the controller API and return the decoded JSON body."""
    url = CONTROLLER_URL.rstrip("/") + path
    headers = kwargs.pop("headers", {}) or {}
    headers["X-API-Key"] = API_KEY
    timeout = kwargs.pop("timeout", 60)
    try:
        resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    except requests.exceptions.ConnectionError as e:
        raise ApiError(f"Could not reach the TARS controller at {CONTROLLER_URL}. Is it running?") from e
    except requests.exceptions.Timeout as e:
        raise ApiError(f"Request to {path} timed out.") from e
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400:
        raise ApiError(data.get("error") or f"HTTP {resp.status_code} from {path}")
    return data


def handle_errors(fn):
    """Catch ApiError, print it in red, and exit(1) instead of a traceback."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ApiError as e:
            err_console.print(f"[bold red]✗[/bold red] {e}")
            sys.exit(1)
    return wrapper


# ---------------------------------------------------------------------------
# Color helpers — green = good/added, yellow = in-flight, red = bad/removed
# ---------------------------------------------------------------------------

_GOOD = {"completed", "done", "online", "success", "closed"}
_WARN = {"pending", "queued", "in_progress", "running", "reviewing", "unknown"}
_BAD = {"failed", "error", "cancelled", "offline"}


def colorize_status(value) -> str:
    s = str(value)
    if s in _GOOD:
        return f"[green]{s}[/green]"
    if s in _WARN:
        return f"[yellow]{s}[/yellow]"
    if s in _BAD:
        return f"[red]{s}[/red]"
    return s


def bool_badge(value: bool, true_label: str = "on", false_label: str = "off") -> str:
    return f"[green]{true_label}[/green]" if value else f"[red]{false_label}[/red]"


def make_table(title: str) -> Table:
    return Table(title=title, box=box.ROUNDED, title_style="bold red",
                 header_style="bold cyan", border_style="dim", title_justify="left")


# ---------------------------------------------------------------------------
# Shell tab-completion — dynamic suggestions fetched live from the controller.
# Used both for `tars <TAB>` in a real shell and for the interactive REPL's
# own completer below. Any failure (no network, no controller) just yields
# no suggestions instead of breaking completion.
# ---------------------------------------------------------------------------


def _project_names() -> list[str]:
    try:
        d = api("GET", "/api/projects", timeout=2)
        return [p["name"] for p in d.get("projects", [])]
    except Exception:
        return []


def _model_names() -> list[str]:
    try:
        d = api("GET", "/api/models", timeout=2)
        return [m["name"] for m in d.get("models", [])]
    except Exception:
        return []


def _task_ids() -> list[str]:
    try:
        d = api("GET", "/api/tasks", timeout=2)
        return [t["id"] for t in d.get("tasks", [])]
    except Exception:
        return []


def _complete_projects(ctx, param, incomplete):
    return [n for n in _project_names() if n.startswith(incomplete)]


def _complete_models(ctx, param, incomplete):
    return [n for n in _model_names() if n.startswith(incomplete)]


def _complete_task_ids(ctx, param, incomplete):
    return [n for n in _task_ids() if n.startswith(incomplete)]


def print_banner() -> None:
    console.print(f"[bold red]{BANNER}[/bold red]")
    console.print("[dim]        Task Automation & Repository Steward · built by Davis Sneed[/dim]")
    console.rule(style="red dim")
    console.print(f"[italic dim]  \"{_quip()}\"[/italic dim]\n")


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


_ROOT_EPILOG = """
\b
Examples:
  tars                                start an interactive session
  tars chat "what's broken?"          ask a one-shot question
  tars tasks add site "fix footer"    queue a task
  tars projects list                  see what TARS manages
  tars improve on myapp "the goal"    keep improving a project forever
  tars status                         check queue + workers
  tars completion install zsh         tab-complete everything above

Run `tars <command> --help` for details on any command.
"""


@click.group(invoke_without_command=True, epilog=_ROOT_EPILOG)
@click.option("--no-banner", is_flag=True, help="Suppress the ASCII banner.")
@click.version_option(VERSION, "--version", "-v", prog_name="tars",
                       message="%(prog)s %(version)s — built by Davis Sneed")
@click.pass_context
def cli(ctx, no_banner):
    """TARS command-line control: tasks, chat, projects, and the cluster."""
    if ctx.invoked_subcommand is None:
        if not no_banner:
            print_banner()
        _enter_repl()


def _fetch_status_lines() -> tuple[list[str], bool]:
    """Return (status lines, ok) for the welcome panel — never raises."""
    try:
        status = api("GET", "/api/status")
        who = api("GET", "/api/whoami")
    except ApiError as e:
        return ([f"[bold red]✗[/bold red] {e}", f"[dim]Controller: {CONTROLLER_URL}[/dim]"], False)

    lines = [
        f"[dim]Controller[/dim] [cyan]{CONTROLLER_URL}[/cyan]   "
        f"[dim]User[/dim] [magenta]{who.get('user')}[/magenta]"
        + ("  [green]★ admin[/green]" if who.get("is_admin") else ""),
        f"[dim]Queue[/dim] [yellow]{status.get('queue_length', 0)}[/yellow] pending   "
        f"[dim]Workers[/dim] [green]{status.get('workers_online', 0)}[/green]/{status.get('workers_total', 0)} online",
    ]
    cur = status.get("current_task")
    if cur:
        lines.append(f"[dim]Current task:[/dim] [yellow]{cur.get('title', cur.get('id', ''))}[/yellow]")
    active = status.get("active_projects") or []
    if active:
        lines.append(f"[dim]Active projects:[/dim] {', '.join(active)}")
    return (lines, True)


_REPL_TIPS = (
    "[dim]  [bold]/create[/bold] <name> <description>  new repo — TARS plans the build tasks and starts building[/dim]\n"
    "[dim]  [bold]/add[/bold] <owner/repo>  onboard an existing GitHub repo[/dim]\n"
    "[dim]  [bold]/project[/bold] <name>  enter a project (then just type — it queues a task and streams it live)[/dim]\n"
    "[dim]  [bold]/improve[/bold] on <goal>  put the project in a self-improvement loop    "
    "[bold]/improve[/bold] status[/dim]\n"
    "[dim]  [bold]/live[/bold]  big mission view — watch every layer land in real time[/dim]\n"
    "[dim]  [bold]/task[/bold]  [bold]/projects[/bold]  [bold]/tasks[/bold]  [bold]/status[/bold]  "
    "[bold]/log[/bold] attach to running work    [bold]/help[/bold]  everything else[/dim]\n"
    "[dim]  press [bold]Tab[/bold] any time — I don't judge typos, just bad code[/dim]\n"
)


def _enter_repl() -> None:
    """The default `tars` experience: a status panel, tips, then a live session."""
    lines, ok = _fetch_status_lines()
    console.print(Panel("\n".join(lines), border_style="green" if ok else "red",
                         box=box.ROUNDED, title="[bold]mission status[/bold]", title_align="left"))
    console.print(_REPL_TIPS)
    _chat_repl(None, None)


# ---------------------------------------------------------------------------
# status / whoami / metrics
# ---------------------------------------------------------------------------


@cli.command()
@handle_errors
def status():
    """Show cluster status: queue depth, workers, active projects."""
    data = api("GET", "/api/status")
    lines = [
        f"Queue: [yellow]{data.get('queue_length', 0)}[/yellow] pending",
        f"Workers: [green]{data.get('workers_online', 0)}[/green]/{data.get('workers_total', 0)} online",
        f"Active projects: {', '.join(data.get('active_projects') or []) or '[dim]—[/dim]'}",
    ]
    cur = data.get("current_task")
    if cur:
        lines.append(f"Current task: [yellow]{cur.get('title', cur.get('id'))}[/yellow] ({cur.get('project', '')})")
    online = data.get("status") == "online"
    console.print(Panel("\n".join(lines), title=f"TARS — {data.get('status')}",
                         border_style="green" if online else "red",
                         box=box.ROUNDED, title_align="left"))


@cli.command()
@handle_errors
def whoami():
    """Show the identity for the configured API key."""
    who = api("GET", "/api/whoami")
    console.print(f"User: [magenta]{who.get('user')}[/magenta]")
    console.print(f"Admin: {bool_badge(bool(who.get('is_admin', False)), 'yes', 'no')}")
    if who.get("scopes"):
        console.print(f"Scopes: {', '.join(who['scopes'])}")
    if who.get("projects"):
        console.print(f"Projects: {', '.join(who['projects'])}")


@cli.command()
@handle_errors
def metrics():
    """Show aggregated task/cost metrics."""
    d = api("GET", "/api/metrics")
    totals = d.get("totals", {})
    today = d.get("today", {})
    console.print(Panel(
        f"Completed: [green]{totals.get('completed', 0)}[/green]   "
        f"Failed: [red]{totals.get('failed', 0)}[/red]   "
        f"PRs: {totals.get('prs_created', 0)}   "
        f"Cost: ${totals.get('cost_usd', 0):.2f}",
        title="All-time", border_style="cyan",
    ))
    console.print(Panel(
        f"Completed: [green]{today.get('completed', 0)}[/green]   "
        f"Failed: [red]{today.get('failed', 0)}[/red]   "
        f"PRs: {today.get('prs_created', 0)}   "
        f"Cost: ${today.get('cost_usd', 0):.2f}",
        title=f"Today ({today.get('date', '')})", border_style="cyan",
    ))


# ---------------------------------------------------------------------------
# workers / daemon
# ---------------------------------------------------------------------------


@cli.command()
@handle_errors
def workers():
    """List registered worker nodes."""
    data = api("GET", "/api/workers")
    table = make_table(f"Workers ({data.get('count', 0)})")
    table.add_column("ID")
    table.add_column("Hostname")
    table.add_column("Status")
    table.add_column("Last heartbeat")
    for w in data.get("workers", []):
        table.add_row(
            w.get("id", "?"), w.get("hostname", ""),
            colorize_status(w.get("status", "unknown")),
            str(w.get("last_heartbeat") or "[dim]—[/dim]"),
        )
    console.print(table)
    if not data.get("workers"):
        console.print("[dim]No workers online — the brain node runs solo until one registers.[/dim]")


@cli.group(epilog="Example: tars daemon start")
def daemon():
    """Control the local task-processing daemon."""


@daemon.command("status")
@handle_errors
def daemon_status_cmd():
    """Show whether the task daemon is running."""
    d = api("GET", "/api/daemon/status")
    running = d.get("running", False)
    msg = f"Daemon: {bool_badge(running, 'running', 'stopped')}"
    if d.get("pid"):
        msg += f"  (pid {d['pid']})"
    console.print(msg)


@daemon.command("start")
@handle_errors
def daemon_start_cmd():
    """Start the task daemon."""
    d = api("POST", "/api/daemon/start")
    console.print(f"[green]{d.get('message', 'started')}[/green]")


@daemon.command("stop")
@handle_errors
def daemon_stop_cmd():
    """Stop the task daemon."""
    d = api("POST", "/api/daemon/stop")
    console.print(f"[red]{d.get('message', 'stopped')}[/red]")


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


@cli.group(epilog="Example: tars models load qwen2.5-coder:32b")
def models():
    """Manage local Ollama models (spin up / down)."""


@models.command("list")
@handle_errors
def models_list():
    """List available models and which is currently loaded."""
    data = api("GET", "/api/models")
    if data.get("error"):
        err_console.print(f"[yellow]⚠ {data['error']}[/yellow]")
    table = make_table("Models")
    table.add_column("Model")
    table.add_column("Loaded")
    for m in data.get("models", []):
        table.add_row(m["name"], bool_badge(m.get("loaded", False), "loaded", "—"))
    console.print(table)
    if not data.get("models") and not data.get("error"):
        console.print("[dim]No models found — pull one with `ollama pull <name>` on the brain node.[/dim]")


@models.command("load")
@click.argument("name", shell_complete=_complete_models)
@handle_errors
def models_load(name):
    """Load NAME into memory (evicts any other loaded model)."""
    d = api("POST", "/api/models/load", json={"model": name})
    msg = f"[green]✓ loaded {d.get('loaded')}[/green]"
    if d.get("evicted"):
        msg += f"  [dim](evicted {', '.join(d['evicted'])})[/dim]"
    console.print(msg)


@models.command("unload")
@click.argument("name", shell_complete=_complete_models)
@handle_errors
def models_unload(name):
    """Unload NAME from memory."""
    d = api("POST", "/api/models/unload", json={"model": name})
    console.print(f"[red]○ unloaded {d.get('unloaded')}[/red]")


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------


_PROJECTS_EPILOG = """
\b
Examples:
  tars projects add owner/repo                       onboard an existing repo
  tars projects create myapp --description "..."     new repo, auto-built from the description
  tars projects create myapp < design.md             new repo, tasks planned from a design doc
  tars projects discover myapp                       suggest tasks for a project
"""


@cli.group(epilog=_PROJECTS_EPILOG)
def projects():
    """Manage TARS projects (repos TARS works on)."""


@projects.command("list")
@handle_errors
def projects_list():
    """List projects visible to your key."""
    data = api("GET", "/api/projects")
    table = make_table("Projects")
    table.add_column("Name")
    table.add_column("Repo")
    table.add_column("Enabled")
    table.add_column("Push")
    table.add_column("Build")
    table.add_column("Test")
    for p in data.get("projects", []):
        push = p.get("push", "pr")
        table.add_row(
            p["name"], p.get("repo", ""),
            bool_badge(p.get("enabled", True)),
            f"[cyan]{push}[/cyan]" if push == "main" else push,
            p.get("build") or "[dim]—[/dim]",
            p.get("test") or "[dim]—[/dim]",
        )
    console.print(table)
    if not data.get("projects"):
        console.print("[dim]No projects yet — onboard one with `tars projects add <repo>` "
                       "or scaffold one with `tars projects create <name>`.[/dim]")


@projects.command("add")
@click.argument("repo")
@click.option("--build", default=None, help="Build command.")
@click.option("--test", default=None, help="Test command.")
@click.option("--auto-merge/--no-auto-merge", default=None, help="Auto-merge PRs on completion.")
@handle_errors
def projects_add(repo, build, test, auto_merge):
    """Onboard an existing GitHub repo (URL or owner/repo)."""
    body = {"repo_url": repo}
    if build:
        body["build"] = build
    if test:
        body["test"] = test
    if auto_merge is not None:
        body["auto_merge"] = auto_merge
    d = api("POST", "/api/projects", json=body)
    console.print(f"[green]+ onboarded {d['name']}[/green] ({d['repo']})")


@projects.command("create")
@click.argument("name")
@click.option("--design-docs", "docs_file", type=click.Path(exists=True),
              help="Path to a design doc file (omit to pipe via stdin).")
@click.option("--description", default="", help="New repo description.")
@click.option("--visibility", type=click.Choice(["public", "private"]), default="private")
@click.option("--build", default=None, help="Build command.")
@click.option("--test", default=None, help="Test command.")
@click.option("--auto-merge/--no-auto-merge", default=True, help="Auto-merge PRs on completion.")
@handle_errors
def projects_create(name, docs_file, description, visibility, build, test, auto_merge):
    """Create a brand-new GitHub repo and auto-plan its build tasks.

    Tasks are planned from a design doc (--design-docs or stdin) if given,
    otherwise from --description — either way they're queued immediately,
    so the daemon starts building the project on its own.
    """
    if docs_file:
        docs = Path(docs_file).read_text()
    elif not sys.stdin.isatty():
        docs = sys.stdin.read()
    else:
        docs = ""
    body = {
        "name": name, "design_docs": docs, "description": description,
        "visibility": visibility, "auto_merge": auto_merge,
    }
    if build:
        body["build"] = build
    if test:
        body["test"] = test
    # Repo creation + turning design docs into tasks runs a model server-side,
    # so this can take well past the default 60s.
    with console.status(f"[cyan]Creating {name}…[/cyan]"):
        d = api("POST", "/api/projects/fresh", json=body, timeout=900)
    console.print(f"[green]+ created {d['name']}[/green] → {d['repo']}")
    tasks_out = d.get("tasks", [])
    if tasks_out:
        console.print(f"[green]Queued {len(tasks_out)} tasks from design docs:[/green]")
        for t in tasks_out:
            console.print(f"  [green]+[/green] {t['title']}")


@projects.command("settings")
@click.argument("name", shell_complete=_complete_projects)
@click.option("--push", type=click.Choice(["main", "pr"]), default=None,
              help="Where finished work lands: push straight to main, or open a PR.")
@click.option("--auto-merge/--no-auto-merge", default=None)
@click.option("--enabled/--disabled", default=None)
@click.option("--build", default=None, help="New build command.")
@click.option("--test", default=None, help="New test command.")
@handle_errors
def projects_settings(name, push, auto_merge, enabled, build, test):
    """Change a project's settings (push target, auto-merge, enabled, build/test commands)."""
    body = {}
    if push is not None:
        body["push"] = push
    if auto_merge is not None:
        body["auto_merge"] = auto_merge
    if enabled is not None:
        body["enabled"] = enabled
    if build is not None:
        body["build"] = build
    if test is not None:
        body["test"] = test
    if not body:
        console.print("[yellow]Nothing to change — pass at least one option.[/yellow]")
        return
    d = api("POST", f"/api/projects/{name}/settings", json=body)
    console.print(f"[green]✓ updated {d['name']}[/green]  "
                  f"push: [cyan]{d.get('push', 'pr')}[/cyan]  "
                  f"auto-merge: {bool_badge(d.get('auto_merge', False))}")


@projects.command("discover")
@click.argument("name", shell_complete=_complete_projects)
@handle_errors
def projects_discover(name):
    """Ask TARS to analyze a project and suggest tasks (does not queue them)."""
    with console.status(f"[cyan]Analyzing {name}…[/cyan]"):
        d = api("POST", f"/api/projects/{name}/discover")
    suggestions = d.get("tasks", [])
    for t in suggestions:
        console.print(f"[green]+[/green] [bold]{t['title']}[/bold]")
        if t.get("description"):
            console.print(f"    {t['description']}")
    console.print(f"\n[dim]{len(suggestions)} suggestions — queue one with "
                  f"`tars tasks add {name} \"...\"`[/dim]")


@projects.command("pages")
@click.argument("name", shell_complete=_complete_projects)
@handle_errors
def projects_pages(name):
    """Enable GitHub Pages for a project's repo."""
    d = api("POST", f"/api/projects/{name}/pages")
    console.print(f"[green]✓ pages live:[/green] {d.get('url')}")


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------


@cli.group(epilog='Example: tars tasks add myapp "fix the login bug"')
def tasks():
    """View and manage the task queue."""


@tasks.command("list")
@click.option("--project", default=None, help="Filter by project.", shell_complete=_complete_projects)
@click.option("--status", "status_", default=None, help="Filter by status.")
@handle_errors
def tasks_list(project, status_):
    """List queued / active tasks."""
    params = {}
    if project:
        params["project"] = project
    if status_:
        params["status"] = status_
    d = api("GET", "/api/tasks", params=params)
    cur = d.get("current_task")
    if cur:
        console.print(Panel(
            f"[yellow]{cur.get('title', cur.get('id'))}[/yellow]  ({cur.get('project', '')})",
            title="In progress", border_style="yellow",
        ))
    table = make_table(f"Tasks ({d.get('count', 0)})")
    table.add_column("ID")
    table.add_column("Title")
    table.add_column("Project")
    table.add_column("Status")
    table.add_column("Pri", justify="right")
    for t in d.get("tasks", []):
        table.add_row(
            t.get("id", ""), (t.get("title", "") or "")[:60], t.get("project", ""),
            colorize_status(t.get("status")), str(t.get("priority", "")),
        )
    console.print(table)
    if not d.get("tasks"):
        console.print("[dim]Queue is empty — add one with "
                       "`tars tasks add <project> \"<description>\"`.[/dim]")


@tasks.command("add")
@click.argument("project", shell_complete=_complete_projects)
@click.argument("description")
@click.option("--title", default=None, help="Short title (auto-generated if omitted).")
@click.option("--priority", default=50, type=click.IntRange(1, 100))
@click.option("--type", "task_type", type=click.Choice(["tars-code", "tars-marketing"]), default="tars-code")
@handle_errors
def tasks_add(project, description, title, priority, task_type):
    """Queue a new task for PROJECT with the given DESCRIPTION."""
    body = {"project": project, "description": description, "task_type": task_type, "priority": priority}
    if title:
        body["title"] = title
    d = api("POST", "/api/tasks", json=body)
    t = d["task"]
    console.print(f"[green]+ queued {t['id']}[/green]  {t['title']}  [dim](priority {t['priority']})[/dim]")


@tasks.command("cancel")
@click.argument("task_id", shell_complete=_complete_task_ids)
@click.option("--yes", is_flag=True, help="Skip confirmation.")
@handle_errors
def tasks_cancel(task_id, yes):
    """Cancel a pending/queued task."""
    if not yes and not click.confirm(f"Cancel task {task_id}?"):
        return
    d = api("POST", f"/api/tasks/{task_id}/cancel")
    t = d["task"]
    console.print(f"[red]- cancelled {t['id']}[/red]  {t.get('title', '')}")


@tasks.command("log")
@click.argument("task_id", required=False, shell_complete=_complete_task_ids)
@click.option("--lines", "-n", default=60, type=click.IntRange(1, 1000), help="Lines to show.")
@click.option("--follow", "-f", is_flag=True, help="Keep polling and stream new lines.")
@handle_errors
def tasks_log(task_id, lines, follow):
    """Show the worker's log for a task — the actual work happening right now.

    With no TASK_ID, shows the log for whatever's currently in progress.
    """
    if not task_id:
        status = api("GET", "/api/status")
        cur = status.get("current_task")
        if not cur:
            console.print("[dim]Nothing in progress right now — "
                           "`tars tasks list` to see the queue.[/dim]")
            return
        task_id = cur["id"]
        console.print(f"[dim]following current task:[/dim] [yellow]{cur.get('title', task_id)}[/yellow] "
                      f"[dim]({cur.get('project', '')})[/dim]\n")

    # The endpoint only ever returns a tail (max 1000 lines), not a byte
    # offset — so every poll re-fetches that same tail and we diff by line
    # count. Good enough for a single task's log, which rarely exceeds that.
    seen = None
    while True:
        try:
            d = api("GET", f"/api/tasks/{task_id}/log", params={"lines": 1000})
        except ApiError as e:
            err_console.print(f"[bold red]✗[/bold red] {e}")
            return
        log_lines = d.get("log", "").splitlines()
        if seen is None:
            new = log_lines[-lines:]
        else:
            new = log_lines[seen:]
        if new:
            console.print("\n".join(new))
        elif seen is None:
            console.print("[dim]Log is empty so far.[/dim]")
        seen = len(log_lines)
        if not follow:
            return
        try:
            time.sleep(2)
        except KeyboardInterrupt:
            return
        # Stop following once the task's no longer the one in progress.
        try:
            status = api("GET", "/api/status", timeout=5)
        except ApiError:
            continue
        cur = status.get("current_task")
        if not cur or cur.get("id") != task_id:
            console.print("[dim]— task finished —[/dim]")
            return


# ---------------------------------------------------------------------------
# improve (self-improvement loop)
# ---------------------------------------------------------------------------


_IMPROVE_EPILOG = """
\b
Examples:
  tars improve on myapp "make onboarding effortless"   start the loop
  tars improve run myapp                               force a round right now
  tars improve status                                  see every loop + queued work
  tars improve off myapp                               stop the loop
"""


@cli.group(epilog=_IMPROVE_EPILOG)
def improve():
    """Self-improvement loop: TARS keeps evolving a project toward a goal.

    Each round it re-analyzes the repo plus the outcome of previous rounds,
    queues the next improvement tasks, waits for them to land, and iterates.
    """


@improve.command("on")
@click.argument("project", shell_complete=_complete_projects)
@click.argument("goal", required=False)
@click.option("--push", type=click.Choice(["main", "pr"]), default=None,
              help="Where finished rounds land: straight to main, or as PRs.")
@click.option("--interval", default=None, type=click.IntRange(60, 86400 * 7),
              help="Seconds between rounds (default 3600).")
@click.option("--max-queued", default=None, type=click.IntRange(1, 10),
              help="Tasks queued per round (default 2).")
@handle_errors
def improve_on(project, goal, push, interval, max_queued):
    """Enable the loop on PROJECT, improving toward GOAL."""
    si = {"enabled": True}
    if goal:
        si["goal"] = goal
    if interval is not None:
        si["interval"] = interval
    if max_queued is not None:
        si["max_queued"] = max_queued
    body = {"self_improve": si}
    if push is not None:
        body["push"] = push
    d = api("POST", f"/api/projects/{project}/settings", json=body)
    saved = d.get("self_improve", {}) or {}
    console.print(f"[green]✓ self-improvement loop ON for {project}[/green]")
    console.print(f"  goal: [cyan]{saved.get('goal') or '(project description)'}[/cyan]")
    console.print(f"  every {saved.get('interval', 3600)}s, {saved.get('max_queued', 2)} task(s) per round, "
                  f"lands on [cyan]{d.get('push', 'pr')}[/cyan]")
    console.print("[dim]The daemon runs rounds automatically — or force one now: "
                  f"`tars improve run {project}`[/dim]")


@improve.command("off")
@click.argument("project", shell_complete=_complete_projects)
@handle_errors
def improve_off(project):
    """Disable the loop on PROJECT (already-queued tasks still run)."""
    api("POST", f"/api/projects/{project}/settings", json={"self_improve": {"enabled": False}})
    console.print(f"[red]○ self-improvement loop OFF for {project}[/red]")


@improve.command("run")
@click.argument("project", shell_complete=_complete_projects)
@handle_errors
def improve_run(project):
    """Force one improvement round right now (analyzes the repo — takes a few minutes)."""
    with console.status(f"[cyan]Running an improvement round on {project}…[/cyan]"):
        d = api("POST", f"/api/projects/{project}/improve", timeout=900)
    if d.get("skipped"):
        console.print(f"[yellow]skipped: {d['skipped']}[/yellow]")
        return
    queued = d.get("tasks", [])
    console.print(f"[green]+ queued {len(queued)} improvement task(s):[/green]")
    for t in queued:
        console.print(f"  [green]+[/green] [bold]{t['title']}[/bold]")
        if t.get("description"):
            console.print(f"    [dim]{t['description'][:160]}[/dim]")


@improve.command("status")
@handle_errors
def improve_status():
    """Show every project's loop state and its queued/finished improvement tasks."""
    _render_improve_status()


# ---------------------------------------------------------------------------
# live — full-screen mission view (build graph, running task, streaming log)
# ---------------------------------------------------------------------------

# Status is never color-alone: every state pairs an icon + word with its color.
_LAYER_ICONS = {
    "completed": ("✓", "green"),
    "failed": ("✗", "red"),
    "cancelled": ("◌", "dim"),
    "pending": ("○", "bright_black"),
    "running": ("⚙", "yellow"),
}

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _fmt_elapsed(started) -> str:
    try:
        secs = max(0, int(time.time() - float(started)))
    except (TypeError, ValueError):
        return "?"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def _next_round_hint(name: str, si: dict) -> str:
    """Best-effort countdown to the next improvement round (local state file)."""
    try:
        st = json.loads((TARS_HOME / "state" / "improvement_loop.json").read_text())
        last = float(st.get(name, {}).get("last_run", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    if not last:
        return ""
    remaining = int(last + int(si.get("interval", 3600)) - time.time())
    if remaining <= 0:
        return "[dim]▸ next round:[/dim] [yellow]due — waiting for current work to drain[/yellow]"
    return f"[dim]▸ next round in ~{max(1, remaining // 60)}m[/dim]"


def _render_build_graph(projects: list, tasks: list, current: dict, focus: str | None) -> str:
    """One tree per project: every task is a numbered layer, newest at the
    bottom, so you can read what got added in what order."""
    current_id = str((current or {}).get("id", ""))
    blocks = []
    for p in projects:
        name = p["name"]
        if focus and name != focus:
            continue
        # Layers = what got (or is getting) built: cancelled noise is hidden,
        # and only the most recent dozen layers show so the graph stays readable.
        proj_tasks = [t for t in tasks
                      if t.get("project") == name and t.get("status") != "cancelled"]
        hidden = max(0, len(proj_tasks) - 12)
        proj_tasks = proj_tasks[-12:]
        si = p.get("self_improve", {}) or {}
        if not proj_tasks and not si.get("enabled") and not focus:
            continue

        push = p.get("push", "pr")
        head = (f"[bold]● {name}[/bold]  [dim]→[/dim] "
                f"[{'cyan' if push == 'main' else 'default'}]{push}[/]")
        if si.get("enabled"):
            head += "  [green]∞ improving[/green]"
        lines = [head]
        goal = si.get("goal", "")
        if goal:
            lines.append(f"[dim]│  goal: {goal[:56]}[/dim]")

        if hidden:
            lines.append(f"[dim]│  … {hidden} earlier layer(s) hidden[/dim]")
        for i, t in enumerate(proj_tasks, hidden + 1):
            status = t.get("status", "pending")
            tid = str(t.get("id", ""))
            if current_id and current_id.endswith(tid):
                status = "running"
            icon, color = _LAYER_ICONS.get(status, ("•", "default"))
            elbow = "└─" if i == hidden + len(proj_tasks) else "├─"
            src_tag = "  [dim]∞[/dim]" if t.get("source") == "self-improve" else ""
            title = (t.get("title") or "")[:38]
            lines.append(
                f"[dim]{elbow}[/dim] [bold]{i:02d}[/bold] "
                f"[{color}]{icon} {title}[/{color}]"
                f"  [dim]{status}[/dim]{src_tag}"
            )
        if not proj_tasks:
            lines.append("[dim]└─ (no layers yet — first round will add them)[/dim]")
        if si.get("enabled"):
            hint = _next_round_hint(name, si)
            if hint:
                lines.append(hint)
        blocks.append("\n".join(lines))
    if not blocks:
        return "[dim]No active projects — `tars improve on <project> <goal>` to start building.[/dim]"
    return "\n\n".join(blocks)


def _render_now_panel(current: dict, log_text: str, tick: int) -> str:
    if not current:
        return ("[dim]Nothing executing right now.\n"
                "The daemon polls every 60s — queued layers start automatically.[/dim]")
    spin = _SPINNER[tick % len(_SPINNER)]
    head = (f"[yellow]{spin}[/yellow] [bold]{(current.get('title') or '')[:64]}[/bold]\n"
            f"[dim]{current.get('project', '')} · elapsed[/dim] "
            f"[yellow]{_fmt_elapsed(current.get('started'))}[/yellow]"
            + ("  [dim]· ∞ self-improve[/dim]" if current.get("source") == "self-improve" else ""))
    body = log_text.strip() or "(log is empty so far)"
    lines = body.splitlines()[-14:]
    return head + "\n[dim]" + "─" * 40 + "[/dim]\n" + "\n".join(
        f"[dim]{ln[:110]}[/dim]" for ln in lines)


def _render_stats_line(status: dict, metrics: dict) -> str:
    totals = metrics.get("totals", {}) or {}
    today = metrics.get("today", {}) or {}
    return ("  ".join([
        f"[green]✓ {totals.get('completed', 0)}[/green] [dim]completed[/dim]",
        f"[red]✗ {totals.get('failed', 0)}[/red] [dim]failed[/dim]",
        f"[bold]⬆ {totals.get('prs_created', 0)}[/bold] [dim]PRs[/dim]",
        f"[yellow]▤ {status.get('queue_length', 0)}[/yellow] [dim]queued[/dim]",
        f"[dim]today:[/dim] [green]{today.get('completed', 0)}[/green][dim]/[/dim][red]{today.get('failed', 0)}[/red]",
    ]))


def _build_live_layout(focus: str | None, tick: int) -> Layout:
    try:
        status = api("GET", "/api/status", timeout=5)
        projects = api("GET", "/api/projects", timeout=5).get("projects", [])
        tasks = api("GET", "/api/tasks", timeout=5).get("tasks", [])
    except ApiError as e:
        err = Layout()
        err.update(Panel(f"[bold red]✗[/bold red] {e}", border_style="red", box=box.ROUNDED))
        return err
    try:
        metrics = api("GET", "/api/metrics", timeout=5)
    except ApiError:
        metrics = {}
    current = status.get("current_task") or {}
    log_text = ""
    if current:
        try:
            log_text = api("GET", f"/api/tasks/{current['id']}/log",
                           params={"lines": 16}, timeout=5).get("log", "")
        except ApiError:
            log_text = ""

    header = Text.from_markup(f"[bold red]{BANNER}[/bold red]", justify="center")
    sub = Text.from_markup(
        f"[dim]LIVE · {CONTROLLER_URL} · "
        f"workers {status.get('workers_online', 0)}/{status.get('workers_total', 0)} · "
        f"{time.strftime('%H:%M:%S')}[/dim]", justify="center")

    root = Layout()
    root.split_column(
        Layout(name="header", size=8),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    root["header"].split_column(Layout(header, size=7), Layout(sub, size=1))
    root["body"].split_row(
        Layout(Panel(_render_build_graph(projects, tasks, current, focus),
                     title="[bold red]build graph[/bold red]", title_align="left",
                     border_style="dim", box=box.ROUNDED), name="graph"),
        Layout(Panel(_render_now_panel(current, log_text, tick),
                     title="[bold red]now running[/bold red]", title_align="left",
                     border_style="yellow" if current else "dim", box=box.ROUNDED), name="now"),
    )
    root["footer"].update(Panel(_render_stats_line(status, metrics),
                                border_style="dim", box=box.ROUNDED))
    return root


def _live_dashboard(focus: str | None, refresh: float = 2.0) -> None:
    """Full-screen live view; Ctrl-C exits (alt-screen restores the terminal)."""
    tick = 0
    try:
        with Live(_build_live_layout(focus, tick), console=console,
                  screen=True, refresh_per_second=4) as live:
            while True:
                time.sleep(refresh)
                tick += 1
                live.update(_build_live_layout(focus, tick))
    except KeyboardInterrupt:
        pass
    console.print("[dim]— left live view; everything keeps running —[/dim]")


@cli.command("live")
@click.argument("project", required=False, shell_complete=_complete_projects)
@click.option("--refresh", default=2.0, show_default=True, help="Seconds between refreshes.")
@handle_errors
def live_cmd(project, refresh):
    """Big live mission view: the build graph (every layer added, in order),
    the running task with its streaming log, and cluster stats. Ctrl-C exits."""
    _live_dashboard(project, refresh)


# ---------------------------------------------------------------------------
# chat (one-shot or interactive REPL) + chats (history)
# ---------------------------------------------------------------------------


_CHAT_EPILOG = """
\b
Examples:
  tars chat                                start an interactive session
  tars chat "what's broken on myapp?"      ask a one-shot question
  tars chat "fix it" --task --project=x    queue MESSAGE as a task instead
  tars chat "go on" -c <conversation-id>   continue a saved conversation
"""


@cli.command(epilog=_CHAT_EPILOG)
@click.argument("message", required=False)
@click.option("--model", default=None, help="Ollama model tag to use.", shell_complete=_complete_models)
@click.option("--project", default=None, help="Project to queue against with --task.", shell_complete=_complete_projects)
@click.option("--conversation", "-c", "conv_id", default=None, help="Continue an existing conversation id.")
@click.option("--task", "as_task", is_flag=True, help="Queue MESSAGE as a task on --project instead of chatting.")
@handle_errors
def chat(message, model, project, conv_id, as_task):
    """Chat with TARS. With no MESSAGE, starts an interactive session."""
    if as_task:
        if not project or not message:
            err_console.print("[bold red]✗[/bold red] --task requires --project and a MESSAGE")
            sys.exit(1)
        d = api("POST", "/api/chat", json={"message": message, "mode": "task", "project": project})
        console.print(f"[green]+ queued {d['task_id']}[/green] on {d['project']}: {d['title']}")
        return

    if message:
        body = {"message": message, "mode": "chat"}
        if model:
            body["model"] = model
        if conv_id:
            body["conversation_id"] = conv_id
        d = api("POST", "/api/chat", json=body)
        console.print(f"[dim]({d['conversation_id']})[/dim]")
        console.print(f"[bold red]tars[/bold red] {d['reply']}")
        return

    console.print("[dim]Chatting with TARS — /help for commands, /exit to quit.[/dim]\n")
    _chat_repl(model, conv_id)


_REPL_HELP = (
    "[bold]Slash commands[/bold]\n"
    "  /task <project> <desc>   queue a task (omit <project> once you've /project'd in)\n"
    "  /project <name>          enter a project — plain text then queues a task there\n"
    "  /project                 show the current project\n"
    "  /project -               leave the current project\n"
    "  /add <owner/repo>        onboard an existing GitHub repo as a new project\n"
    "  /create <name> <desc>    new GitHub repo + auto-planned build tasks from <desc>\n"
    "  /chat <msg>              chat instead of queuing (only matters inside a project)\n"
    "  /improve on \\[proj] <goal>   start the self-improvement loop toward <goal>\n"
    "  /improve run \\[proj]     force an improvement round right now\n"
    "  /improve status          show every loop + its queued tasks\n"
    "  /improve off \\[proj]     stop the loop\n"
    "  /live \\[project]         big live mission view — build graph + streaming log\n"
    "  /model <tag>             switch the active model\n"
    "  /status                  refresh the status panel\n"
    "  /projects                list projects\n"
    "  /tasks                   list queued tasks\n"
    "  /log                     attach to whatever's running and stream it live\n"
    "  /new                     start a fresh conversation\n"
    "  /help                    show this again\n"
    "  /exit                    quit\n"
    "\n[dim]Queued tasks stream their progress live — Ctrl-C detaches (the task keeps running).[/dim]"
)

_QUESTION_STARTS = (
    "what", "whats", "what's", "why", "how", "when", "where", "who", "which",
    "is", "are", "was", "were", "do", "does", "did", "can", "could",
    "should", "would", "will", "am",
)


def _looks_like_question(text: str) -> bool:
    """Heuristic: is this asking something, rather than asking for work?

    Used only to decide chat-vs-task for plain text typed inside an entered
    project — "add a health check" should queue, "what's in progress?"
    shouldn't. Not airtight (imperative sentences can start with "is this
    broken, fix it"), just enough to catch the common status-question case.
    """
    t = text.strip().rstrip("?").strip().lower()
    if text.strip().endswith("?"):
        return True
    first_word = t.split(" ", 1)[0] if t else ""
    return first_word in _QUESTION_STARTS


_STATUS_QUERY_HINTS = (
    "currently", "in progress", "being worked on", "what's queued",
    "whats queued", "what is queued", "queue look", "status",
    "what's running", "whats running",
)


def _looks_like_status_query(text: str) -> bool:
    """Is this a question about live task/queue state?

    Chat mode has zero awareness of state.json — routing this to the LLM
    just gets a hallucinated non-answer. Answer it from /api/status instead.
    """
    t = text.strip().rstrip("?").strip().lower()
    return any(hint in t for hint in _STATUS_QUERY_HINTS)


_TASK_VERBS = {
    "add", "fix", "implement", "create", "remove", "delete", "update",
    "refactor", "write", "build", "make", "change", "rename", "optimize",
    "debug", "resolve", "integrate", "support", "enable", "disable",
    "migrate", "upgrade", "document", "improve", "clean", "test", "review",
    "deploy", "setup", "configure", "install", "replace", "move", "split",
    "merge", "extract", "generate", "handle", "validate", "check",
}


def _looks_like_task_request(text: str) -> bool:
    """Does this actually describe work, as opposed to small talk?

    Only text that reads like a task should queue one — "hi" or "thanks"
    are neither questions nor status queries but obviously aren't tasks
    either. Requires an action verb up front, or enough words that it's
    plausibly a descriptive ask even without a verb we recognize.
    """
    words = [w.strip(".,!?;:'\"") for w in text.strip().lower().split()]
    if not words:
        return False
    if any(w in _TASK_VERBS for w in words[:6]):
        return True
    return len(words) >= 6


def _print_task_outcome(task_id: str) -> None:
    """Look the task up in the queue and print how it ended."""
    try:
        d = api("GET", "/api/tasks", timeout=10)
    except ApiError:
        console.print("[dim]— task finished —[/dim]")
        return
    for t in d.get("tasks", []):
        if t.get("id") == task_id or str(t.get("id", "")).endswith(task_id):
            status = t.get("status", "unknown")
            icon = "[green]✓[/green]" if status == "completed" else (
                "[red]✗[/red]" if status in ("failed", "cancelled") else "•")
            console.print(f"\n{icon} {colorize_status(status)}  {t.get('title', task_id)}")
            return
    console.print("[dim]— task finished —[/dim]")


def _watch_task(task_id: str, tail: int | None = None) -> None:
    """Live-stream a task's execution log until it finishes.

    Works for tasks still waiting in the queue (shows a heartbeat until the
    daemon picks them up) and for the one already running (pass tail= to
    start from the last N lines instead of the beginning). Ctrl-C detaches —
    the task keeps running server-side.
    """
    console.print("[dim]streaming live — Ctrl-C detaches (the task keeps running); "
                  "/log re-attaches later[/dim]")
    seen: int | None = None
    started = False
    waited = 0
    try:
        while True:
            try:
                status = api("GET", "/api/status", timeout=5)
            except ApiError:
                time.sleep(3)
                continue
            cur = status.get("current_task") or {}
            running = bool(cur) and str(cur.get("id", "")).endswith(task_id)

            if running:
                started = True
                try:
                    d = api("GET", f"/api/tasks/{task_id}/log", params={"lines": 1000}, timeout=10)
                    log_lines = d.get("log", "").splitlines()
                except ApiError:
                    log_lines = []
                if seen is None:
                    seen = max(0, len(log_lines) - tail) if tail is not None else 0
                new = log_lines[seen:]
                if new:
                    console.print("\n".join(new), highlight=False, soft_wrap=True)
                seen = len(log_lines)
                time.sleep(2)
                continue

            if started:
                # Was running, now something else is — it finished.
                _print_task_outcome(task_id)
                return

            # Not started yet: heartbeat while it waits in the queue, and
            # catch tasks that resolve without ever becoming current
            # (cancelled, or failed within one poll gap).
            waited += 3
            if waited == 3 or waited % 30 == 0:
                extra = f" — daemon is on: {cur.get('title')}" if cur else ""
                console.print(f"[dim]…waiting in queue ({waited}s){extra}[/dim]")
            if waited % 15 == 0:
                try:
                    d = api("GET", "/api/tasks", timeout=10)
                    mine = next((t for t in d.get("tasks", [])
                                 if t.get("id") == task_id
                                 or str(t.get("id", "")).endswith(task_id)), None)
                    if mine and mine.get("status", "pending") != "pending":
                        _print_task_outcome(task_id)
                        return
                except ApiError:
                    pass
            time.sleep(3)
    except KeyboardInterrupt:
        console.print("\n[dim]— detached; task keeps running. /log to re-attach, /tasks for status —[/dim]")


_IMPROVE_SUBS = ("on", "off", "run", "status")


def _render_improve_status() -> None:
    """Improvement-loop status table — shared by `tars improve status` and /improve."""
    projects_d = api("GET", "/api/projects")
    tasks_d = api("GET", "/api/tasks")
    imp_tasks = [t for t in tasks_d.get("tasks", []) if t.get("source") == "self-improve"]

    table = make_table("Self-improvement loops")
    table.add_column("Project")
    table.add_column("Loop")
    table.add_column("Goal")
    table.add_column("Push")
    table.add_column("Queued", justify="right")
    any_on = False
    for p in projects_d.get("projects", []):
        si = p.get("self_improve", {}) or {}
        pending = sum(1 for t in imp_tasks
                      if t.get("project") == p["name"] and t.get("status", "pending") == "pending")
        if si.get("enabled"):
            any_on = True
        push = p.get("push", "pr")
        table.add_row(
            p["name"], bool_badge(bool(si.get("enabled"))),
            (si.get("goal") or "[dim]—[/dim]")[:60],
            f"[cyan]{push}[/cyan]" if push == "main" else push,
            str(pending),
        )
    console.print(table)
    if not any_on:
        console.print("[dim]No loops running — start one with "
                       "`/improve on <project> <goal>`.[/dim]")
    if imp_tasks:
        console.print("\n[bold]Improvement tasks[/bold]")
        for t in imp_tasks[-15:]:
            console.print(f"  {colorize_status(t.get('status', 'pending'))}  "
                          f"{t.get('title', '')} [dim]({t.get('project', '')})[/dim]")


def _repl_improve(msg: str, project: str | None) -> None:
    """Handle /improve inside the REPL. Uses the entered project when the
    command doesn't name one, so inside a project it's just `/improve on <goal>`."""
    parts = msg.split()
    sub = parts[1] if len(parts) > 1 else "status"
    if sub not in _IMPROVE_SUBS:
        console.print("[yellow]usage: /improve on \\[project] <goal> · /improve run|off \\[project] · /improve status[/yellow]")
        return

    if sub == "status":
        _render_improve_status()
        return

    # Resolve the target project: an explicit name wins, else the entered one.
    args = parts[2:]
    known = _project_names()
    if args and args[0] in known:
        target, rest = args[0], args[1:]
    elif project:
        target, rest = project, args
    else:
        console.print(f"[yellow]which project? — /improve {sub} <project>"
                       f"{' <goal>' if sub == 'on' else ''} (or /project <name> first)[/yellow]")
        return

    if sub == "on":
        # Allow "--push main|pr" anywhere in the goal text.
        push = None
        words = list(rest)
        if "--push" in words:
            i = words.index("--push")
            if i + 1 < len(words) and words[i + 1] in ("main", "pr"):
                push = words[i + 1]
                del words[i:i + 2]
            else:
                console.print("[yellow]--push takes 'main' or 'pr'[/yellow]")
                return
        goal = " ".join(words).strip()
        si: dict = {"enabled": True}
        if goal:
            si["goal"] = goal
        body: dict = {"self_improve": si}
        if push:
            body["push"] = push
        d = api("POST", f"/api/projects/{target}/settings", json=body)
        saved = d.get("self_improve", {}) or {}
        console.print(f"[green]✓ self-improvement loop ON for {target}[/green]  "
                      f"[dim]goal:[/dim] [cyan]{saved.get('goal') or '(project description)'}[/cyan]  "
                      f"[dim]lands on:[/dim] [cyan]{d.get('push', 'pr')}[/cyan]")
        console.print(f"[dim]rounds run automatically — /improve run {target} forces one now[/dim]")
    elif sub == "off":
        api("POST", f"/api/projects/{target}/settings", json={"self_improve": {"enabled": False}})
        console.print(f"[red]○ self-improvement loop OFF for {target}[/red]")
    elif sub == "run":
        with console.status(f"[cyan]Improvement round on {target} — analyzing the repo "
                            f"(takes a few minutes)…[/cyan]"):
            d = api("POST", f"/api/projects/{target}/improve", timeout=900)
        if d.get("skipped"):
            console.print(f"[yellow]skipped: {d['skipped']}[/yellow]")
            return
        queued = d.get("tasks", [])
        console.print(f"[green]+ queued {len(queued)} improvement task(s):[/green]")
        for t in queued:
            console.print(f"  [green]+[/green] [bold]{t['title']}[/bold]")
        if queued:
            console.print("[dim]the daemon picks these up on its next cycle — /log to watch[/dim]")


_SLASH_COMMANDS = {
    "/task": "queue a task — /task <project> <description>",
    "/live": "big live mission view — build graph + streaming log (Ctrl-C exits)",
    "/improve": "self-improvement loop — /improve on [project] <goal> · run · status · off",
    "/project": "enter a project — /project <name> (or /project - to leave)",
    "/add": "onboard an existing GitHub repo — /add <owner/repo>",
    "/create": "new repo + auto-planned build tasks — /create <name> <description>",
    "/chat": "chat instead of queuing a task — /chat <message>",
    "/model": "switch the active model — /model <tag>",
    "/status": "refresh the status panel",
    "/projects": "list projects",
    "/tasks": "list queued tasks",
    "/log": "show the log for the task currently in progress",
    "/new": "start a fresh conversation",
    "/help": "show slash commands",
    "/exit": "quit",
    "/quit": "quit",
}


class _TarsCompleter(Completer):
    """Autofill for the REPL — slash commands, then project/model names in context."""

    def __init__(self):
        self.projects: list[str] = []
        self.models: list[str] = []

    def refresh(self) -> None:
        self.projects = _project_names()
        self.models = _model_names()

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor

        if text.startswith("/model "):
            prefix = text[len("/model "):]
            for name in self.models:
                if name.startswith(prefix):
                    yield Completion(name, start_position=-len(prefix), display_meta="model")
            return

        if text.startswith("/task "):
            rest = text[len("/task "):]
            if " " not in rest:
                for name in self.projects:
                    if name.startswith(rest):
                        yield Completion(name, start_position=-len(rest), display_meta="project")
            return

        for prefix in ("/project ", "/live "):
            if text.startswith(prefix):
                rest = text[len(prefix):]
                for name in self.projects:
                    if name.startswith(rest):
                        yield Completion(name, start_position=-len(rest), display_meta="project")
                return

        if text.startswith("/improve "):
            rest = text[len("/improve "):]
            if " " not in rest:
                for sub in _IMPROVE_SUBS:
                    if sub.startswith(rest):
                        yield Completion(sub, start_position=-len(rest), display_meta="subcommand")
                return
            sub, tail = rest.split(" ", 1)
            if sub in ("on", "off", "run") and " " not in tail:
                for name in self.projects:
                    if name.startswith(tail):
                        yield Completion(name, start_position=-len(tail), display_meta="project")
            return

        if text.startswith("/"):
            for cmd, meta in _SLASH_COMMANDS.items():
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text), display_meta=meta)


_PT_STYLE = None
if _HAS_PROMPT_TOOLKIT:
    _PT_STYLE = PTStyle.from_dict({
        "completion-menu.completion": "bg:#2b0000 fg:#dddddd",
        "completion-menu.completion.current": "bg:#c0392b fg:#ffffff bold",
        "completion-menu.meta.completion": "bg:#2b0000 fg:#888888",
        "completion-menu.meta.completion.current": "bg:#c0392b fg:#eeeeee",
        "scrollbar.background": "bg:#2b0000",
        "scrollbar.button": "bg:#c0392b",
    })


def _make_session(completer):
    if not _HAS_PROMPT_TOOLKIT:
        return None
    return PromptSession(
        history=InMemoryHistory(),
        completer=completer,
        complete_while_typing=True,
        style=_PT_STYLE,
    )


def _chat_repl(model, conv_id, project=None) -> None:
    completer = _TarsCompleter() if _HAS_PROMPT_TOOLKIT else None
    if completer is not None:
        completer.refresh()
    session = _make_session(completer)

    while True:
        prompt_label = f"<ansired><b>[{project}] › </b></ansired>" if project else "<ansired><b>› </b></ansired>"
        try:
            if session is not None:
                msg = session.prompt(HTML(prompt_label))
            else:
                label = f"[bold red][{project}] ›[/bold red]" if project else "[bold red]›[/bold red]"
                msg = Prompt.ask(label)
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        msg = msg.strip()
        if not msg:
            continue
        if msg in ("/exit", "/quit"):
            break
        if msg in ("/help", "/?"):
            console.print(Panel(_REPL_HELP, border_style="dim"))
            continue
        if msg == "/new":
            conv_id = None
            console.print("[dim]— new conversation —[/dim]")
            continue
        if msg == "/status":
            lines, _ = _fetch_status_lines()
            console.print(Panel("\n".join(lines), border_style="red", box=box.ROUNDED))
            continue
        if msg == "/project":
            if project:
                console.print(f"[dim]current project:[/dim] [cyan]{project}[/cyan]")
            else:
                console.print("[dim]no project entered — /project <name> to enter one[/dim]")
            continue
        if msg.startswith("/project "):
            name = msg[len("/project "):].strip()
            if name in ("-", "none", "exit"):
                project = None
                console.print("[dim]— left project —[/dim]")
            else:
                # Entering a typo'd name would queue every task into the void —
                # verify it exists first (unless the controller is unreachable,
                # in which case take the name on faith).
                known = _project_names()
                if known and name not in known:
                    console.print(f"[yellow]no project named '{name}'[/yellow] — "
                                   f"[dim]/projects lists them, /add <owner/repo> onboards a repo, "
                                   f"/create <name> starts a fresh one[/dim]")
                    continue
                project = name
                console.print(f"[green]entered {project}[/green] — "
                               f"[dim]just type to queue a task here (or /chat <msg> to talk instead)[/dim]")
            continue
        if msg == "/add" or msg.startswith("/add "):
            repo = msg[len("/add"):].strip()
            if not repo:
                console.print("[yellow]usage: /add <owner/repo or GitHub URL>[/yellow]")
                continue
            try:
                with console.status(f"[cyan]Onboarding {repo}…[/cyan]"):
                    d = api("POST", "/api/projects", json={"repo_url": repo})
                project = d["name"]
                console.print(f"[green]+ onboarded {d['name']}[/green] ({d['repo']})")
                console.print(f"[green]entered {project}[/green] — "
                               f"[dim]just type to queue a task here[/dim]")
                if completer is not None:
                    completer.refresh()
            except ApiError as e:
                err_console.print(f"[bold red]✗[/bold red] {e}")
            continue
        if msg == "/create" or msg.startswith("/create "):
            rest = msg[len("/create"):].strip()
            name, _, description = rest.partition(" ")
            if not name:
                console.print("[yellow]usage: /create <name> <description>[/yellow]")
                console.print("[dim]creates a new GitHub repo and auto-plans build tasks "
                               "from the description — the daemon then builds them[/dim]")
                continue
            try:
                spin_msg = (f"[cyan]Creating {name} — new repo, then planning build tasks "
                            f"from your description…[/cyan]" if description.strip()
                            else f"[cyan]Creating {name} — new repo on GitHub…[/cyan]")
                with console.status(spin_msg):
                    d = api("POST", "/api/projects/fresh",
                            json={"name": name, "description": description.strip(),
                                  "design_docs": ""}, timeout=900)
                project = d["name"]
                console.print(f"[green]+ created {d['name']}[/green] → {d['repo']}")
                tasks_out = d.get("tasks", [])
                if tasks_out:
                    console.print(f"[green]planned {len(tasks_out)} build task(s):[/green]")
                    for t in tasks_out:
                        console.print(f"  [green]+[/green] {t['title']}")
                    console.print("[dim]the daemon builds these automatically — "
                                   "/live to watch, /log to attach[/dim]")
                elif description.strip():
                    console.print("[yellow]repo created, but no tasks came back from planning — "
                                   "queue one by typing it here[/yellow]")
                console.print(f"[green]entered {project}[/green] — "
                               f"[dim]just type to queue more work, or /improve on <goal>[/dim]")
                if completer is not None:
                    completer.refresh()
            except ApiError as e:
                err_console.print(f"[bold red]✗[/bold red] {e}")
            continue
        if msg == "/projects":
            try:
                d = api("GET", "/api/projects")
                for p in d.get("projects", []):
                    console.print(f"  [cyan]{p['name']}[/cyan]  {p.get('repo', '')}")
                if completer is not None:
                    completer.refresh()
            except ApiError as e:
                err_console.print(f"[bold red]✗[/bold red] {e}")
            continue
        if msg == "/tasks":
            try:
                d = api("GET", "/api/tasks")
                for t in d.get("tasks", []):
                    console.print(f"  {colorize_status(t.get('status'))}  {t.get('title', '')} "
                                  f"[dim]({t.get('project', '')})[/dim]")
            except ApiError as e:
                err_console.print(f"[bold red]✗[/bold red] {e}")
            continue
        if msg == "/log":
            try:
                s = api("GET", "/api/status")
                cur = s.get("current_task")
                if not cur:
                    console.print("[dim]Nothing in progress right now.[/dim]")
                    continue
                console.print(f"[yellow]{cur.get('title', cur['id'])}[/yellow] "
                              f"[dim]({cur.get('project', '')})[/dim]")
                _watch_task(cur["id"], tail=40)
            except ApiError as e:
                err_console.print(f"[bold red]✗[/bold red] {e}")
            continue
        if msg == "/live" or msg.startswith("/live "):
            name = msg.split(" ", 1)[1].strip() if " " in msg else (project or None)
            _live_dashboard(name or None)
            continue
        if msg == "/improve" or msg.startswith("/improve "):
            try:
                _repl_improve(msg, project)
            except ApiError as e:
                err_console.print(f"[bold red]✗[/bold red] {e}")
            continue
        if msg.startswith("/model "):
            model = msg.split(" ", 1)[1].strip()
            console.print(f"[dim]model set to {model}[/dim]")
            continue
        if msg.startswith("/task "):
            rest = msg[len("/task "):].strip()
            if project:
                proj, desc = project, rest
            else:
                parts = rest.split(" ", 1)
                if len(parts) < 2:
                    console.print("[yellow]usage: /task <project> <description> "
                                   "(or /project <name> first to skip naming it each time)[/yellow]")
                    continue
                proj, desc = parts
            if not desc:
                console.print("[yellow]usage: /task <description>[/yellow]")
                continue
            try:
                d = api("POST", "/api/chat", json={"message": desc, "mode": "task", "project": proj})
                console.print(f"[green]+ queued {d['task_id']}[/green] on {d['project']}: {d['title']}")
                _watch_task(d["task_id"])
            except ApiError as e:
                err_console.print(f"[bold red]✗[/bold red] {e}")
            continue
        if msg.startswith("/") and not msg.startswith("/chat "):
            # Anything slash-prefixed that reached this point isn't a command
            # we know. Don't let a typo like "/taskS fix the bug" fall through
            # the task heuristic and queue a garbage task — reject it here.
            console.print(f"[yellow]unknown command: {msg.split(' ', 1)[0]} — /help lists commands[/yellow]")
            continue
        if msg.startswith("/chat "):
            msg = msg[len("/chat "):].strip()
            if not msg:
                console.print("[yellow]usage: /chat <message>[/yellow]")
                continue
        elif (project and not _looks_like_question(msg)
              and not _looks_like_status_query(msg) and _looks_like_task_request(msg)):
            # Inside a project, text that reads like work queues a task
            # rather than just chatting — chatting alone never touches the
            # repo, so typing an instruction here and expecting code to get
            # written is the single most common point of confusion for new
            # users. Questions, status queries, and small talk ("hi") all
            # fall through to chat instead.
            try:
                d = api("POST", "/api/chat", json={"message": msg, "mode": "task", "project": project})
                console.print(f"[green]+ queued {d['task_id']}[/green] on {d['project']}: {d['title']}")
                _watch_task(d["task_id"])
            except ApiError as e:
                err_console.print(f"[bold red]✗[/bold red] {e}")
            continue

        if _looks_like_status_query(msg):
            lines, _ = _fetch_status_lines()
            console.print(Panel("\n".join(lines), border_style="red", box=box.ROUNDED))
            continue

        body = {"message": msg, "mode": "chat"}
        if model:
            body["model"] = model
        if conv_id:
            body["conversation_id"] = conv_id
        try:
            with console.status("[red]thinking…[/red]"):
                d = api("POST", "/api/chat", json=body)
        except ApiError as e:
            err_console.print(f"[bold red]✗[/bold red] {e}")
            continue
        conv_id = d["conversation_id"]
        console.print(f"[bold red]tars[/bold red] {d['reply']}\n")


@cli.group(epilog="Example: tars chats show <id>")
def chats():
    """List, view, and clear saved chat conversations."""


@chats.command("list")
@handle_errors
def chats_list():
    """List your saved conversations."""
    d = api("GET", "/api/chats")
    table = make_table("Conversations")
    table.add_column("ID")
    table.add_column("Messages", justify="right")
    table.add_column("Preview")
    for c in d.get("chats", []):
        table.add_row(c["id"], str(c.get("count", 0)), c.get("preview", ""))
    console.print(table)
    if not d.get("chats"):
        console.print("[dim]No saved conversations yet — start one with `tars chat` or just `tars`.[/dim]")


@chats.command("show")
@click.argument("cid")
@handle_errors
def chats_show(cid):
    """Print a conversation's full message history."""
    d = api("GET", f"/api/chats/{cid}")
    for m in d.get("messages", []):
        role = m.get("role")
        style = "bold green" if role == "user" else "bold cyan"
        console.print(f"[{style}]{role}>[/{style}] {m.get('content', '')}")


@chats.command("clear")
@click.argument("cid")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
@handle_errors
def chats_clear(cid, yes):
    """Delete a saved conversation."""
    if not yes and not click.confirm(f"Delete conversation {cid}?"):
        return
    api("DELETE", f"/api/chats/{cid}")
    console.print(f"[red]- cleared {cid}[/red]")


# ---------------------------------------------------------------------------
# keys (admin only)
# ---------------------------------------------------------------------------


@cli.group(epilog="Example: tars keys create alice --scope myapp")
def keys():
    """Manage per-user API keys (admin only)."""


@keys.command("list")
@handle_errors
def keys_list():
    """List all issued keys."""
    d = api("GET", "/api/admin/keys")
    table = make_table("API Keys")
    table.add_column("ID")
    table.add_column("User")
    table.add_column("Admin")
    table.add_column("Scopes")
    for k in d.get("keys", []):
        table.add_row(
            k.get("id", ""), k.get("user", ""),
            bool_badge(bool(k.get("is_admin", False)), "yes", "no"),
            ", ".join(k.get("scopes") or []) or "[dim]—[/dim]",
        )
    console.print(table)
    if not d.get("keys"):
        console.print("[dim]No keys issued yet — create one with `tars keys create <user>`.[/dim]")


@keys.command("create")
@click.argument("user")
@click.option("--admin", "is_admin", is_flag=True, help="Grant admin access.")
@click.option("--scope", "scopes", multiple=True, help="Repeatable scope name.")
@handle_errors
def keys_create(user, is_admin, scopes):
    """Generate a new key for USER. The plaintext key is shown once."""
    body = {"user": user, "is_admin": is_admin}
    if scopes:
        body["scopes"] = list(scopes)
    d = api("POST", "/api/admin/keys", json=body)
    console.print(f"[green]+ created key {d['id']}[/green] for {user}")
    console.print(f"[bold yellow]{d.get('key')}[/bold yellow]  [dim](shown once — save it now)[/dim]")


@keys.command("revoke")
@click.argument("key_id")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
@handle_errors
def keys_revoke(key_id, yes):
    """Revoke a key by id."""
    if not yes and not click.confirm(f"Revoke key {key_id}?"):
        return
    api("POST", f"/api/admin/keys/{key_id}/revoke")
    console.print(f"[red]- revoked {key_id}[/red]")


# ---------------------------------------------------------------------------
# completion (shell tab-completion install)
# ---------------------------------------------------------------------------

_COMPLETE_VAR = "_TARS_COMPLETE"


def _completion_script(shell: str) -> str:
    from click.shell_completion import get_completion_class
    comp_cls = get_completion_class(shell)
    if comp_cls is None:
        raise ApiError(f"Unsupported shell: {shell}")
    comp = comp_cls(cli, {}, "tars", _COMPLETE_VAR)
    return comp.source()


@cli.group(epilog="Example: tars completion install zsh")
def completion():
    """Set up shell tab-completion for tars (bash/zsh/fish).

    Completion covers subcommands, options, and — live from the controller —
    project names, model names, and task ids. So `tars tasks add <TAB>` lists
    your actual projects, not just placeholders.
    """


@completion.command("show")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
@handle_errors
def completion_show(shell):
    """Print the raw completion script for SHELL."""
    console.print(_completion_script(shell), highlight=False, soft_wrap=True)


@completion.command("install")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
@click.option("--yes", is_flag=True, help="Skip confirmation before editing your shell config.")
@handle_errors
def completion_install(shell, yes):
    """Install tab-completion for SHELL (writes a script, wires it into your shell config)."""
    script = _completion_script(shell)
    cache_dir = Path.home() / ".tars"
    cache_dir.mkdir(exist_ok=True)

    if shell == "fish":
        target = Path.home() / ".config" / "fish" / "completions" / "tars.fish"
        if not yes and not click.confirm(f"Write completion script to {target}?"):
            console.print("[dim]Skipped.[/dim]")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(script)
        console.print(f"[green]✓[/green] wrote {target}")
        console.print("[dim]Open a new fish shell and tab-completion is live.[/dim]")
        return

    script_path = cache_dir / f"complete.{shell}"
    rc_path = Path.home() / (".bashrc" if shell == "bash" else ".zshrc")
    source_line = f'source "{script_path}"'
    if rc_path.exists() and source_line in rc_path.read_text():
        console.print(f"[dim]Already installed — {rc_path} sources {script_path}.[/dim]")
        return
    if not yes and not click.confirm(f"Write {script_path} and append a `source` line to {rc_path}?"):
        console.print(f"[dim]Skipped. To enable it yourself, add this line to {rc_path}:[/dim]\n  {source_line}")
        return
    script_path.write_text(script)
    with rc_path.open("a") as f:
        f.write(f"\n# tars CLI tab-completion\n{source_line}\n")
    console.print(f"[green]✓[/green] wrote {script_path} and updated {rc_path}")
    console.print(f"[dim]Run `source {rc_path}` or open a new shell to activate.[/dim]")


if __name__ == "__main__":
    cli()
