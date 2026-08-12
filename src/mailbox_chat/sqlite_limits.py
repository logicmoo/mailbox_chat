"""Apply a hard maximum page count to relay-owned SQLite databases."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


MAX_SQLITE_ENV = "MAILBOX_RELAY_MAX_SQLITE_BYTES"
DEFAULT_MAX_SQLITE_BYTES = 1024 * 1024 * 1024


def apply_sqlite_limit(connection: sqlite3.Connection, path: Path) -> None:
    maximum = int(os.environ.get(MAX_SQLITE_ENV, DEFAULT_MAX_SQLITE_BYTES))
    if maximum < 1:
        raise ValueError("SQLite size limit must be positive")
    if path.exists() and path.stat().st_size > maximum:
        raise ValueError(f"SQLite database exceeds its {maximum}-byte limit: {path}")
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    maximum_pages = max(1, maximum // page_size)
    connection.execute(f"PRAGMA max_page_count={maximum_pages}")
