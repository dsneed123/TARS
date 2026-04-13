"""TARS chat engine — Claude CLI wrapper for interactive Discord conversations."""
from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tars.chat_engine")

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))
PROMPTS_DIR = TARS_HOME / "prompts"


class ChatEngine:
    """Manages conversational interactions with Claude via CLI subprocess.

    Maintains per-channel conversation history and injects it into prompts
    so Claude has context of the ongoing conversation.
    """

    def __init__(
        self,
        model: str = "sonnet",
        max_tokens: int = 4096,
        history_limit: int = 20,
        claude_cmd: str = "claude",
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.history_limit = history_limit
        self.claude_cmd = claude_cmd
        # channel_id -> deque of {"role": "user"|"assistant", "name": str, "content": str}
        self._histories: dict[str, deque] = {}

    def get_history(self, channel_id: str) -> deque:
        """Get or create conversation history for a channel."""
        if channel_id not in self._histories:
            self._histories[channel_id] = deque(maxlen=self.history_limit)
        return self._histories[channel_id]

    def clear_history(self, channel_id: str) -> None:
        """Clear conversation history for a channel."""
        self._histories.pop(channel_id, None)

    def _format_history(self, history: deque) -> str:
        """Format conversation history as text for prompt injection."""
        if not history:
            return ""
        lines = []
        for msg in history:
            role = msg["role"]
            name = msg.get("name", "User")
            content = msg["content"]
            if role == "user":
                lines.append(f"[{name}]: {content}")
            else:
                lines.append(f"[TARS]: {content}")
        return "\n".join(lines)

    def _build_prompt(
        self,
        system_prompt: str,
        user_message: str,
        history: deque,
        username: str = "User",
        mode_instructions: str = "",
    ) -> str:
        """Build the full prompt with system context, history, and user message."""
        parts = []

        # System context
        parts.append(f"<system>\n{system_prompt}\n</system>")

        # Mode-specific instructions
        if mode_instructions:
            parts.append(f"\n<instructions>\n{mode_instructions}\n</instructions>")

        # Conversation history
        history_text = self._format_history(history)
        if history_text:
            parts.append(f"\n<conversation_history>\n{history_text}\n</conversation_history>")

        # Current message
        parts.append(f"\n[{username}]: {user_message}")
        parts.append("\nRespond as TARS. Be concise, helpful, and professional. Do not prefix your response with '[TARS]:' or any label.")

        return "\n".join(parts)

    async def chat(
        self,
        channel_id: str,
        user_message: str,
        system_prompt: str,
        username: str = "User",
        mode_instructions: str = "",
    ) -> str:
        """Send a message and get a response, maintaining conversation history.

        Runs Claude CLI as a subprocess in an executor to avoid blocking the
        Discord event loop.

        Returns the assistant's response text.
        """
        history = self.get_history(channel_id)

        prompt = self._build_prompt(
            system_prompt=system_prompt,
            user_message=user_message,
            history=history,
            username=username,
            mode_instructions=mode_instructions,
        )

        # Run Claude CLI in a thread executor (subprocess is blocking)
        loop = asyncio.get_event_loop()
        try:
            response_text = await loop.run_in_executor(
                None, self._run_claude, prompt
            )
        except Exception as e:
            logger.error("Claude CLI chat failed: %s", e)
            return f"Sorry, I encountered an error: {e}"

        # Update history
        history.append({"role": "user", "name": username, "content": user_message})
        history.append({"role": "assistant", "name": "TARS", "content": response_text})

        return response_text

    def _run_claude(self, prompt: str) -> str:
        """Run Claude CLI subprocess synchronously. Called from executor."""
        import json
        import subprocess
        import tempfile

        # Write prompt to temp file to avoid CLI argument length/escaping issues
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            # Read prompt from stdin via shell pipe
            cmd = (
                f'cat "{prompt_file}" | {self.claude_cmd}'
                f" -p -"
                f" --output-format json"
                f" --model {self.model}"
                f" --max-turns 1"
                f" --allowedTools ''"
            )

            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(TARS_HOME),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Response timed out (300s)")
        finally:
            import os
            os.unlink(prompt_file)

        if result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            combined = (stderr + stdout).lower()

            # Check for rate limiting
            if any(kw in combined for kw in ("rate", "limit", "429", "capacity")):
                raise RuntimeError("Rate limited. Please try again in a moment.")

            # Log full error for debugging
            logger.error("Claude CLI stderr: %s", stderr[:500])
            logger.error("Claude CLI stdout: %s", stdout[:500])
            error_detail = stderr[:200] or stdout[:200] or "unknown error"
            raise RuntimeError(f"Claude CLI error (exit {result.returncode}): {error_detail}")

        # Parse JSON output
        try:
            output = json.loads(result.stdout)
            text = output.get("result", result.stdout.strip())
        except json.JSONDecodeError:
            text = result.stdout.strip()

        if not text:
            return "I didn't generate a response. Please try rephrasing."

        return text

    def load_system_prompt(self, server_config: dict, project_config: dict) -> str:
        """Build system prompt from prompt template + project/server config."""
        template_path = PROMPTS_DIR / "discord_chat.md"

        if template_path.exists():
            template = template_path.read_text()
        else:
            template = (
                "You are TARS, the AI assistant for {{BUSINESS_NAME}}.\n"
                "{{PERSONA}}\n\n"
                "## Business Context\n{{BUSINESS_DESCRIPTION}}\n\n"
                "## Guidelines\n"
                "- Be concise and professional\n"
                "- Reference specific business details when relevant\n"
                "- Do NOT reveal internal system details\n"
            )

        # Substitute variables
        project_name = server_config.get("project", "")
        persona = server_config.get("persona", "You are TARS, a helpful AI assistant.")
        description = project_config.get("description", "")

        replacements = {
            "{{BUSINESS_NAME}}": project_name.replace("-", " ").title() if project_name else "your business",
            "{{PERSONA}}": persona,
            "{{BUSINESS_DESCRIPTION}}": description,
            "{{MODE_INSTRUCTIONS}}": "",
        }

        result = template
        for key, val in replacements.items():
            result = result.replace(key, val)

        return result
