"""TARS metrics tracker — task stats and daily summaries."""

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger("tars.metrics")

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))
STATE_DIR = TARS_HOME / "state"
METRICS_FILE = STATE_DIR / "metrics.json"


class MetricsTracker:
    """Tracks task execution metrics and generates summaries."""

    def __init__(self):
        self.data = self._load()

    def _load(self) -> dict:
        if METRICS_FILE.exists():
            with open(METRICS_FILE) as f:
                return json.load(f)
        return {"days": {}}

    def _save(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(METRICS_FILE, "w") as f:
            json.dump(self.data, f, indent=2)

    def _today_key(self) -> str:
        return str(date.today())

    def _ensure_day(self, day: str) -> dict:
        if day not in self.data["days"]:
            self.data["days"][day] = {
                "completed": 0,
                "failed": 0,
                "tasks": [],
                "prs_created": 0,
                "tokens_used": 0,
                "cost_usd": 0.0,
            }
        return self.data["days"][day]

    def record_task(self, project: str, title: str, status: str, pr_url: str = ""):
        """Record a task execution."""
        day = self._today_key()
        entry = self._ensure_day(day)

        if status == "success":
            entry["completed"] += 1
        else:
            entry["failed"] += 1

        if pr_url and pr_url != "direct-push":
            entry["prs_created"] += 1

        entry["tasks"].append({
            "project": project,
            "title": title,
            "status": status,
            "pr_url": pr_url,
            "timestamp": datetime.now().isoformat(),
        })

        # Keep only last 30 days of data
        days = sorted(self.data["days"].keys())
        while len(days) > 30:
            del self.data["days"][days.pop(0)]

        self._save()

    def get_today_stats(self) -> dict:
        """Get today's stats summary."""
        day = self._today_key()
        entry = self._ensure_day(day)

        # Merge in token usage
        try:
            from lib.token_tracker import TokenTracker
            tt = TokenTracker()
            usage = tt.get_today_usage()
            entry["tokens_used"] = usage.get("total_tokens", 0)
            entry["cost_usd"] = usage.get("total_cost", 0.0)
        except Exception:
            pass

        return entry

    def send_daily_summary(self):
        """Send daily summary to Discord."""
        stats = self.get_today_stats()

        try:
            from lib.discord_logger import DiscordLogger
            dl = DiscordLogger()
            dl.log_daily_summary(stats)
            logger.info("Daily summary sent to Discord")
        except Exception as e:
            logger.warning("Failed to send daily summary: %s", e)

    def get_weekly_stats(self) -> dict:
        """Get stats for the last 7 days."""
        from datetime import timedelta
        today = date.today()
        totals = {"completed": 0, "failed": 0, "prs_created": 0, "tokens_used": 0, "cost_usd": 0.0}

        for i in range(7):
            day = str(today - timedelta(days=i))
            entry = self.data.get("days", {}).get(day, {})
            totals["completed"] += entry.get("completed", 0)
            totals["failed"] += entry.get("failed", 0)
            totals["prs_created"] += entry.get("prs_created", 0)
            totals["tokens_used"] += entry.get("tokens_used", 0)
            totals["cost_usd"] += entry.get("cost_usd", 0.0)

        return totals
