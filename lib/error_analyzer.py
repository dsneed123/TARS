"""TARS error analyzer — error detection, auto-patch loop, and circuit breakers."""

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("tars.error_analyzer")

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))
STATE_DIR = TARS_HOME / "state"
CB_FILE = STATE_DIR / "circuit_breakers.json"

# Defaults
CIRCUIT_BREAKER_THRESHOLD = int(os.environ.get("CIRCUIT_BREAKER_THRESHOLD", "3"))
CIRCUIT_BREAKER_LOCKOUT = int(os.environ.get("CIRCUIT_BREAKER_LOCKOUT", "3600"))


class ErrorAnalyzer:
    """Manages error tracking, circuit breakers, and failure analysis."""

    def __init__(self):
        self.cb_data = self._load_cb()

    def _load_cb(self) -> dict:
        if CB_FILE.exists():
            with open(CB_FILE) as f:
                return json.load(f)
        return {}

    def _save_cb(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(CB_FILE, "w") as f:
            json.dump(self.cb_data, f, indent=2)

    def record_failure(self, project: str, task_id: str, error: str = ""):
        """Record a task failure for circuit breaker tracking."""
        entry = self.cb_data.setdefault(project, {
            "consecutive_failures": 0,
            "total_failures": 0,
            "locked_until": 0,
            "lockout_duration": CIRCUIT_BREAKER_LOCKOUT,
            "last_error": "",
            "last_task": "",
        })

        entry["consecutive_failures"] += 1
        entry["total_failures"] += 1
        entry["last_error"] = error[:500]
        entry["last_task"] = task_id

        # Trip circuit breaker if threshold exceeded
        if entry["consecutive_failures"] >= CIRCUIT_BREAKER_THRESHOLD:
            lockout = entry.get("lockout_duration", CIRCUIT_BREAKER_LOCKOUT)
            entry["locked_until"] = int(time.time()) + lockout
            # Double lockout for next time (exponential backoff)
            entry["lockout_duration"] = min(lockout * 2, 86400)  # Max 24h
            logger.warning(
                "Circuit breaker tripped for %s: locked for %ds",
                project, lockout,
            )

        self._save_cb()

    def reset_failures(self, project: str):
        """Reset consecutive failure count on success."""
        if project in self.cb_data:
            self.cb_data[project]["consecutive_failures"] = 0
            self.cb_data[project]["lockout_duration"] = CIRCUIT_BREAKER_LOCKOUT
            self._save_cb()

    def is_locked(self, project: str) -> bool:
        """Check if a project is locked by circuit breaker."""
        entry = self.cb_data.get(project, {})
        locked_until = entry.get("locked_until", 0)
        if locked_until > time.time():
            remaining = int(locked_until - time.time())
            logger.info("Project %s locked for %ds more", project, remaining)
            return True
        return False

    def get_status(self, project: str) -> dict:
        """Get circuit breaker status for a project."""
        entry = self.cb_data.get(project, {})
        locked_until = entry.get("locked_until", 0)
        return {
            "consecutive_failures": entry.get("consecutive_failures", 0),
            "total_failures": entry.get("total_failures", 0),
            "is_locked": locked_until > time.time(),
            "locked_until": locked_until,
            "remaining_lockout": max(0, int(locked_until - time.time())),
            "last_error": entry.get("last_error", ""),
            "last_task": entry.get("last_task", ""),
        }

    def get_all_statuses(self) -> dict:
        """Get circuit breaker status for all projects."""
        return {p: self.get_status(p) for p in self.cb_data}

    def classify_error(self, error_output: str) -> dict:
        """Classify an error to help decide how to handle it.

        Returns dict with:
          - category: build|test|runtime|syntax|dependency|unknown
          - retryable: bool
          - suggestion: str
        """
        error_lower = error_output.lower()

        if any(kw in error_lower for kw in ["syntax error", "syntaxerror", "unexpected token"]):
            return {"category": "syntax", "retryable": True, "suggestion": "Fix syntax error"}
        if any(kw in error_lower for kw in ["import error", "modulenotfounderror", "no module named", "cannot find module"]):
            return {"category": "dependency", "retryable": True, "suggestion": "Fix import/dependency"}
        if any(kw in error_lower for kw in ["build failed", "compilation error", "does not compile", "linker error"]):
            return {"category": "build", "retryable": True, "suggestion": "Fix build error"}
        if any(kw in error_lower for kw in ["test failed", "assertion", "expected", "xctassert"]):
            return {"category": "test", "retryable": True, "suggestion": "Fix failing test"}
        if any(kw in error_lower for kw in ["permission denied", "access denied", "eacces"]):
            return {"category": "permission", "retryable": False, "suggestion": "Permission issue, needs manual intervention"}
        if any(kw in error_lower for kw in ["network", "timeout", "connection refused", "econnrefused"]):
            return {"category": "network", "retryable": True, "suggestion": "Network issue, retry after delay"}

        return {"category": "unknown", "retryable": True, "suggestion": "Unknown error, attempt auto-fix"}
