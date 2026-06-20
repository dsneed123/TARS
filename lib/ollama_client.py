"""TARS Ollama HTTP client — stdlib-only wrapper around the Ollama REST API.

No external dependencies (uses urllib) so it works in the same minimal Python
environment as the rest of TARS. Talks to a local Ollama daemon, by default at
http://localhost:11434.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger("tars.ollama_client")

DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
# Generation timeout — 70B models on local hardware are slow; be generous.
DEFAULT_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "1200"))


class OllamaError(Exception):
    """Raised when the Ollama API call fails."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class OllamaClient:
    """Thin client over the Ollama HTTP API."""

    def __init__(self, host: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
        self.host = (host or DEFAULT_HOST).rstrip("/")
        self.timeout = timeout

    # ----------------------------------------------------------------- internals
    def _post(self, path: str, payload: dict, timeout: Optional[int] = None) -> dict:
        url = f"{self.host}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise OllamaError(f"Ollama HTTP {e.code}: {detail}", status=e.code)
        except urllib.error.URLError as e:
            raise OllamaError(
                f"Cannot reach Ollama at {self.host}: {e.reason}. Is `ollama serve` running?"
            )
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise OllamaError(f"Ollama returned non-JSON: {body[:300]}")

    def _get(self, path: str, timeout: int = 10) -> dict:
        url = f"{self.host}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise OllamaError(f"Cannot reach Ollama at {self.host}: {e.reason}")

    # ------------------------------------------------------------------- public API
    def is_up(self) -> bool:
        """Return True if the Ollama daemon answers."""
        try:
            self._get("/api/tags")
            return True
        except OllamaError:
            return False

    def list_models(self) -> list[str]:
        """Return the list of locally available model tags."""
        data = self._get("/api/tags")
        return [m.get("name", "") for m in data.get("models", [])]

    def has_model(self, model: str) -> bool:
        """True if `model` (with or without an explicit tag) is pulled locally."""
        available = self.list_models()
        if model in available:
            return True
        # Allow matching "qwen2.5-coder:32b" against "qwen2.5-coder:32b" or base name.
        base = model.split(":")[0]
        return any(m.split(":")[0] == base for m in available)

    def chat(
        self,
        model: str,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        num_ctx: int = 16384,
        temperature: float = 0.2,
        timeout: Optional[int] = None,
        keep_alive: str = "30m",
        num_predict: Optional[int] = None,
        stop: Optional[list] = None,
    ) -> dict:
        """Run a non-streaming chat completion.

        Returns the raw Ollama response dict. Key fields:
          response["message"]["content"]      -> assistant text
          response["message"].get("tool_calls") -> list of tool calls (if any)
          response["prompt_eval_count"]       -> input tokens
          response["eval_count"]              -> output tokens
        """
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": keep_alive,
            "options": {
                "num_ctx": num_ctx,
                "temperature": temperature,
            },
        }
        if num_predict is not None:
            # Cap output length so replies stay fast when a long answer isn't needed.
            payload["options"]["num_predict"] = num_predict
        if stop:
            # Stop sequences — hard stop if the model tries to write the next turn.
            payload["options"]["stop"] = stop
        if tools:
            payload["tools"] = tools

        logger.debug("Ollama chat: model=%s, messages=%d, tools=%s",
                     model, len(messages), bool(tools))
        return self._post("/api/chat", payload, timeout=timeout)

    # ------------------------------------------------------------- model lifecycle
    def ps(self) -> list[dict]:
        """Return models currently loaded in memory (Ollama /api/ps)."""
        return self._get("/api/ps").get("models", [])

    def loaded_models(self) -> list[str]:
        return [m.get("name", "") for m in self.ps()]

    def load_model(self, model: str, keep_alive: str = "30m") -> dict:
        """Spin a model UP — load it into memory without generating anything."""
        return self._post(
            "/api/generate",
            {"model": model, "keep_alive": keep_alive},
            timeout=300,
        )

    def unload_model(self, model: str) -> dict:
        """Spin a model DOWN — unload it from memory (keep_alive=0)."""
        return self._post(
            "/api/generate",
            {"model": model, "keep_alive": 0},
            timeout=60,
        )
