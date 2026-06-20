"""TARS API key store — per-user keys for multi-user access.

Keys are stored as SHA-256 hashes in state/api_keys.json (gitignored). The
plaintext key is shown exactly once, at creation. Lookups hash the presented
key and constant-time compare. The master TARS_API_KEY (from tars.conf) is
always admin and is handled separately by the controller.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))
STATE_DIR = TARS_HOME / "state"
KEYS_FILE = STATE_DIR / "api_keys.json"

KEY_PREFIX = "tars_"
DEFAULT_SCOPES = ["chat", "tasks", "projects"]


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _read() -> dict:
    try:
        with open(KEYS_FILE) as f:
            data = json.load(f)
            if isinstance(data, dict) and "keys" in data:
                return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"keys": []}


def _write(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = KEYS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    tmp.rename(KEYS_FILE)


def _public(rec: dict) -> dict:
    """A record safe to return over the API (no hash)."""
    return {k: v for k, v in rec.items() if k != "key_hash"}


def generate_key(user: str, scopes: Optional[list[str]] = None, is_admin: bool = False) -> dict:
    """Create a new key for `user`. Returns the record WITH the plaintext key
    under "key" — this is the only time the plaintext is available."""
    secret = KEY_PREFIX + secrets.token_hex(24)
    rec = {
        "id": secrets.token_hex(6),
        "user": user,
        "key_prefix": secret[:12],
        "key_hash": _hash(secret),
        "scopes": scopes or list(DEFAULT_SCOPES),
        "is_admin": is_admin,
        "projects": [],
        "created": int(time.time()),
        "revoked": False,
    }
    data = _read()
    data["keys"].append(rec)
    _write(data)
    out = _public(rec)
    out["key"] = secret
    return out


def list_keys() -> list[dict]:
    return [_public(r) for r in _read()["keys"]]


def revoke_key(key_id: str) -> bool:
    data = _read()
    found = False
    for r in data["keys"]:
        if r["id"] == key_id and not r.get("revoked"):
            r["revoked"] = True
            found = True
    if found:
        _write(data)
    return found


def lookup(provided: str) -> Optional[dict]:
    """Return the (non-revoked) key record matching the presented key, or None."""
    if not provided:
        return None
    h = _hash(provided)
    for r in _read()["keys"]:
        if r.get("revoked"):
            continue
        if hmac.compare_digest(r.get("key_hash", ""), h):
            return _public(r)
    return None


def add_project(key_id: str, project: str) -> bool:
    """Associate a project with a key (used by onboarding for ownership/scoping)."""
    data = _read()
    found = False
    for r in data["keys"]:
        if r["id"] == key_id:
            if project not in r["projects"]:
                r["projects"].append(project)
            found = True
    if found:
        _write(data)
    return found
