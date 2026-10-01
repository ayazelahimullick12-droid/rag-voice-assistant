"""
Durable copies of the app's data files, for hosts with a throwaway disk.

Render's free tier wipes the local disk every time the service sleeps,
restarts or redeploys, which would silently lose registered users, saved
settings and uploaded documents. This module keeps a copy of those files
in a Postgres table (a free Neon database works fine):

  - restore() runs once at startup, before anything reads the files, and
    writes the stored copies back onto disk.
  - save(path) / remove(path) are called right after the app writes or
    deletes one of those files, and copy the change into the table.

The rest of the app keeps reading and writing its usual local files and
never talks to the database directly. Without DATABASE_URL every function
here is a no-op, so running locally works exactly as before.

Mirrored files (paths relative to this folder, also the table's key):
  users.json, settings.json, saved_keys.json, .embed_cache.json,
  knowledge/*.md|.txt|.markdown

saved_keys.json holds API keys saved from the admin panel on a host, so
those end up in the database too, unencrypted. Keys set in the host's own
environment settings, and the local .env file, are never mirrored.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator, Optional

BASE_DIR = Path(__file__).resolve().parent

TRACKED_FILES = {"users.json", "settings.json", "saved_keys.json", ".embed_cache.json"}
TRACKED_DIRS = {"knowledge": {".md", ".txt", ".markdown"}}

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS app_files (
    path       TEXT PRIMARY KEY,
    content    BYTEA,                          -- NULL marks a deleted file
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
_UPSERT = """
INSERT INTO app_files (path, content, updated_at) VALUES (%s, %s, now())
ON CONFLICT (path) DO UPDATE SET content = EXCLUDED.content, updated_at = now()
"""

# One background thread for writes: request handlers don't wait on the
# network, and writes still reach the database in the order they happened.
# Pending writes are flushed at interpreter exit (concurrent.futures joins
# its worker threads), so a normal shutdown doesn't drop them.
_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="storage")


def enabled() -> bool:
    # Read on every call rather than at import, so a DATABASE_URL loaded
    # from .env after this module is imported is still picked up.
    return bool(os.environ.get("DATABASE_URL", "").strip())


def _connect():
    import psycopg  # only needed when a database is configured
    return psycopg.connect(
        os.environ["DATABASE_URL"].strip(),
        autocommit=True,
        connect_timeout=20,
    )


def _is_tracked(rel: str) -> bool:
    parts = rel.split("/")
    if len(parts) == 1:
        return rel in TRACKED_FILES
    suffixes = TRACKED_DIRS.get(parts[0])
    return bool(suffixes) and ".." not in parts and Path(rel).suffix.lower() in suffixes


def _rel(path: Path) -> Optional[str]:
    """The table key for a local path, or None if that path isn't mirrored."""
    try:
        rel = Path(path).resolve().relative_to(BASE_DIR).as_posix()
    except ValueError:
        return None
    return rel if _is_tracked(rel) else None


def _local_tracked() -> Iterator[str]:
    for name in TRACKED_FILES:
        if (BASE_DIR / name).is_file():
            yield name
    for folder in TRACKED_DIRS:
        root = BASE_DIR / folder
        if root.is_dir():
            for p in root.rglob("*"):
                rel = _rel(p) if p.is_file() else None
                if rel:
                    yield rel


def restore(attempts: int = 5) -> None:
    """
    Bring the local files in line with the database. Call once at startup.

    Every stored file is written to disk (or deleted, if it was deleted
    through the app). Any mirrored local file the database has never seen —
    everything on the very first deploy, or a document added to the repo
    later — is uploaded, so the repo's files seed an empty database.

    Raises if the database can't be reached after a few tries: starting
    anyway from the repo's copies would let the next write overwrite newer
    data (e.g. registered users) with stale files.
    """
    if not enabled():
        if os.environ.get("RENDER"):  # set by Render on every service
            print("[storage] no DATABASE_URL - accounts, settings and uploads made here "
                  "are lost when the service restarts; only the repo's files come back")
        return

    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            _restore_once()
            return
        except Exception as exc:
            if attempt == attempts:
                raise RuntimeError(f"[storage] could not reach DATABASE_URL: {exc}") from exc
            print(f"[storage] database not reachable yet ({exc}); retrying in {delay:.0f}s")
            time.sleep(delay)
            delay *= 2


def _restore_once() -> None:
    with _connect() as conn:
        conn.execute(_CREATE_TABLE)
        rows = conn.execute("SELECT path, content FROM app_files").fetchall()

        stored = set()
        written = deleted = 0
        for rel, content in rows:
            stored.add(rel)
            if not _is_tracked(rel):
                continue
            target = BASE_DIR / rel
            if content is None:
                if target.exists():
                    target.unlink()
                    deleted += 1
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bytes(content))
                written += 1

        seeded = 0
        for rel in _local_tracked():
            if rel not in stored:
                conn.execute(_UPSERT, (rel, (BASE_DIR / rel).read_bytes()))
                seeded += 1

    print(f"[storage] restored {written} file(s), removed {deleted}, seeded {seeded} from the repo")


def _write(rel: str, content: Optional[bytes]) -> None:
    try:
        with _connect() as conn:
            conn.execute(_UPSERT, (rel, content))
    except Exception as exc:
        print(f"[storage] failed to save {rel}: {exc}")


def save(path: Path) -> None:
    """Copy a just-written local file to the database (in the background)."""
    rel = _rel(path) if enabled() else None
    if rel is None:
        return
    # Snapshot the bytes now, so a later write to the same file can't race
    # with this one while it waits in the queue.
    content = (BASE_DIR / rel).read_bytes()
    _writer.submit(_write, rel, content)


def remove(path: Path) -> None:
    """Record that a local file was deleted, so a restart doesn't bring it back."""
    rel = _rel(path) if enabled() else None
    if rel is None:
        return
    _writer.submit(_write, rel, None)


def flush(timeout: float = 30.0) -> None:
    """Block until every queued write has been sent (used by tests)."""
    _writer.submit(lambda: None).result(timeout=timeout)
