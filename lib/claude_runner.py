"""TARS Claude CLI subprocess wrapper — the core engine."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tars.claude_runner")

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))
PROMPTS_DIR = TARS_HOME / "prompts"


class ClaudeError(Exception):
    """Raised when Claude CLI returns an error."""
    def __init__(self, message: str, exit_code: int = 1, stderr: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class ClaudeRunner:
    """Wraps Claude CLI invocations."""

    def __init__(
        self,
        model: str = "sonnet",
        max_turns: int = 20,
        claude_cmd: str = "claude",
        allowed_tools: Optional[list[str]] = None,
    ):
        self.model = model
        self.max_turns = max_turns
        self.claude_cmd = claude_cmd
        self.allowed_tools = allowed_tools

    def run(
        self,
        prompt: str,
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        max_turns: Optional[int] = None,
        timeout: int = 600,
        append_system: Optional[str] = None,
    ) -> dict:
        """Run Claude CLI with a prompt and return parsed JSON output.

        Returns dict with keys: result, cost_usd, duration_ms, tokens_in, tokens_out
        """
        cmd = [
            self.claude_cmd,
            "-p", prompt,
            "--output-format", "json",
            "--model", model or self.model,
            "--max-turns", str(max_turns or self.max_turns),
            "--dangerously-skip-permissions",
        ]

        if self.allowed_tools:
            for tool in self.allowed_tools:
                cmd.extend(["--allowedTools", tool])

        if append_system:
            cmd.extend(["--append-system-prompt", append_system])

        env = os.environ.copy()
        start = time.time()

        logger.info("Running Claude: model=%s, cwd=%s", model or self.model, cwd)
        logger.debug("Prompt (first 200 chars): %s", prompt[:200])

        max_retries = 3
        for attempt in range(max_retries):
            try:
                result = subprocess.run(
                    cmd,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                raise ClaudeError("Claude CLI timed out", exit_code=-1)

            # Check for rate limiting — wait and retry
            combined_output = (result.stderr + result.stdout).lower()
            if result.returncode != 0 and ("rate" in combined_output or "limit" in combined_output or "429" in combined_output or "capacity" in combined_output):
                wait_time = 300 * (attempt + 1)  # 5min, 10min, 15min
                logger.warning("Rate limited (attempt %d/%d), waiting %ds", attempt + 1, max_retries, wait_time)
                time.sleep(wait_time)
                continue

            break

        duration_ms = int((time.time() - start) * 1000)

        if result.returncode != 0:
            raise ClaudeError(
                f"Claude CLI exited with code {result.returncode}",
                exit_code=result.returncode,
                stderr=result.stderr,
            )

        # Parse JSON output
        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            # If not JSON, wrap raw output
            output = {"result": result.stdout.strip(), "is_error": False}

        # Extract token usage from output if available
        tokens_in = output.get("num_input_tokens", 0)
        tokens_out = output.get("num_output_tokens", 0)
        cost_usd = output.get("cost_usd", 0.0)
        text_result = output.get("result", result.stdout.strip())

        # Check for session limit warnings in stderr (even on success)
        session_warning = False
        if result.stderr:
            stderr_lower = result.stderr.lower()
            if "session limit" in stderr_lower or "% of" in stderr_lower:
                # Extract percentage if possible
                import re
                pct_match = re.search(r'(\d{2,3})%', result.stderr)
                pct = int(pct_match.group(1)) if pct_match else 0
                if pct >= 90:
                    session_warning = True
                    logger.warning("Claude session at %d%% limit", pct)

        return {
            "result": text_result,
            "cost_usd": cost_usd,
            "duration_ms": duration_ms,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "is_error": output.get("is_error", False),
            "session_warning": session_warning,
        }

    def run_with_prompt_file(
        self,
        prompt_file: str,
        variables: Optional[dict] = None,
        **kwargs,
    ) -> dict:
        """Load a prompt template, substitute variables, and run."""
        path = PROMPTS_DIR / prompt_file
        if not path.exists():
            raise FileNotFoundError(f"Prompt template not found: {path}")

        template = path.read_text()
        if variables:
            for key, val in variables.items():
                template = template.replace(f"{{{{{key}}}}}", str(val))

        return self.run(template, **kwargs)

    def self_review(self, diff: str, cwd: Optional[str] = None) -> dict:
        """Have Claude review a diff before pushing."""
        return self.run_with_prompt_file(
            "review_code.md",
            variables={"DIFF": diff},
            cwd=cwd,
            model="sonnet",
            max_turns=3,
        )
