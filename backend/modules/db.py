"""Shared foundation for local SQLite storage.

Everything that "starts accruing the day you install it" lands in one database
(`~/.floorzero/history.db`); each module registers its own tables.

⚠️ **Not inside the repository.** Updating the code or re-cloning must not cost the
user the history they have accrued (VibeResearch issue #12 was exactly this).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

DEFAULT_DIR = os.environ.get("FZ_DATA_DIR") or os.path.expanduser("~/.floorzero")
DB_PATH = os.path.join(DEFAULT_DIR, "history.db")

_LOCK = threading.Lock()
_applied: set[str] = set()


def ensure_schema(name: str, schema_sql: str) -> None:
    """Create tables (idempotent). `name` only guards against repeating within one process."""
    if name in _applied:
        return
    with _LOCK:
        if name in _applied:
            return
        os.makedirs(DEFAULT_DIR, exist_ok=True)
        with sqlite3.connect(DB_PATH) as conn:
            conn.executescript(schema_sql)
        _applied.add(name)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Take a connection (rows addressable by name). Callers must `ensure_schema` first."""
    os.makedirs(DEFAULT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
