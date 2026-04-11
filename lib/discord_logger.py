"""TARS Discord webhook logger — one-way logging with embeds."""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from lib.config_loader import load_discord_config

logger = logging.getLogger("tars.discord_logger")

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))

# Embed colors
COLOR_SUCCESS = 0x2ECC71   # Green
COLOR_WARNING = 0xF39C12   # Yellow/Orange
COLOR_ERROR = 0xE74C3C     # Red
COLOR_INFO = 0x3498DB      # Blue
COLOR_SUMMARY = 0x9B59B6   # Purple


class DiscordLogger:
    """Sends log messages to Discord via webhook."""

    def __init__(self):
        self.config = load_discord_config()
        self.webhook_url = self.config.get("webhook_url", "")
        self.username = self.config.get("username", "TARS")
        self.avatar_url = self.config.get("avatar_url", "")
        self.notify_on = self.config.get("notify_on", [])
        self.enabled = bool(self.webhook_url)

        if not self.enabled:
            logger.debug("Discord logging disabled (no webhook_url)")

    def _send(self, payload: dict) -> bool:
        """Send a payload to the Discord webhook."""
        if not self.enabled:
            return False

        payload["username"] = self.username
        if self.avatar_url:
            payload["avatar_url"] = self.avatar_url

        try:
            resp = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10,
            )
            if resp.status_code == 204:
                return True
            logger.warning("Discord webhook returned %d: %s", resp.status_code, resp.text)
            return False
        except Exception as e:
            logger.warning("Discord webhook failed: %s", e)
            return False

    def _embed(
        self,
        title: str,
        description: str,
        color: int,
        fields: Optional[list[dict]] = None,
        footer: Optional[str] = None,
    ) -> dict:
        """Build a Discord embed object."""
        embed = {
            "title": title,
            "description": description[:4096],
            "color": color,
            "timestamp": datetime.utcnow().isoformat(),
        }
        if fields:
            embed["fields"] = fields[:25]
        if footer:
            embed["footer"] = {"text": footer}
        return embed

    def log_success(
        self,
        task_title: str,
        pr_url: str = "",
        project: str = "",
        details: str = "",
    ):
        """Log a successful task completion."""
        if "task_complete" not in self.notify_on and self.notify_on:
            return

        fields = []
        if project:
            fields.append({"name": "Project", "value": project, "inline": True})
        if pr_url and pr_url != "direct-push":
            fields.append({"name": "PR", "value": pr_url, "inline": True})

        embed = self._embed(
            title=f"Task Completed: {task_title}",
            description=details or "Task implemented and pushed successfully.",
            color=COLOR_SUCCESS,
            fields=fields,
        )
        self._send({"embeds": [embed]})

    def log_error(self, task_title: str, error: str = "", project: str = ""):
        """Log a task failure."""
        if "task_failed" not in self.notify_on and self.notify_on:
            return

        fields = []
        if project:
            fields.append({"name": "Project", "value": project, "inline": True})

        # Truncate error for Discord
        if len(error) > 1000:
            error = error[:997] + "..."

        embed = self._embed(
            title=f"Task Failed: {task_title}",
            description=f"```\n{error}\n```" if error else "Task failed. Check logs for details.",
            color=COLOR_ERROR,
            fields=fields,
        )
        self._send({"embeds": [embed]})

    def log_warning(self, message: str, project: str = ""):
        """Log a warning."""
        fields = []
        if project:
            fields.append({"name": "Project", "value": project, "inline": True})

        embed = self._embed(
            title="Warning",
            description=message,
            color=COLOR_WARNING,
            fields=fields,
        )
        self._send({"embeds": [embed]})

    def log_info(self, message: str, project: str = ""):
        """Log an informational message."""
        fields = []
        if project:
            fields.append({"name": "Project", "value": project, "inline": True})

        embed = self._embed(
            title="Info",
            description=message,
            color=COLOR_INFO,
            fields=fields,
        )
        self._send({"embeds": [embed]})

    def log_circuit_breaker(self, project: str, lockout_secs: int, failures: int):
        """Log a circuit breaker trip."""
        if "circuit_breaker" not in self.notify_on and self.notify_on:
            return

        hours = lockout_secs / 3600
        embed = self._embed(
            title=f"Circuit Breaker Tripped: {project}",
            description=f"Project locked for {hours:.1f} hours after {failures} consecutive failures.",
            color=COLOR_ERROR,
            fields=[
                {"name": "Lockout", "value": f"{hours:.1f} hours", "inline": True},
                {"name": "Failures", "value": str(failures), "inline": True},
            ],
        )
        self._send({"embeds": [embed]})

    def log_daily_summary(self, stats: dict):
        """Send the daily summary embed."""
        if "daily_summary" not in self.notify_on and self.notify_on:
            return

        completed = stats.get("completed", 0)
        failed = stats.get("failed", 0)
        total = completed + failed
        tokens = stats.get("tokens_used", 0)
        cost = stats.get("cost_usd", 0.0)
        prs = stats.get("prs_created", 0)

        success_rate = (completed / total * 100) if total > 0 else 0

        embed = self._embed(
            title="Daily Summary",
            description=f"**{completed}** tasks completed, **{failed}** failed ({success_rate:.0f}% success rate)",
            color=COLOR_SUMMARY,
            fields=[
                {"name": "Tasks", "value": f"{completed} / {total}", "inline": True},
                {"name": "PRs Created", "value": str(prs), "inline": True},
                {"name": "Tokens Used", "value": f"{tokens:,}", "inline": True},
                {"name": "Cost", "value": f"${cost:.4f}", "inline": True},
            ],
            footer=f"TARS Daily Report - {datetime.now().strftime('%Y-%m-%d')}",
        )
        self._send({"embeds": [embed]})
