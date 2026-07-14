"""Lean per-role LLM interface for the node pipeline.

One class, two capabilities:
  text(role, prompt)  — single completion, structured-output friendly
  agent(role, prompt) — native tool-calling loop with file/command tools

Roles (router, planner, coder, reviewer, escalation) map to models + options in
config/graph.yaml's `models:` section — config, not code. Every result dict
reports tokens and the number of LLM calls made so the graph executor can
account per-node cost precisely.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

try:
    from ollama_client import OllamaClient, OllamaError
except ImportError:
    from lib.ollama_client import OllamaClient, OllamaError

logger = logging.getLogger("tars.llm")

MAX_FILE_CHARS = 48_000
MAX_CMD_OUTPUT = 8_000

TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file. Path is relative to the working directory.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a file with the given full contents.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace exact text in a file. `search` must match the file exactly once; cheaper than rewriting the whole file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "search": {"type": "string"},
            "replace": {"type": "string"}},
            "required": ["path", "search", "replace"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List entries at a path relative to the working directory.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command in the working directory (build, test, grep, git).",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                       "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "Call when the task is fully complete, with a short summary of what changed.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}},
                       "required": ["summary"]}}},
]

_THINK_RE = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


def extract_json(text: str) -> Optional[dict]:
    """Pull the first JSON object out of model text (fenced or bare)."""
    text = strip_think(text)
    if not text:
        return None
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


class LLM:
    """Role-routed Ollama access with call/token accounting."""

    def __init__(self, roles: dict, host: Optional[str] = None):
        """`roles` is graph.yaml's models section:
        {coder: {model: ..., num_ctx: ..., num_predict: ..., keep_alive: ...}, ...}"""
        self.roles = roles
        self.client = OllamaClient(host=host)

    def _role(self, role: str) -> dict:
        cfg = self.roles.get(role)
        if not cfg:
            raise OllamaError(f"Unknown LLM role: {role} (configure it in config/graph.yaml)")
        return cfg

    def _chat(self, role: str, messages: list, tools=None, num_predict=None) -> dict:
        cfg = self._role(role)
        return self.client.chat(
            cfg["model"], messages,
            tools=tools,
            num_ctx=int(cfg.get("num_ctx", 32768)),
            temperature=float(cfg.get("temperature", 0.2)),
            keep_alive=str(cfg.get("keep_alive", "60m")),
            num_predict=num_predict if num_predict is not None else cfg.get("num_predict"),
        )

    # ------------------------------------------------------------------ text
    def text(self, role: str, prompt: str, system: Optional[str] = None,
             num_predict: Optional[int] = None) -> dict:
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        start = time.time()
        resp = self._chat(role, messages, num_predict=num_predict)
        return {
            "result": strip_think((resp.get("message") or {}).get("content", "")),
            "tokens_in": resp.get("prompt_eval_count", 0),
            "tokens_out": resp.get("eval_count", 0),
            "duration_ms": int((time.time() - start) * 1000),
            "calls": 1,
            "is_error": False,
        }

    # ----------------------------------------------------------------- agent
    def agent(self, role: str, prompt: str, system: str, cwd: str,
              max_turns: int = 25, timeout: int = 900,
              on_progress: Optional[Callable[[str], None]] = None) -> dict:
        """Native tool-calling loop. Hard caps: max_turns and wall-clock timeout."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        start = time.time()
        tokens_in = tokens_out = calls = 0
        changed: list[str] = []
        final_text = ""
        stop_reason = "finished"

        for _turn in range(max_turns):
            if time.time() - start > timeout:
                stop_reason = "timeout"
                break
            resp = self._chat(role, messages, tools=TOOLS)
            calls += 1
            tokens_in += resp.get("prompt_eval_count", 0)
            tokens_out += resp.get("eval_count", 0)
            msg = resp.get("message", {}) or {}
            messages.append(msg)

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                final_text = strip_think(msg.get("content", "")) or final_text
                break

            done = False
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if name == "finish":
                    final_text = args.get("summary", "Task complete.")
                    done = True
                    break
                output, changed_path = run_tool(name, args, cwd)
                if changed_path:
                    changed.append(changed_path)
                if on_progress:
                    on_progress(f"{name}({str(args.get('path', args.get('command', '')))[:60]})")
                messages.append({"role": "tool", "tool_name": name, "content": output})
            if done:
                break
        else:
            stop_reason = "max_turns"

        return {
            "result": final_text or f"(stopped: {stop_reason})",
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "duration_ms": int((time.time() - start) * 1000),
            "calls": calls,
            "changed_files": sorted(set(changed)),
            "stop_reason": stop_reason,
            "is_error": stop_reason == "timeout" and not changed,
        }


# ---------------------------------------------------------------- agent tools
def _safe_path(cwd: str, rel: str) -> Path:
    base = Path(cwd).resolve()
    target = (base / rel).resolve()
    target.relative_to(base)  # raises ValueError on escape
    return target


def run_tool(name: str, args: dict, cwd: str) -> tuple[str, Optional[str]]:
    """Execute one agent tool. Returns (output_text, changed_path_or_None)."""
    try:
        if name == "read_file":
            p = _safe_path(cwd, args["path"])
            if not p.is_file():
                return f"ERROR: file not found: {args['path']}", None
            text = p.read_text(errors="replace")
            if len(text) > MAX_FILE_CHARS:
                text = text[:MAX_FILE_CHARS] + "\n...[truncated]..."
            return text, None

        if name == "write_file":
            p = _safe_path(cwd, args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args.get("content", ""))
            return f"Wrote {args['path']} ({len(args.get('content', ''))} chars)", args["path"]

        if name == "edit_file":
            p = _safe_path(cwd, args["path"])
            if not p.is_file():
                return f"ERROR: file not found: {args['path']}", None
            original = p.read_text(errors="replace")
            search = args.get("search", "")
            hits = original.count(search) if search else 0
            if hits == 0:
                return f"ERROR: search text not found in {args['path']} (re-read the file)", None
            if hits > 1:
                return f"ERROR: search text matches {hits} places in {args['path']} — make it unique", None
            p.write_text(original.replace(search, args.get("replace", ""), 1))
            return f"Edited {args['path']}", args["path"]

        if name == "list_dir":
            p = _safe_path(cwd, args.get("path", "."))
            if not p.is_dir():
                return f"ERROR: not a directory: {args.get('path', '.')}", None
            entries = sorted(e.name + ("/" if e.is_dir() else "") for e in p.iterdir())
            return "\n".join(entries) or "(empty)", None

        if name == "run_command":
            try:
                proc = subprocess.run(
                    args["command"], shell=True, cwd=cwd,
                    capture_output=True, text=True, timeout=300,
                )
            except subprocess.TimeoutExpired:
                return "ERROR: command timed out (300s)", None
            out = (proc.stdout or "") + (proc.stderr or "")
            if len(out) > MAX_CMD_OUTPUT:
                out = out[:MAX_CMD_OUTPUT] + "\n...[truncated]..."
            return f"(exit {proc.returncode})\n{out}".strip(), None

        return f"ERROR: unknown tool {name}", None
    except ValueError:
        return f"ERROR: path escapes working directory: {args.get('path')}", None
    except KeyError as e:
        return f"ERROR: missing argument {e}", None
    except Exception as e:  # noqa: BLE001 - surface to the model, don't crash the loop
        return f"ERROR: {type(e).__name__}: {e}", None


def load_roles(graph_cfg: dict) -> dict:
    """Extract the models section from a loaded graph config."""
    return graph_cfg.get("models", {})
