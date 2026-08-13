import sqlite3
from pathlib import Path

from mailbox_channels.sqlite_limits import MAX_SQLITE_ENV, apply_sqlite_limit


def test_sqlite_page_count_is_bounded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(MAX_SQLITE_ENV, str(10 * 4096))
    path = tmp_path / "bounded.sqlite3"
    connection = sqlite3.connect(path)
    try:
        apply_sqlite_limit(connection, path)
        assert connection.execute("PRAGMA max_page_count").fetchone()[0] == 10
    finally:
        connection.close()
