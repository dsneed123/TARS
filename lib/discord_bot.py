"""TARS Discord bot — bidirectional control, chat mode, and mode commands."""

import asyncio
import base64
import io
import json
import logging
import os
import signal
import subprocess
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
import yaml

# Ensure TARS_HOME is set and lib/ is importable
TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))
sys.path.insert(0, str(TARS_HOME))
os.environ.setdefault("TARS_HOME", str(TARS_HOME))

from lib.config_loader import load_discord_config, load_queue, list_projects, load_project
from lib.token_tracker import TokenTracker
from lib.metrics import MetricsTracker
from lib.error_analyzer import ErrorAnalyzer
from lib.chat_engine import ChatEngine
from lib.discord_modes import parse_modes, run_mode_pipeline, MODES
from lib.image_handler import ImageHandler

logger = logging.getLogger("tars.discord_bot")

# Embed colors
COLOR_SUCCESS = 0x2ECC71
COLOR_WARNING = 0xF39C12
COLOR_ERROR = 0xE74C3C
COLOR_INFO = 0x3498DB
COLOR_SUMMARY = 0x9B59B6

STATE_DIR = TARS_HOME / "state"
LOGS_DIR = TARS_HOME / "logs"
CONFIG_DIR = TARS_HOME / "config"


def load_bot_config() -> dict:
    """Load Discord bot config from discord.yaml."""
    return load_discord_config()


class TARSBot(commands.Bot):
    """TARS Discord bot with command handlers, chat mode, and mode commands."""

    def __init__(self, config: dict):
        self.config = config
        self.servers = config.get("servers", {})
        prefix = config.get("command_prefix", "!")

        # Legacy single-channel support
        self._legacy_channel_id = config.get("command_channel_id")

        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(command_prefix=prefix, intents=intents)

        # Chat engine
        self.chat_engine = ChatEngine(
            model=config.get("chat_model", "sonnet"),
            max_tokens=config.get("chat_max_tokens", 4096),
        )

        # Image handlers (lazily created per project)
        self._image_handlers: dict[str, ImageHandler] = {}

        # System prompt cache (per server guild_id)
        self._system_prompts: dict[str, str] = {}

        self._register_commands()

    # ------------------------------------------------------------------
    # Server context resolution
    # ------------------------------------------------------------------

    def _get_server_context(self, guild_id: int) -> Optional[dict]:
        """Get server configuration for a guild. Returns None if not configured."""
        return self.servers.get(str(guild_id))

    def _check_command_channel(self, ctx: commands.Context) -> bool:
        """Check if command was sent in an allowed channel."""
        if not ctx.guild:
            return False

        # Multi-server path
        if self.servers:
            server = self._get_server_context(ctx.guild.id)
            if not server:
                return False
            cmd_channels = server.get("command_channels", [])
            chat_channels = server.get("chat_channels", [])
            # Commands work in both command channels and chat channels
            allowed = cmd_channels + chat_channels
            if not allowed:
                return True  # No restriction = allow everywhere
            return str(ctx.channel.id) in allowed

        # Legacy single-channel path
        if self._legacy_channel_id:
            return str(ctx.channel.id) == str(self._legacy_channel_id)
        return True

    def _is_admin_server(self, guild_id: int) -> bool:
        """Check if this server has admin access."""
        if not self.servers:
            return True  # Legacy mode = admin
        server = self._get_server_context(guild_id)
        if not server:
            return False
        return server.get("mode") == "admin"

    def _get_scoped_project(self, guild_id: int) -> Optional[str]:
        """Get the project this server is scoped to. None = all projects."""
        server = self._get_server_context(guild_id)
        if not server:
            return None
        return server.get("project")

    def _get_image_handler(self, project: str) -> ImageHandler:
        """Get or create an ImageHandler for a project."""
        if project not in self._image_handlers:
            self._image_handlers[project] = ImageHandler(project)
        return self._image_handlers[project]

    def _get_system_prompt(self, guild_id: int) -> str:
        """Get or build the system prompt for a server."""
        key = str(guild_id)
        if key not in self._system_prompts:
            server = self._get_server_context(guild_id) or {}
            project_name = server.get("project")
            project_config = {}
            if project_name:
                try:
                    project_config = load_project(project_name)
                except FileNotFoundError:
                    pass
            self._system_prompts[key] = self.chat_engine.load_system_prompt(
                server, project_config
            )
        return self._system_prompts[key]

    # ------------------------------------------------------------------
    # Daemon management
    # ------------------------------------------------------------------

    async def _ensure_daemon_running(self, project: Optional[str] = None) -> bool:
        """Start the TARS daemon if not already running.

        If project is specified, sets active_projects so the daemon only
        works on that project. Returns True if daemon was started, False
        if already running.
        """
        pid_file = STATE_DIR / "daemon.pid"

        # Check if already running
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                return False  # Already running
            except (ProcessLookupError, ValueError):
                pid_file.unlink(missing_ok=True)

        # Scope daemon to this project only
        if project:
            import json
            active_projects_file = STATE_DIR / "active_projects.json"
            active_projects_file.write_text(json.dumps({"active": [project]}))

        # Start daemon
        daemon_script = TARS_HOME / "bin" / "tars-daemon.sh"
        log_file = LOGS_DIR / "daemon.log"
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        with open(log_file, "a") as lf:
            proc = subprocess.Popen(
                [str(daemon_script)],
                stdout=lf, stderr=lf,
                start_new_session=True,
                cwd=str(TARS_HOME),
                env={**os.environ, "TARS_HOME": str(TARS_HOME)},
            )
        pid_file.write_text(str(proc.pid))
        logger.info("Daemon started (PID %d) for project: %s", proc.pid, project or "all")
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _error_embed(self, message: str) -> discord.Embed:
        """Create a standard error embed."""
        return discord.Embed(title="Error", description=message, color=COLOR_ERROR)

    async def _send_long_message(self, channel, text: str, max_len: int = 1950):
        """Send a message, splitting into chunks if it exceeds Discord's limit."""
        if not text:
            return
        if len(text) <= max_len:
            await channel.send(text)
            return

        while text:
            if len(text) <= max_len:
                await channel.send(text)
                break
            # Find a good split point
            split_at = text.rfind("\n\n", 0, max_len)
            if split_at == -1:
                split_at = text.rfind("\n", 0, max_len)
            if split_at == -1:
                split_at = text.rfind(". ", 0, max_len)
            if split_at == -1:
                split_at = max_len
            else:
                split_at += 1
            await channel.send(text[:split_at])
            text = text[split_at:].lstrip()

    # ------------------------------------------------------------------
    # on_message — chat mode + mode commands
    # ------------------------------------------------------------------

    async def on_message(self, message: discord.Message):
        """Handle all incoming messages: commands, mode commands, and chat."""
        # Ignore own messages and DMs
        if message.author == self.user or message.author.bot:
            return
        if not message.guild:
            return

        # Process registered commands first (e.g. !status, !queue, !clear)
        await self.process_commands(message)

        # If it was a registered command, don't also process as chat/mode
        ctx = await self.get_context(message)
        if ctx.valid:
            return

        # Check if this guild is configured
        server = self._get_server_context(message.guild.id)

        # Legacy mode: no servers configured, ignore non-command messages
        if not self.servers:
            return
        if not server:
            return

        content = message.content.strip()
        chat_channels = server.get("chat_channels", [])
        is_chat_channel = str(message.channel.id) in chat_channels

        # Parse mode prefixes
        modes, prompt = parse_modes(content)

        # If no modes detected and not in a chat channel, ignore
        if not modes and not is_chat_channel:
            return

        # If in a chat channel with no modes and no content, check for image-only messages
        if not prompt and not message.attachments:
            return

        # Default to chat mode in chat channels
        if not modes:
            modes = {"chat"}

        # If just an image with no text, set a default prompt
        if not prompt and message.attachments:
            prompt = "I've uploaded an image."

        async with message.channel.typing():
            system_prompt = self._get_system_prompt(message.guild.id)
            project = server.get("project")

            # Handle image attachments
            image_infos = []
            if message.attachments and project:
                handler = self._get_image_handler(project)
                for attachment in message.attachments:
                    ct = attachment.content_type or ""
                    if ct.startswith("image/"):
                        info = await handler.download_attachment(attachment)
                        if info:
                            image_infos.append(info)

            # Add image context to prompt
            if image_infos:
                img_context = "\n".join(
                    f"[Attached image: {info['filename']} saved to {info['web_path']}]"
                    for info in image_infos
                )
                prompt = f"{prompt}\n\n{img_context}"

            # Run the mode pipeline
            result = await run_mode_pipeline(
                modes=modes,
                prompt=prompt,
                chat_engine=self.chat_engine,
                channel_id=str(message.channel.id),
                server_config=server,
                system_prompt=system_prompt,
                username=message.author.display_name,
            )

            # Send text response
            if result["text"]:
                await self._send_long_message(message.channel, result["text"])

            # Upload PDF if generated
            if result["pdf_bytes"]:
                safe_name = prompt[:40].replace(" ", "-").lower()
                safe_name = "".join(c for c in safe_name if c.isalnum() or c == "-")
                filename = f"tars-{safe_name or 'report'}.pdf"
                file = discord.File(
                    io.BytesIO(result["pdf_bytes"]),
                    filename=filename,
                )
                await message.channel.send(
                    embed=discord.Embed(
                        title="PDF Generated",
                        description=f"Here's your document: **{filename}**",
                        color=COLOR_SUCCESS,
                    ),
                    file=file,
                )

            # Confirm implementation task and auto-start daemon
            if result["task"]:
                task = result["task"]
                # Auto-start daemon for this project if not running
                daemon_started = await self._ensure_daemon_running(
                    project=server.get("project"),
                )
                status_text = "Daemon started — implementing now" if daemon_started else "Daemon already running — task queued"
                await message.channel.send(
                    embed=discord.Embed(
                        title="Implementation Task Created",
                        description=f"**{task['title']}**",
                        color=COLOR_SUCCESS,
                    ).add_field(
                        name="Task ID", value=f"`{task['id']}`", inline=True
                    ).add_field(
                        name="Priority", value=str(task["priority"]), inline=True
                    ).add_field(
                        name="Status", value=status_text, inline=True
                    )
                )

            # Confirm image saves
            for info in image_infos:
                handler = self._get_image_handler(project)
                user_desc = message.content or ""
                task = handler.create_integration_task(
                    info,
                    user_description=user_desc,
                    username=message.author.display_name,
                )
                await message.channel.send(
                    embed=discord.Embed(
                        title="Image Saved",
                        description=(
                            f"Saved `{info['filename']}` to the website repo.\n"
                            f"Created task `{task['id']}` to integrate it."
                        ),
                        color=COLOR_SUCCESS,
                    )
                )

    # ------------------------------------------------------------------
    # Command registration
    # ------------------------------------------------------------------

    def _register_commands(self):
        """Register all bot commands."""

        @self.command(name="status")
        async def cmd_status(ctx: commands.Context):
            """Show TARS daemon status, uptime, tokens, and task stats."""
            if not self._check_command_channel(ctx):
                return

            pid_file = STATE_DIR / "daemon.pid"
            running = False
            pid = None
            uptime_str = "N/A"

            if pid_file.exists():
                pid = pid_file.read_text().strip()
                try:
                    os.kill(int(pid), 0)
                    running = True
                    started = pid_file.stat().st_mtime
                    uptime_secs = int(datetime.now().timestamp() - started)
                    hours = uptime_secs // 3600
                    mins = (uptime_secs % 3600) // 60
                    uptime_str = f"{hours}h {mins}m"
                except (ProcessLookupError, ValueError):
                    pass

            # Token usage
            try:
                tt = TokenTracker()
                usage = tt.get_today_usage()
                budget = tt.budget
                total = usage.get("total_tokens", 0)
                limit = budget.get("daily_limit", 1_000_000)
                pct = (total / limit * 100) if limit > 0 else 0
                token_str = f"{total:,} / {limit:,} ({pct:.1f}%)"
                cost_str = f"${usage.get('total_cost', 0):.4f}"
            except Exception:
                token_str = "N/A"
                cost_str = "N/A"

            # Task stats
            try:
                mt = MetricsTracker()
                stats = mt.get_today_stats()
                task_str = f"{stats.get('completed', 0)} completed, {stats.get('failed', 0)} failed"
            except Exception:
                task_str = "N/A"

            embed = discord.Embed(
                title="TARS Status",
                color=COLOR_SUCCESS if running else COLOR_ERROR,
            )
            embed.add_field(name="Daemon", value="Running" if running else "Stopped", inline=True)
            if running:
                embed.add_field(name="PID", value=str(pid), inline=True)
                embed.add_field(name="Uptime", value=uptime_str, inline=True)
            embed.add_field(name="Tokens Today", value=token_str, inline=True)
            embed.add_field(name="Cost Today", value=cost_str, inline=True)
            embed.add_field(name="Tasks Today", value=task_str, inline=True)
            embed.timestamp = datetime.utcnow()
            await ctx.send(embed=embed)

        @self.command(name="queue")
        async def cmd_queue(ctx: commands.Context):
            """List pending tasks (project-scoped in project servers)."""
            if not self._check_command_channel(ctx):
                return

            try:
                from lib.task_manager import TaskManager
                tm = TaskManager()
                tasks = tm.get_all_tasks()
            except Exception as e:
                await ctx.send(embed=self._error_embed(f"Failed to load tasks: {e}"))
                return

            # Project scoping
            scoped = self._get_scoped_project(ctx.guild.id)
            if scoped:
                tasks = [t for t in tasks if t.get("project") == scoped]

            if not tasks:
                await ctx.send(embed=discord.Embed(
                    title="Task Queue", description="No pending tasks.", color=COLOR_INFO,
                ))
                return

            lines = []
            for t in tasks[:15]:
                pri = t.get("priority", 0)
                src = t.get("source", "?")
                proj = t.get("project", "?")
                title = t.get("title", "Untitled")[:60]
                if scoped:
                    lines.append(f"`[{pri:3d}]` `{src:8s}` {title}")
                else:
                    lines.append(f"`[{pri:3d}]` `{src:8s}` **{proj}** — {title}")

            desc = "\n".join(lines)
            if len(tasks) > 15:
                desc += f"\n\n*...and {len(tasks) - 15} more*"

            embed = discord.Embed(title="Task Queue", description=desc, color=COLOR_INFO)
            embed.set_footer(text=f"{len(tasks)} total tasks")
            await ctx.send(embed=embed)

        @self.command(name="add")
        async def cmd_add(ctx: commands.Context, *, rest: str):
            """Add a task. Project servers: !add <title> | <desc>. Admin: !add <project> <title> | <desc>"""
            if not self._check_command_channel(ctx):
                return

            scoped = self._get_scoped_project(ctx.guild.id)
            if scoped:
                project = scoped
                parts = rest.split("|", 1)
                title = parts[0].strip()
                desc = parts[1].strip() if len(parts) > 1 else ""
            else:
                tokens = rest.split(None, 1)
                if len(tokens) < 2:
                    await ctx.send(embed=self._error_embed(
                        "Usage: `!add <project> <title> | <description>`"
                    ))
                    return
                project = tokens[0]
                parts = tokens[1].split("|", 1)
                title = parts[0].strip()
                desc = parts[1].strip() if len(parts) > 1 else ""

            if not title:
                usage = "`!add <title> | <description>`" if scoped else "`!add <project> <title> | <description>`"
                await ctx.send(embed=self._error_embed(f"Usage: {usage}"))
                return

            try:
                self._append_to_queue(project, title, desc, priority=100)
                embed = discord.Embed(
                    title="Task Added",
                    description=f"**{title}**",
                    color=COLOR_SUCCESS,
                )
                embed.add_field(name="Project", value=project, inline=True)
                embed.add_field(name="Priority", value="100", inline=True)
                if desc:
                    embed.add_field(name="Description", value=desc[:200], inline=False)
                await ctx.send(embed=embed)
            except Exception as e:
                await ctx.send(embed=self._error_embed(f"Failed to add task: {e}"))

        @self.command(name="run")
        async def cmd_run(ctx: commands.Context, *, rest: str):
            """Queue a high-priority task. Project servers: !run <title> | <desc>"""
            if not self._check_command_channel(ctx):
                return

            scoped = self._get_scoped_project(ctx.guild.id)
            if scoped:
                project = scoped
                parts = rest.split("|", 1)
                title = parts[0].strip()
                desc = parts[1].strip() if len(parts) > 1 else ""
            else:
                tokens = rest.split(None, 1)
                if len(tokens) < 2:
                    await ctx.send(embed=self._error_embed(
                        "Usage: `!run <project> <title> | <description>`"
                    ))
                    return
                project = tokens[0]
                parts = tokens[1].split("|", 1)
                title = parts[0].strip()
                desc = parts[1].strip() if len(parts) > 1 else ""

            if not title:
                usage = "`!run <title> | <description>`" if scoped else "`!run <project> <title> | <description>`"
                await ctx.send(embed=self._error_embed(f"Usage: {usage}"))
                return

            try:
                self._append_to_queue(project, title, desc, priority=200)
                embed = discord.Embed(
                    title="Priority Task Queued",
                    description=f"**{title}**",
                    color=COLOR_WARNING,
                )
                embed.add_field(name="Project", value=project, inline=True)
                embed.add_field(name="Priority", value="200 (immediate)", inline=True)
                if desc:
                    embed.add_field(name="Description", value=desc[:200], inline=False)
                await ctx.send(embed=embed)
            except Exception as e:
                await ctx.send(embed=self._error_embed(f"Failed to queue task: {e}"))

        @self.command(name="new-project")
        async def cmd_new_project(ctx: commands.Context, name: str, project_type: str, *, rest: str = ""):
            """Create a new project (admin only)."""
            if not self._check_command_channel(ctx):
                return
            if not self._is_admin_server(ctx.guild.id):
                await ctx.send(embed=self._error_embed("This command is only available in the admin server."))
                return

            parts = rest.split("|", 1) if rest else [""]
            description = parts[0].strip()

            await ctx.send(embed=discord.Embed(
                title="Creating Project...",
                description=f"Setting up **{name}** ({project_type})",
                color=COLOR_INFO,
            ))

            try:
                from lib.project_creator import create_project
                result = create_project(
                    name=name,
                    project_type=project_type,
                    description=description,
                )
                embed = discord.Embed(
                    title="Project Created",
                    description=f"**{name}** is ready!",
                    color=COLOR_SUCCESS,
                )
                embed.add_field(name="Repository", value=result["repo_url"], inline=False)
                embed.add_field(name="Config", value=result["config_path"], inline=False)
                await ctx.send(embed=embed)
            except Exception as e:
                await ctx.send(embed=self._error_embed(f"Project creation failed: {e}"))

        @self.command(name="projects")
        async def cmd_projects(ctx: commands.Context):
            """List configured projects (scoped in project servers)."""
            if not self._check_command_channel(ctx):
                return

            scoped = self._get_scoped_project(ctx.guild.id)

            if scoped:
                # Show only the scoped project
                try:
                    cfg = load_project(scoped)
                    repo = cfg.get("repo", "?")
                    strategy = cfg.get("git", {}).get("strategy", "?")
                    enabled = cfg.get("enabled", False)
                    status = "enabled" if enabled else "disabled"
                    desc = f"**{scoped}** — `{repo}` ({strategy}, {status})"
                    embed = discord.Embed(title="Project", description=desc, color=COLOR_INFO)
                    embed.set_footer(text="Project-scoped server")
                    await ctx.send(embed=embed)
                except FileNotFoundError:
                    await ctx.send(embed=self._error_embed(f"Project `{scoped}` not found."))
                return

            projects = list_projects()
            if not projects:
                await ctx.send(embed=discord.Embed(
                    title="Projects", description="No projects configured.", color=COLOR_INFO,
                ))
                return

            lines = []
            for p in projects:
                name = p.get("_name", "?")
                repo = p.get("repo", "?")
                strategy = p.get("git", {}).get("strategy", "?")
                enabled = p.get("enabled", False)
                status = "enabled" if enabled else "disabled"
                lines.append(f"**{name}** — `{repo}` ({strategy}, {status})")

            embed = discord.Embed(
                title="Projects",
                description="\n".join(lines),
                color=COLOR_INFO,
            )
            embed.set_footer(text=f"{len(projects)} project(s)")
            await ctx.send(embed=embed)

        @self.command(name="cancel")
        async def cmd_cancel(ctx: commands.Context, task_id: str):
            """Cancel a task: !cancel <task_id>"""
            if not self._check_command_channel(ctx):
                return

            try:
                queue_path = CONFIG_DIR / "queue.yaml"
                if not queue_path.exists():
                    await ctx.send(embed=self._error_embed("No queue file found."))
                    return

                with open(queue_path) as f:
                    data = yaml.safe_load(f) or {}

                tasks = data.get("tasks", [])
                found = False
                for t in tasks:
                    if t.get("id") == task_id:
                        t["status"] = "cancelled"
                        found = True
                        break

                if not found:
                    # Also check per-project queues
                    scoped = self._get_scoped_project(ctx.guild.id)
                    if scoped:
                        pq = CONFIG_DIR / "queues" / f"{scoped}.yaml"
                        if pq.exists():
                            with open(pq) as f:
                                pdata = yaml.safe_load(f) or {}
                            for t in pdata.get("tasks", []):
                                if t.get("id") == task_id:
                                    t["status"] = "cancelled"
                                    found = True
                                    break
                            if found:
                                with open(pq, "w") as f:
                                    yaml.dump(pdata, f, default_flow_style=False, sort_keys=False)

                if not found:
                    await ctx.send(embed=self._error_embed(f"Task `{task_id}` not found."))
                    return

                if not found:
                    pass  # Already handled above
                else:
                    with open(queue_path, "w") as f:
                        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

                await ctx.send(embed=discord.Embed(
                    title="Task Cancelled",
                    description=f"Task `{task_id}` has been cancelled.",
                    color=COLOR_WARNING,
                ))
            except Exception as e:
                await ctx.send(embed=self._error_embed(f"Failed to cancel task: {e}"))

        @self.command(name="stop")
        async def cmd_stop(ctx: commands.Context):
            """Stop the TARS daemon."""
            if not self._check_command_channel(ctx):
                return

            pid_file = STATE_DIR / "daemon.pid"
            if not pid_file.exists():
                await ctx.send(embed=self._error_embed("TARS daemon is not running."))
                return

            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                await ctx.send(embed=discord.Embed(
                    title="Daemon Stopping",
                    description=f"Sent SIGTERM to PID {pid}",
                    color=COLOR_WARNING,
                ))
            except ProcessLookupError:
                pid_file.unlink(missing_ok=True)
                await ctx.send(embed=self._error_embed("Daemon PID not found (stale PID file removed)."))
            except Exception as e:
                await ctx.send(embed=self._error_embed(f"Failed to stop daemon: {e}"))

        @self.command(name="start")
        async def cmd_start(ctx: commands.Context):
            """Start the TARS daemon."""
            if not self._check_command_channel(ctx):
                return

            pid_file = STATE_DIR / "daemon.pid"
            if pid_file.exists():
                pid = pid_file.read_text().strip()
                try:
                    os.kill(int(pid), 0)
                    await ctx.send(embed=self._error_embed(f"Daemon is already running (PID {pid})."))
                    return
                except (ProcessLookupError, ValueError):
                    pid_file.unlink(missing_ok=True)

            try:
                daemon_script = TARS_HOME / "bin" / "tars-daemon.sh"
                log_file = LOGS_DIR / "daemon.log"
                with open(log_file, "a") as lf:
                    proc = subprocess.Popen(
                        [str(daemon_script)],
                        stdout=lf, stderr=lf,
                        start_new_session=True,
                        cwd=str(TARS_HOME),
                        env={**os.environ, "TARS_HOME": str(TARS_HOME)},
                    )
                pid_file.write_text(str(proc.pid))
                await ctx.send(embed=discord.Embed(
                    title="Daemon Started",
                    description=f"TARS daemon started (PID {proc.pid})",
                    color=COLOR_SUCCESS,
                ))
            except Exception as e:
                await ctx.send(embed=self._error_embed(f"Failed to start daemon: {e}"))

        @self.command(name="logs")
        async def cmd_logs(ctx: commands.Context, n: int = 20):
            """Show last n lines of daemon log: !logs [n]"""
            if not self._check_command_channel(ctx):
                return

            log_file = LOGS_DIR / "daemon.log"
            if not log_file.exists():
                await ctx.send(embed=self._error_embed("No daemon log found."))
                return

            try:
                lines = log_file.read_text().splitlines()
                tail = lines[-n:] if len(lines) > n else lines
                text = "\n".join(tail)
                if len(text) > 1900:
                    text = text[-1900:]

                await ctx.send(embed=discord.Embed(
                    title=f"Daemon Log (last {len(tail)} lines)",
                    description=f"```\n{text}\n```",
                    color=COLOR_INFO,
                ))
            except Exception as e:
                await ctx.send(embed=self._error_embed(f"Failed to read logs: {e}"))

        @self.command(name="budget")
        async def cmd_budget(ctx: commands.Context):
            """Show token usage breakdown."""
            if not self._check_command_channel(ctx):
                return

            try:
                tt = TokenTracker()
                usage = tt.get_today_usage()
                budget = tt.budget

                total = usage.get("total_tokens", 0)
                limit = budget.get("daily_limit", 1_000_000)
                pct = (total / limit * 100) if limit > 0 else 0
                remaining = tt.get_remaining_budget()

                embed = discord.Embed(
                    title="Token Budget",
                    color=COLOR_WARNING if tt.is_warning() else COLOR_INFO,
                )
                embed.add_field(name="Used Today", value=f"{total:,}", inline=True)
                embed.add_field(name="Daily Limit", value=f"{limit:,}", inline=True)
                embed.add_field(name="Remaining", value=f"{remaining:,}", inline=True)
                embed.add_field(name="Usage", value=f"{pct:.1f}%", inline=True)
                embed.add_field(name="Cost", value=f"${usage.get('total_cost', 0):.4f}", inline=True)
                embed.add_field(name="Requests", value=str(usage.get("requests", 0)), inline=True)

                # Hourly breakdown
                hourly = usage.get("hourly", {})
                if hourly:
                    hourly_lines = []
                    for hour in sorted(hourly.keys(), key=int):
                        h = hourly[hour]
                        hourly_lines.append(f"`{int(hour):02d}:00` — {h['tokens']:,} tokens (${h['cost']:.4f})")
                    if hourly_lines:
                        embed.add_field(
                            name="Hourly Breakdown",
                            value="\n".join(hourly_lines[:12]),
                            inline=False,
                        )

                embed.timestamp = datetime.utcnow()
                await ctx.send(embed=embed)
            except Exception as e:
                await ctx.send(embed=self._error_embed(f"Failed to load budget: {e}"))

        # ---- New commands for chat mode ----

        @self.command(name="clear")
        async def cmd_clear(ctx: commands.Context):
            """Clear conversation history for this channel."""
            if not self._check_command_channel(ctx):
                return
            self.chat_engine.clear_history(str(ctx.channel.id))
            await ctx.send(embed=discord.Embed(
                title="History Cleared",
                description="Conversation history for this channel has been reset.",
                color=COLOR_INFO,
            ))

        @self.command(name="modes")
        async def cmd_modes(ctx: commands.Context):
            """Show available mode commands."""
            if not self._check_command_channel(ctx):
                return

            scoped = self._get_scoped_project(ctx.guild.id)
            desc_lines = [
                "**Mode commands** can be used alone or combined:\n",
                "`!chat <message>` — Natural conversation",
                "`!implement <description>` — Create a website change task",
                "`!pdf <topic>` — Generate a PDF document",
                "`!list <topic>` — Format response as a structured list",
                "`!research <topic>` — Search the web, then respond",
                "",
                "**Combine modes:** `!research !list Seattle diving spots`",
                "",
                "**Other commands:**",
                "`!status` — TARS daemon status",
                "`!queue` — View pending tasks",
                "`!add <title> | <desc>` — Add a task",
                "`!run <title> | <desc>` — Add a high-priority task",
                "`!cancel <id>` — Cancel a task",
                "`!clear` — Reset chat history",
                "`!budget` — Token usage",
            ]
            if scoped:
                desc_lines.append(f"\n*Scoped to project: **{scoped}***")

            await ctx.send(embed=discord.Embed(
                title="TARS Modes & Commands",
                description="\n".join(desc_lines),
                color=COLOR_INFO,
            ))

        @self.command(name="upload")
        async def cmd_upload(ctx: commands.Context, *, description: str = ""):
            """Upload images to the website: attach image(s) and !upload [description]"""
            if not self._check_command_channel(ctx):
                return

            project = self._get_scoped_project(ctx.guild.id)
            if not project:
                await ctx.send(embed=self._error_embed("Image upload requires a project-scoped server."))
                return

            if not ctx.message.attachments:
                await ctx.send(embed=self._error_embed("Attach one or more images with this command."))
                return

            handler = self._get_image_handler(project)
            saved = []

            for attachment in ctx.message.attachments:
                ct = attachment.content_type or ""
                if not ct.startswith("image/"):
                    continue
                info = await handler.download_attachment(attachment)
                if info:
                    task = handler.create_integration_task(
                        info,
                        user_description=description,
                        username=ctx.author.display_name,
                    )
                    saved.append((info, task))

            if not saved:
                await ctx.send(embed=self._error_embed("No valid images found in attachments."))
                return

            for info, task in saved:
                await ctx.send(embed=discord.Embed(
                    title="Image Uploaded",
                    description=(
                        f"Saved `{info['filename']}` to the website repo.\n"
                        f"Task `{task['id']}` created to integrate it.\n"
                    ),
                    color=COLOR_SUCCESS,
                ).add_field(
                    name="Web Path", value=f"`{info['web_path']}`", inline=True
                ).add_field(
                    name="Size", value=f"{info['size_bytes']:,} bytes", inline=True
                ))

    def _append_to_queue(self, project: str, title: str, description: str, priority: int = 100):
        """Append a task to config/queue.yaml."""
        queue_path = CONFIG_DIR / "queue.yaml"
        if queue_path.exists():
            with open(queue_path) as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}

        tasks = data.setdefault("tasks", [])

        import hashlib
        task_hash = hashlib.md5(f"{project}{title}{datetime.now().isoformat()}".encode()).hexdigest()[:8]
        task_id = f"task-{task_hash}"

        tasks.append({
            "id": task_id,
            "title": title,
            "description": description,
            "project": project,
            "priority": priority,
            "status": "pending",
        })

        with open(queue_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    async def on_ready(self):
        logger.info("TARS bot connected as %s", self.user)
        print(f"TARS bot connected as {self.user}")
        if self.servers:
            print(f"Configured servers: {', '.join(s.get('name', gid) for gid, s in self.servers.items())}")
        else:
            print("Running in legacy mode (no servers configured)")

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handle command errors gracefully."""
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(embed=self._error_embed(f"Missing argument: `{error.param.name}`"))
        elif isinstance(error, commands.CommandNotFound):
            pass  # Silently ignore — might be a mode command handled by on_message
        else:
            logger.error("Command error: %s", error)
            await ctx.send(embed=self._error_embed(str(error)))


def main():
    """Entry point for the Discord bot."""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    )

    config = load_bot_config()
    token = config.get("bot_token", "")

    if not token:
        print("ERROR: No bot_token in config/discord.yaml")
        print("Add 'bot_token: YOUR_TOKEN' to config/discord.yaml")
        sys.exit(1)

    bot = TARSBot(config)
    bot.run(token)


if __name__ == "__main__":
    main()
