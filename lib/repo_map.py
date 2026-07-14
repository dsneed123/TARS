"""Per-project repo map with a git-HEAD-keyed cache.

Builds a compact, deterministic picture of a repository — file tree, extracted
top-level symbols, README head — so every pipeline node can "know" the repo
without re-exploring it, and without spending an LLM call. Cached at
<repo>/.tars/repo_map.json and rebuilt only when git HEAD moves (or the file
set changes for untracked-heavy repos).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger("tars.repo_map")

GIT_CMD = os.environ.get("GIT_CMD", "git")
CACHE_REL = ".tars/repo_map.json"

_SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".lock", ".map", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".min.js",
}

# language -> regex for top-level symbol extraction (cheap, line-based)
_SYMBOL_RES = {
    ".py": re.compile(r"^(?:class|def|async def)\s+(\w+)", re.M),
    ".js": re.compile(r"^(?:export\s+)?(?:async\s+)?(?:function|class)\s+(\w+)|^(?:export\s+)?const\s+(\w+)\s*=", re.M),
    ".ts": re.compile(r"^(?:export\s+)?(?:async\s+)?(?:function|class|interface|type)\s+(\w+)|^(?:export\s+)?const\s+(\w+)\s*=", re.M),
    ".go": re.compile(r"^(?:func|type)\s+(\w+)", re.M),
    ".sh": re.compile(r"^(\w+)\s*\(\)\s*\{", re.M),
    ".rb": re.compile(r"^\s*(?:class|module|def)\s+([\w.]+)", re.M),
}
_SYMBOL_RES[".tsx"] = _SYMBOL_RES[".ts"]
_SYMBOL_RES[".jsx"] = _SYMBOL_RES[".js"]


def _git(work_dir: str, *args: str, timeout: int = 30) -> str:
    try:
        return subprocess.run(
            [GIT_CMD, *args], cwd=work_dir, capture_output=True, text=True,
            timeout=timeout,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _head(work_dir: str) -> str:
    return _git(work_dir, "rev-parse", "HEAD").strip() or "no-head"


def build_map(work_dir: str, max_files: int = 400, symbol_files: int = 120) -> dict:
    """Build the map fresh (no cache). Deterministic, no LLM involved."""
    files = [f for f in _git(work_dir, "ls-files").splitlines() if f.strip()]
    entries = []
    base = Path(work_dir)
    for rel in files[:max_files]:
        p = base / rel
        try:
            size = p.stat().st_size
        except OSError:
            continue
        entries.append({"path": rel, "size": size})

    # Symbols for the biggest code files first — they define the project.
    code = [e for e in entries
            if Path(e["path"]).suffix in _SYMBOL_RES
            and not any(e["path"].endswith(s) for s in _SKIP_EXT)]
    code.sort(key=lambda e: -e["size"])
    symbols: dict[str, list[str]] = {}
    for e in code[:symbol_files]:
        p = base / e["path"]
        try:
            text = p.read_text(errors="replace")[:120_000]
        except OSError:
            continue
        rx = _SYMBOL_RES[Path(e["path"]).suffix]
        names = [next(g for g in m.groups() if g) for m in rx.finditer(text) if any(m.groups())]
        if names:
            symbols[e["path"]] = names[:40]

    readme = ""
    for cand in ("README.md", "README.rst", "readme.md"):
        rp = base / cand
        if rp.exists():
            try:
                readme = rp.read_text(errors="replace")[:2000]
            except OSError:
                pass
            break

    return {
        "head": _head(work_dir),
        "file_count": len(files),
        "files": entries,
        "symbols": symbols,
        "readme_head": readme,
    }


def get_map(work_dir: str) -> dict:
    """Return the repo map, using the cache when HEAD hasn't moved."""
    cache = Path(work_dir) / CACHE_REL
    head = _head(work_dir)
    if cache.exists():
        try:
            data = json.loads(cache.read_text())
            if data.get("head") == head:
                return data
        except (OSError, json.JSONDecodeError):
            pass
    data = build_map(work_dir)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data))
    except OSError as e:
        logger.debug("repo map cache write failed: %s", e)
    return data


def render(work_dir: str, max_chars: int = 6000) -> str:
    """Render the map as compact text for a prompt, within a char budget."""
    m = get_map(work_dir)
    if not m.get("files"):
        return "(empty repository — no files yet)"
    lines = [f"REPO MAP ({m['file_count']} files):"]
    for e in m["files"]:
        syms = m["symbols"].get(e["path"])
        if syms:
            lines.append(f"  {e['path']}  [{', '.join(syms[:12])}]")
        else:
            lines.append(f"  {e['path']}")
    text = "\n".join(lines)
    if m.get("readme_head"):
        text += "\n\nREADME (head):\n" + m["readme_head"]
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[map truncated]..."
    return text
