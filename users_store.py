"""
JSON-backed user store for the voice assistant's own login/register system.

This is separate from the admin panel's credentials (ADMIN_USERNAME /
ADMIN_PASSWORD in the environment) — these are end-user accounts, created
via /register, needed to use the
voice assistant itself. Passwords are never stored in plain text: each is
hashed with PBKDF2-HMAC-SHA256 and a random per-user salt.

users.json shape:
{
  "some_username": {"salt": "<hex>", "hash": "<hex>", "created": "<iso8601>"}
}
"""

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import storage

BASE_DIR = Path(__file__).resolve().parent
USERS_PATH = BASE_DIR / "users.json"

PBKDF2_ITERATIONS = 200_000
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")


def _load() -> dict:
    if not USERS_PATH.exists():
        return {}
    try:
        data = json.loads(USERS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(users: dict) -> None:
    USERS_PATH.write_text(json.dumps(users, indent=2), encoding="utf-8")
    storage.save(USERS_PATH)


def _hash_password(password: str, salt: Optional[bytes] = None) -> tuple[str, str]:
    """Return (salt_hex, hash_hex). Generates a new salt if none is given."""
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex(), digest.hex()


def validate_username(username: str) -> Optional[str]:
    """Return an error message if invalid, else None."""
    if not username:
        return "ইউজারনেম দিতে হবে"
    if not USERNAME_RE.match(username):
        return "ইউজারনেম ৩-৩২ অক্ষরের হতে হবে (শুধু ইংরেজি অক্ষর, সংখ্যা, . _ -)"
    return None


def validate_password(password: str) -> Optional[str]:
    if not password or len(password) < 6:
        return "পাসওয়ার্ড অন্তত ৬ অক্ষরের হতে হবে"
    return None


def user_exists(username: str) -> bool:
    return username.lower() in {u.lower() for u in _load().keys()}


def create_user(username: str, password: str) -> None:
    """Raises ValueError with a Bengali message on any validation failure."""
    err = validate_username(username)
    if err:
        raise ValueError(err)
    err = validate_password(password)
    if err:
        raise ValueError(err)
    if user_exists(username):
        raise ValueError("এই ইউজারনেম আগে থেকেই আছে")

    users = _load()
    salt_hex, hash_hex = _hash_password(password)
    users[username] = {
        "salt": salt_hex,
        "hash": hash_hex,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    _save(users)


def verify_user(username: str, password: str) -> bool:
    users = _load()
    record = users.get(username)
    if not record:
        return False
    salt = bytes.fromhex(record["salt"])
    _, computed_hash = _hash_password(password, salt)
    return secrets.compare_digest(computed_hash, record["hash"])


def user_count() -> int:
    return len(_load())
