"""TARS Ollama agent runner — the local-model replacement for ClaudeRunner.

Ollama only returns text/tool-calls; it is NOT an agent. This module wraps a
local model with the agent loop TARS needs: the model can read/write files and
run commands in a working directory, so it can autonomously implement and fix
code the same way `claude -p` did. It emits the exact same result dict shape
ClaudeRunner did, so the worker's `jq` parsing and every call site keep working.

Execution modes (chosen by role):
  - "agent": native tool-calling loop (qwen-coder for `implement`)
  - "edit":  text-directive loop with READ/RUN/EDIT blocks, for reasoning
             models that lack reliable tool-calling (deepseek-r1 for `fix`)
  - "text":  single completion, no file editing (review / plan / chat)
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

try:
    from ollama_client import OllamaClient, OllamaError
except ImportError:  # imported as lib.ollama_runner (PYTHONPATH=TARS_HOME)
    from lib.ollama_client import OllamaClient, OllamaError

logger = logging.getLogger("tars.ollama_runner")

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))
PROMPTS_DIR = TARS_HOME / "prompts"

# Claude model aliases that carry no meaning for Ollama — ignore and use the
# role default model instead.
_CLAUDE_ALIASES = {"sonnet", "opus", "haiku", "claude"}

# Map prompt template -> logical role. Legacy call sites invoke the LLM via
# run_with_prompt_file(), so the filename reliably tells us the role.
ROLE_BY_PROMPT = {
    "discover_improvements.md": "plan",
    "self_improve.md": "plan",
    "go_plan.md": "plan",
    "go_review.md": "review",
}

# Per-role default model + execution mode. Models are overridable via env.
ROLE_MODEL_ENV = {
    "code": ("OLLAMA_CODE_MODEL", "qwen2.5-coder:32b"),
    "fix": ("OLLAMA_FIX_MODEL", "deepseek-r1:70b"),
    "review": ("OLLAMA_REVIEW_MODEL", "deepseek-r1:70b"),
    "plan": ("OLLAMA_PLAN_MODEL", "deepseek-r1:70b"),
    "chat": ("OLLAMA_CHAT_MODEL", "qwen2.5:7b"),
}

NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))
MAX_FILE_CHARS = int(os.environ.get("OLLAMA_MAX_FILE_CHARS", "60000"))
MAX_CMD_OUTPUT = int(os.environ.get("OLLAMA_MAX_CMD_OUTPUT", "12000"))


def resolve_model_for_role(role: str) -> str:
    env_key, default = ROLE_MODEL_ENV.get(role, ROLE_MODEL_ENV["chat"])
    return os.environ.get(env_key, default)


def managed_models() -> set[str]:
    """All Ollama models TARS manages (one per role). Auto-swap only ever
    unloads models in this set — never a model the user loaded for themselves."""
    return {resolve_model_for_role(role) for role in ROLE_MODEL_ENV}


# Spin the right model up and the others down before each run. Disable with
# TARS_OLLAMA_AUTO_SWAP=0 (e.g. on a big box where everything fits in VRAM).
AUTO_SWAP = os.environ.get("TARS_OLLAMA_AUTO_SWAP", "1").lower() not in ("0", "false", "no")


def is_reasoning_model(model: str) -> bool:
    """Reasoning models (deepseek-r1, qwq, etc.) emit <think> and lack reliable
    tool-calling — they use the text 'edit' loop rather than native tools."""
    m = model.lower()
    return "r1" in m or "qwq" in m or "reasoning" in m or "marco-o1" in m


def strip_think(text: str) -> str:
    """Remove <think>...</think> reasoning spans emitted by r1-style models."""
    if not text:
        return text
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Handle an unterminated opening tag: keep only what follows the last close,
    # or drop a dangling open block entirely.
    if "<think>" in text.lower():
        idx = text.lower().rfind("</think>")
        if idx != -1:
            text = text[idx + len("</think>"):]
        else:
            text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


# --------------------------------------------------------------------------- tools
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's contents. Path is relative to the working directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given full contents. Path is relative to the working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories at a path relative to the working directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the working directory and return its output. Use for building, running tests, grep, etc.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Call this when the task is fully complete. Provide a short summary of what was changed.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]


class OllamaRunner:
    """Drop-in replacement for ClaudeRunner, backed by a local Ollama model."""

    def __init__(
        self,
        model: str = "sonnet",
        max_turns: int = 20,
        allowed_tools: Optional[list[str]] = None,
        host: Optional[str] = None,
    ):
        self.model = model
        self.max_turns = max_turns
        self.allowed_tools = allowed_tools
        self.client = OllamaClient(host=host)

    # ------------------------------------------------------------- public interface
    def run(
        self,
        prompt: str,
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        max_turns: Optional[int] = None,
        timeout: int = 600,
        append_system: Optional[str] = None,
        role: Optional[str] = None,
    ) -> dict:
        """Run the local model. Mirrors ClaudeRunner.run()'s return contract."""
        role = role or "chat"
        resolved_model = self._resolve_model(model, role)
        turns = max_turns or self.max_turns
        start = time.time()

        # Spin the right model up and the others down so a 32B coder and a 70B
        # reasoner don't sit resident together and exhaust VRAM.
        self._swap_to(resolved_model)

        # Persistent project context: re-load the saved digest so small-context
        # local models "understand" the repo without re-reading everything.
        ctx = self._load_context(cwd)
        if ctx:
            header = "Persisted project context (read before acting):\n" + ctx
            append_system = header + ("\n\n" + append_system if append_system else "")

        logger.info("Ollama run: role=%s model=%s cwd=%s", role, resolved_model, cwd)

        try:
            if role in ("code", "fix"):
                if is_reasoning_model(resolved_model):
                    return self._run_edit(prompt, resolved_model, cwd, turns, start, append_system)
                return self._run_agent(prompt, resolved_model, cwd, turns, start, append_system)
            # Bound output length for speed — small for chat, roomier for
            # review/plan (which emit JSON). 0/unset env keeps it generous.
            if role == "chat":
                num_predict = int(os.environ.get("OLLAMA_CHAT_NUM_PREDICT", "768"))
            else:
                num_predict = int(os.environ.get("OLLAMA_TEXT_NUM_PREDICT", "2048"))
            return self._run_text(prompt, resolved_model, cwd, start, append_system,
                                  num_predict=num_predict or None)
        except OllamaError as e:
            logger.error("Ollama error (role=%s): %s", role, e)
            return self._result(f"Ollama error: {e}", 0, 0, start, is_error=True)

    def chat_messages(self, messages, model=None, system=None, num_predict=None) -> dict:
        """Role-based chat: pass proper {role, content} messages so the model
        emits exactly ONE assistant turn and stops — instead of continuing the
        whole conversation (writing both sides) like a single text transcript does."""
        resolved = self._resolve_model(model, "chat")
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        for m in messages:
            role = m.get("role", "user")
            if role not in ("user", "assistant", "system"):
                role = "user"
            msgs.append({"role": role, "content": m.get("content", "")})
        start = time.time()
        try:
            resp = self.client.chat(
                resolved, msgs, num_ctx=NUM_CTX, num_predict=num_predict,
                # Safety net: never let it start a new turn for the user.
                stop=["\n[User]:", "[User]:", "\nUser:", "\n[USER]:"],
            )
        except OllamaError as e:
            return self._result(f"Ollama error: {e}", 0, 0, start, is_error=True)
        msg = resp.get("message", {}) or {}
        text = strip_think(msg.get("content", "")).strip()
        return self._result(text, resp.get("prompt_eval_count", 0),
                           resp.get("eval_count", 0), start)

    def run_with_prompt_file(
        self, prompt_file: str, variables: Optional[dict] = None, **kwargs
    ) -> dict:
        """Load a prompt template, substitute {{VARS}}, and run with the role
        inferred from the template filename."""
        path = PROMPTS_DIR / prompt_file
        if not path.exists():
            raise FileNotFoundError(f"Prompt template not found: {path}")
        template = path.read_text()
        if variables:
            for key, val in variables.items():
                template = template.replace(f"{{{{{key}}}}}", str(val))
        kwargs.setdefault("role", ROLE_BY_PROMPT.get(prompt_file, "chat"))
        return self.run(template, **kwargs)

    # ------------------------------------------------------------------- helpers
    def _resolve_model(self, model: Optional[str], role: str) -> str:
        """Honor an explicit Ollama model name; otherwise pick the role default.
        Claude aliases (sonnet/opus/haiku) are meaningless locally — ignore them."""
        if model and model.lower() not in _CLAUDE_ALIASES:
            return model
        if self.model and self.model.lower() not in _CLAUDE_ALIASES:
            return self.model
        return resolve_model_for_role(role)

    def _swap_to(self, model: str) -> None:
        """Ensure `model` is loaded and unload the other TARS-managed models to
        free VRAM. Best-effort: model management must never break a task."""
        if not AUTO_SWAP:
            return
        try:
            loaded = set(self.client.loaded_models())
        except OllamaError as e:
            logger.debug("auto-swap: could not list loaded models: %s", e)
            return

        # Match by base name too — /api/ps may report a fully-qualified tag.
        def same(a: str, b: str) -> bool:
            return a == b or a.split(":")[0] == b.split(":")[0]

        for other in managed_models():
            if same(other, model):
                continue
            if any(same(lm, other) for lm in loaded):
                try:
                    logger.info("auto-swap: unloading %s", other)
                    self.client.unload_model(other)
                except OllamaError as e:
                    logger.debug("auto-swap: unload %s failed: %s", other, e)

        if not any(same(lm, model) for lm in loaded):
            try:
                logger.info("auto-swap: loading %s", model)
                self.client.load_model(model)
            except OllamaError as e:
                logger.debug("auto-swap: load %s failed: %s", model, e)

    def _result(self, text, tokens_in, tokens_out, start, is_error=False) -> dict:
        return {
            "result": text,
            "cost_usd": 0.0,  # local inference is free
            "duration_ms": int((time.time() - start) * 1000),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "is_error": is_error,
            "session_warning": False,
        }

    CONTEXT_REL = ".tars/context.md"
    MAX_CONTEXT_CHARS = int(os.environ.get("OLLAMA_MAX_CONTEXT_CHARS", "8000"))

    def _load_context(self, cwd: Optional[str]) -> str:
        """Load the persisted per-project context digest, if present."""
        if not cwd:
            return ""
        p = Path(cwd) / self.CONTEXT_REL
        try:
            if p.exists():
                return p.read_text(errors="replace")[: self.MAX_CONTEXT_CHARS]
        except OSError:
            return ""
        return ""

    def generate_context(self, cwd: str) -> str:
        """Generate and persist a project context digest at <cwd>/.tars/context.md.
        Returns the digest text (empty string on failure)."""
        base = Path(cwd)
        if not base.exists():
            return ""
        # Cheap repo snapshot: top-level entries + a shallow file tree.
        try:
            listing = subprocess.run(
                "git ls-files 2>/dev/null | head -200 || find . -maxdepth 2 -type f | head -200",
                shell=True, cwd=cwd, capture_output=True, text=True, timeout=30,
            ).stdout
        except subprocess.TimeoutExpired:
            listing = ""
        # Skip the (slow) digest for a freshly-bootstrapped/near-empty repo —
        # there's nothing to summarize and it would just block the task.
        tracked = [f for f in listing.splitlines() if f.strip() and f.strip() != "README.md"]
        if not tracked:
            logger.info("Skipping context digest: repo has no code yet")
            return ""
        readme = ""
        for cand in ("README.md", "README.rst", "readme.md"):
            rp = base / cand
            if rp.exists():
                readme = rp.read_text(errors="replace")[:4000]
                break
        prompt = (
            "You are documenting a code repository so a teammate can get up to "
            "speed fast. Using the file list and README below, write a concise "
            "context digest in Markdown covering: what the project is, the main "
            "architecture/entry points, key directories/files, conventions, and "
            "how to build/test. Keep it under 400 lines. Output only the Markdown.\n\n"
            f"FILES:\n{listing}\n\nREADME:\n{readme}"
        )
        plan_model = resolve_model_for_role("plan")
        self._swap_to(plan_model)  # don't load the digest model alongside the coder
        res = self._run_text(prompt, plan_model, cwd, time.time(), None)
        digest = res.get("result", "") or ""
        if digest:
            out = base / self.CONTEXT_REL
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(digest)
        return digest

    def _safe_path(self, cwd: Optional[str], rel: str) -> Path:
        base = Path(cwd or ".").resolve()
        target = (base / rel).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            raise OllamaError(f"Path escapes working directory: {rel}")
        return target

    # ----------------------------------------------------------------- text mode
    def _run_text(self, prompt, model, cwd, start, append_system, num_predict=None) -> dict:
        messages = []
        if append_system:
            messages.append({"role": "system", "content": append_system})
        messages.append({"role": "user", "content": prompt})
        resp = self.client.chat(model, messages, num_ctx=NUM_CTX, num_predict=num_predict)
        msg = resp.get("message", {})
        text = strip_think(msg.get("content", ""))
        return self._result(
            text,
            resp.get("prompt_eval_count", 0),
            resp.get("eval_count", 0),
            start,
        )

    # ---------------------------------------------------------------- agent mode
    def _run_agent(self, prompt, model, cwd, max_turns, start, append_system) -> dict:
        system = (
            os.environ.get("TARS_PERSONA", "You are TARS, an AI assistant for software development and business, built by Davis Sneed. You help users write, test, and ship code and handle business tasks. You are a coding and business tool — NOT a military, combat, defense, or science-fiction system.")
            + " You are an autonomous coding agent working inside the "
            f"directory `{cwd}`. Use the provided tools to inspect and modify files "
            "and to run commands (builds, tests). All paths are relative to the "
            "working directory. Make the changes directly — do not ask the user "
            "questions. When the task is fully complete, call `finish` with a short "
            "summary of what you changed."
        )
        if append_system:
            system += "\n\n" + append_system
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

        tokens_in = tokens_out = 0
        changed: list[str] = []
        final_text = ""

        for turn in range(max_turns):
            resp = self.client.chat(model, messages, tools=TOOL_SCHEMAS, num_ctx=NUM_CTX)
            msg = resp.get("message", {}) or {}
            tokens_in += resp.get("prompt_eval_count", 0)
            tokens_out += resp.get("eval_count", 0)
            messages.append(msg)

            # Ollama ≥0.32 parses tool calls natively (renderer/parser per
            # model) — no calls means the model considers itself done.
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

                output, file_changed = self._exec_tool(name, args, cwd)
                if file_changed:
                    changed.append(file_changed)
                messages.append({"role": "tool", "tool_name": name, "content": output})

            if done:
                break
        else:
            logger.warning("Agent hit max_turns=%d without finishing", max_turns)
            final_text = final_text or "Reached max turns."

        if changed:
            uniq = sorted(set(changed))
            final_text = (final_text + f"\n\nFiles changed: {', '.join(uniq)}").strip()
        return self._result(final_text, tokens_in, tokens_out, start)

    def _exec_tool(self, name: str, args: dict, cwd: Optional[str]):
        """Execute one agent tool. Returns (output_text, changed_path_or_None)."""
        try:
            if name == "read_file":
                p = self._safe_path(cwd, args["path"])
                if not p.exists():
                    return f"ERROR: file not found: {args['path']}", None
                text = p.read_text(errors="replace")
                if len(text) > MAX_FILE_CHARS:
                    text = text[:MAX_FILE_CHARS] + "\n...[truncated]..."
                return text, None

            if name == "write_file":
                p = self._safe_path(cwd, args["path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(args.get("content", ""))
                return f"Wrote {args['path']} ({len(args.get('content', ''))} bytes)", args["path"]

            if name == "list_dir":
                p = self._safe_path(cwd, args.get("path", "."))
                if not p.exists():
                    return f"ERROR: not found: {args.get('path', '.')}", None
                entries = sorted(
                    (e.name + ("/" if e.is_dir() else "")) for e in p.iterdir()
                )
                return "\n".join(entries) or "(empty)", None

            if name == "run_command":
                return self._run_command(args["command"], cwd), None

            return f"ERROR: unknown tool {name}", None
        except OllamaError as e:
            return f"ERROR: {e}", None
        except Exception as e:  # noqa: BLE001 - surface to model, don't crash loop
            return f"ERROR: {type(e).__name__}: {e}", None

    def _run_command(self, command: str, cwd: Optional[str]) -> str:
        try:
            proc = subprocess.run(
                command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=300
            )
        except subprocess.TimeoutExpired:
            return "ERROR: command timed out (300s)"
        out = (proc.stdout or "") + (proc.stderr or "")
        if len(out) > MAX_CMD_OUTPUT:
            out = out[:MAX_CMD_OUTPUT] + "\n...[truncated]..."
        return f"(exit {proc.returncode})\n{out}".strip()

    # ----------------------------------------------------------------- edit mode
    # Text-directive loop for reasoning models (deepseek-r1) that can't reliably
    # tool-call. The model uses plain-text directives we parse and act on:
    #   <<<READ path>>>                  (request a file's contents)
    #   <<<RUN command>>>                (run a shell command)
    #   <<<EDIT path>>> <<<SEARCH>>> old <<<REPLACE>>> new <<<END>>>
    _EDIT_RE = re.compile(
        r"<<<EDIT\s+(?P<path>.+?)>>>\s*"
        r"<<<SEARCH>>>(?P<search>.*?)"
        r"<<<REPLACE>>>(?P<replace>.*?)"
        r"<<<END>>>",
        re.DOTALL,
    )
    _READ_RE = re.compile(r"<<<READ\s+(.+?)>>>")
    _RUN_RE = re.compile(r"<<<RUN\s+(.+?)>>>")

    def _run_edit(self, prompt, model, cwd, max_turns, start, append_system) -> dict:
        system = (
            os.environ.get("TARS_PERSONA", "You are TARS, an AI assistant for software development and business, built by Davis Sneed. You help users write, test, and ship code and handle business tasks. You are a coding and business tool — NOT a military, combat, defense, or science-fiction system.")
            + " You are a code-fixing agent working in directory "
            f"`{cwd}`. You cannot call functions, so you act using plain-text "
            "directives that the system executes for you:\n"
            "  <<<READ path>>>              — ask to see a file (relative path)\n"
            "  <<<RUN command>>>            — run a shell command (e.g. tests)\n"
            "  <<<EDIT path>>>\n"
            "  <<<SEARCH>>>\n"
            "  exact existing text to replace\n"
            "  <<<REPLACE>>>\n"
            "  new text\n"
            "  <<<END>>>                    — apply an edit (empty SEARCH = new file)\n\n"
            "Read the files you need before editing. The SEARCH text must match the "
            "file EXACTLY. Emit edits only when you are confident. When everything is "
            "done and verified, reply with the single line: DONE: <short summary>."
        )
        if append_system:
            system += "\n\n" + append_system
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

        tokens_in = tokens_out = 0
        changed: list[str] = []
        final_text = ""

        for turn in range(max_turns):
            resp = self.client.chat(model, messages, num_ctx=NUM_CTX, temperature=0.1)
            msg = resp.get("message", {}) or {}
            tokens_in += resp.get("prompt_eval_count", 0)
            tokens_out += resp.get("eval_count", 0)
            content = strip_think(msg.get("content", ""))
            messages.append({"role": "assistant", "content": content})

            edits = list(self._EDIT_RE.finditer(content))
            reads = self._READ_RE.findall(content)
            runs = self._RUN_RE.findall(content)

            if edits:
                feedback = []
                for m in edits:
                    res, path = self._apply_edit(
                        m.group("path").strip(), m.group("search"), m.group("replace"), cwd
                    )
                    if path:
                        changed.append(path)
                    feedback.append(res)
                messages.append({"role": "user", "content": "Edit results:\n" + "\n".join(feedback)})
                # Let it verify/continue unless it already declared completion.
                if "DONE:" in content:
                    final_text = content.split("DONE:", 1)[1].strip()
                    break
                continue

            if reads:
                parts = []
                for rel in reads:
                    p_out, _ = self._exec_tool("read_file", {"path": rel.strip()}, cwd)
                    parts.append(f"=== {rel.strip()} ===\n{p_out}")
                messages.append({"role": "user", "content": "\n\n".join(parts)})
                continue

            if runs:
                parts = [self._run_command(c.strip(), cwd) for c in runs]
                messages.append({"role": "user", "content": "Command output:\n" + "\n\n".join(parts)})
                continue

            # No directives -> treat as final answer.
            if "DONE:" in content:
                final_text = content.split("DONE:", 1)[1].strip()
            else:
                final_text = content
            break
        else:
            final_text = final_text or "Reached max turns."

        if changed:
            uniq = sorted(set(changed))
            final_text = (final_text + f"\n\nFiles changed: {', '.join(uniq)}").strip()
        return self._result(final_text, tokens_in, tokens_out, start)

    def _apply_edit(self, rel: str, search: str, replace: str, cwd: Optional[str]):
        """Apply one SEARCH/REPLACE edit. Returns (feedback, changed_path_or_None)."""
        try:
            p = self._safe_path(cwd, rel)
        except OllamaError as e:
            return f"FAILED {rel}: {e}", None

        search = search.strip("\n")
        replace = replace.strip("\n")

        # Empty SEARCH -> create/overwrite a new file.
        if not search:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(replace + ("\n" if not replace.endswith("\n") else ""))
            return f"OK {rel}: created", rel

        if not p.exists():
            return f"FAILED {rel}: file does not exist", None
        original = p.read_text(errors="replace")
        if search not in original:
            return f"FAILED {rel}: SEARCH text not found (re-read the file)", None
        p.write_text(original.replace(search, replace, 1))
        return f"OK {rel}: applied", rel
