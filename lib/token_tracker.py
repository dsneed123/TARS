"""TARS token budget tracker — adaptive pacing and budget management."""

import json
import logging
import os
import time
from datetime import datetime, date
from pathlib import Path

from lib.config_loader import load_token_budget

logger = logging.getLogger("tars.token_tracker")

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))
STATE_DIR = TARS_HOME / "state"
USAGE_FILE = STATE_DIR / "token_usage.json"


class TokenTracker:
    """Tracks token usage and enforces budget limits with adaptive pacing."""

    def __init__(self):
        self.budget = load_token_budget()
        self.usage = self._load_usage()

    def _load_usage(self) -> dict:
        if USAGE_FILE.exists():
            with open(USAGE_FILE) as f:
                data = json.load(f)
            # Reset if it's a new day
            if data.get("date") != str(date.today()):
                return self._new_day()
            return data
        return self._new_day()

    def _new_day(self) -> dict:
        return {
            "date": str(date.today()),
            "tokens_in": 0,
            "tokens_out": 0,
            "total_tokens": 0,
            "total_cost": 0.0,
            "requests": 0,
            "hourly": {},
        }

    def _save_usage(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(USAGE_FILE, "w") as f:
            json.dump(self.usage, f, indent=2)

    def record(self, tokens_in: int, tokens_out: int, cost_usd: float = 0.0):
        """Record token usage from a Claude invocation."""
        # Reset if new day
        if self.usage.get("date") != str(date.today()):
            self.usage = self._new_day()

        self.usage["tokens_in"] += tokens_in
        self.usage["tokens_out"] += tokens_out
        self.usage["total_tokens"] += tokens_in + tokens_out
        self.usage["total_cost"] += cost_usd
        self.usage["requests"] += 1

        # Track hourly usage
        hour = str(datetime.now().hour)
        hourly = self.usage.setdefault("hourly", {})
        hourly.setdefault(hour, {"tokens": 0, "cost": 0.0})
        hourly[hour]["tokens"] += tokens_in + tokens_out
        hourly[hour]["cost"] += cost_usd

        self._save_usage()
        logger.debug(
            "Recorded: %d in + %d out = %d tokens ($%.4f)",
            tokens_in, tokens_out, tokens_in + tokens_out, cost_usd,
        )

    def can_spend(self, estimated_tokens: int = 0) -> bool:
        """Check if we're within budget, accounting for peak hours."""
        if self.usage.get("date") != str(date.today()):
            return True

        daily_limit = self.budget.get("daily_limit", 1_000_000)
        current = self.usage.get("total_tokens", 0)

        # Adjust limit for peak hours
        effective_limit = daily_limit
        hour = datetime.now().hour
        peak = self.budget.get("peak_hours", {})
        if peak.get("start", 9) <= hour < peak.get("end", 17):
            multiplier = self.budget.get("peak_multiplier", 0.5)
            effective_limit = int(daily_limit * multiplier)
            logger.debug("Peak hours: effective limit = %d", effective_limit)

        return (current + estimated_tokens) < effective_limit

    def get_today_usage(self) -> dict:
        """Get today's usage summary."""
        if self.usage.get("date") != str(date.today()):
            return self._new_day()
        return self.usage.copy()

    def get_wait_time(self) -> int:
        """Calculate how long to wait before next request (rate limit backoff)."""
        if self.can_spend():
            return 0

        # Check if we're in peak hours — if so, wait until off-peak
        hour = datetime.now().hour
        peak = self.budget.get("peak_hours", {})
        if peak.get("start", 9) <= hour < peak.get("end", 17):
            # Wait until end of peak
            end_hour = peak.get("end", 17)
            now = datetime.now()
            end_time = now.replace(hour=end_hour, minute=0, second=0)
            wait = int((end_time - now).total_seconds())
            return max(wait, self.budget.get("rate_limit_backoff", 60))

        # Over daily budget — wait until tomorrow
        now = datetime.now()
        seconds_until_midnight = (
            24 * 3600 - (now.hour * 3600 + now.minute * 60 + now.second)
        )
        return seconds_until_midnight

    def is_warning(self) -> bool:
        """Check if we're approaching the budget threshold."""
        if self.usage.get("date") != str(date.today()):
            return False
        threshold = self.budget.get("warning_threshold", 0.8)
        daily_limit = self.budget.get("daily_limit", 1_000_000)
        current = self.usage.get("total_tokens", 0)
        return current >= (daily_limit * threshold)

    def get_remaining_budget(self) -> int:
        """Get remaining token budget for today."""
        daily_limit = self.budget.get("daily_limit", 1_000_000)
        current = self.usage.get("total_tokens", 0)
        return max(0, daily_limit - current)
