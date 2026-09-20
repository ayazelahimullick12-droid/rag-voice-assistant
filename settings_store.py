"""
Settings persistence for the BRAC voice assistant.

Everything the admin panel controls lives in settings.json, next to this
file. server.py reads it once at startup to build the Live session config
(voice, retrieval top-K, minimum score) and reads the behavior/interface
keys on every page load for the frontend to apply.

The Gemini API key is handled separately, in a .env file, because it's a
secret rather than a preference — see read_api_key / write_api_key below.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "settings.json"
ENV_PATH = BASE_DIR / ".env"

VALID_VOICES = ["Puck", "Charon", "Kore", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr"]

DEFAULTS: Dict[str, Any] = {
    "voice": "Kore",
    "echo_guard_default": True,
    "show_transcription": True,
    "show_only_assistant": False,
    "show_retrieval_logs": True,
    "min_score": 0.15,
    "rag_top_k": 4,
    "orb_speed": "normal",       # slow | normal | fast
    "font_size": "normal",       # small | normal | large
}


def load_settings() -> Dict[str, Any]:
    """Read settings.json, filling in any missing keys with defaults."""
    data = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                data.update(saved)
        except (json.JSONDecodeError, OSError):
            pass  # corrupt or unreadable file — fall back to defaults
    return _validate(data)


def save_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge patch into current settings, validate, persist, and return the result."""
    current = load_settings()
    current.update(patch)
    current = _validate(current)
    SETTINGS_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def _validate(data: Dict[str, Any]) -> Dict[str, Any]:
    """Clamp/normalize values so a bad admin-panel POST can't produce a broken config."""
    if data.get("voice") not in VALID_VOICES:
        data["voice"] = DEFAULTS["voice"]

    for key in ("echo_guard_default", "show_transcription", "show_only_assistant", "show_retrieval_logs"):
        data[key] = bool(data.get(key, DEFAULTS[key]))

    try:
        score = float(data.get("min_score", DEFAULTS["min_score"]))
    except (TypeError, ValueError):
        score = DEFAULTS["min_score"]
    data["min_score"] = round(min(0.30, max(0.10, score)), 2)

    try:
        k = int(data.get("rag_top_k", DEFAULTS["rag_top_k"]))
    except (TypeError, ValueError):
        k = DEFAULTS["rag_top_k"]
    data["rag_top_k"] = k if k in (2, 4, 6, 8) else DEFAULTS["rag_top_k"]

    if data.get("orb_speed") not in ("slow", "normal", "fast"):
        data["orb_speed"] = DEFAULTS["orb_speed"]

    if data.get("font_size") not in ("small", "normal", "large"):
        data["font_size"] = DEFAULTS["font_size"]

    return data


# ---------------------------------------------------------------------------
# API key(s) — stored in .env, not settings.json, and never echoed in full.
#
# GEMINI_API_KEY is the primary key: the voice assistant's Live sessions and
# RAG embeddings always use it.
#
# UPLOAD_GEMINI_API_KEY is optional and independent — the document-upload
# app (upload_server.py) checks this first, and only falls back to the
# shared GEMINI_API_KEY if this one was never set. This lets you run the
# two apps on separate keys (separate quota, separate billing project,
# separate usage tracking) without disturbing the single-key setup most
# people start with.
# ---------------------------------------------------------------------------
def _read_env_var(var_name: str) -> str:
    """Environment wins, then .env file, for the given variable name."""
    if os.environ.get(var_name):
        return os.environ[var_name]
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{var_name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _write_env_var(var_name: str, new_value: str) -> None:
    """
    Persist a value to .env under var_name and the current process's
    environment.

    Only this running process picks it up immediately (os.environ is
    per-process) — a fresh session opened after this call uses the new
    value. A separate process (or one you restart) reads .env the next
    time it starts, since .env is not auto-reloaded across processes.
    """
    new_value = new_value.strip()
    if not new_value:
        raise ValueError("Value cannot be empty")

    lines = []
    replaced = False
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{var_name}="):
                lines.append(f"{var_name}={new_value}")
                replaced = True
            else:
                lines.append(line)
    if not replaced:
        lines.append(f"{var_name}={new_value}")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[var_name] = new_value


def _mask(key: str) -> str:
    if not key:
        return ""
    return f"{'•' * max(0, len(key) - 4)}{key[-4:]}"


def read_api_key() -> str:
    """The primary key — used by the voice assistant's Live sessions and RAG embeddings."""
    return _read_env_var("GEMINI_API_KEY")


def write_api_key(new_key: str) -> None:
    if not new_key.strip():
        raise ValueError("API key cannot be empty")
    _write_env_var("GEMINI_API_KEY", new_key)


def masked_api_key() -> str:
    """A display-safe version of the primary key: last 4 chars only, or empty string."""
    return _mask(read_api_key())


def read_upload_api_key() -> str:
    """
    The document-processing key. Checks UPLOAD_GEMINI_API_KEY first; if that
    was never set, falls back to the shared GEMINI_API_KEY so a fresh setup
    with only one key configured still works everywhere.
    """
    dedicated = _read_env_var("UPLOAD_GEMINI_API_KEY")
    return dedicated if dedicated else read_api_key()


def write_upload_api_key(new_key: str) -> None:
    """Set a document-processing key independent of the shared GEMINI_API_KEY."""
    if not new_key.strip():
        raise ValueError("Upload API key cannot be empty")
    _write_env_var("UPLOAD_GEMINI_API_KEY", new_key)


def upload_key_is_dedicated() -> bool:
    """True if a separate upload key has been explicitly set (not just
    falling back to the shared one) — lets the UI show which is in effect."""
    return bool(_read_env_var("UPLOAD_GEMINI_API_KEY"))


def masked_upload_api_key() -> str:
    """Display-safe version of whichever key document processing is actually using."""
    return _mask(read_upload_api_key())