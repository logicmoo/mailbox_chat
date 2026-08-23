"""Durable origin and endpoint traversal ledger for relay loop prevention."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .sqlite_limits import apply_sqlite_limit


def origin_id(message: dict[str, Any]) -> str:
    """Return an immutable origin identity, preserving one supplied upstream."""
    existing = str(message.get("origin_id") or "").strip()
    if existing:
        return existing
    origin = message.get("origin") if isinstance(message.get("origin"), dict) else {}
    identity = {
        "adapter": origin.get("adapter") or message.get("channel_type") or "mailbox",
        "connector": origin.get("connector_id") or message.get("connector_id") or "",
        "source": origin.get("source_id") or message.get("source_id") or message.get("id") or "",
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "origin:" + hashlib.sha256(encoded).hexdigest()


def endpoint_id(
    adapter: str,
    *,
    connector_id: str = "",
    channel_id: str = "",
    presence_id: str = "",
) -> str:
    return ":".join((adapter.strip().lower(), connector_id.strip(), presence_id.strip(), channel_id.strip()))


class DeliveryLedger:
    def __init__(self, root: Path) -> None:
        self.path = root / "runtime" / "delivery-ledger.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """CREATE TABLE IF NOT EXISTS traversals (
                    origin_id TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY (origin_id, endpoint_id)
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        apply_sqlite_limit(connection, self.path)
        return connection

    def claim(self, message: dict[str, Any], endpoint: str) -> bool:
        """Atomically claim an origin/endpoint pair; false means already traversed."""
        immutable_origin = origin_id(message)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as database:
            cursor = database.execute(
                "INSERT OR IGNORE INTO traversals VALUES (?, ?, ?, ?)",
                (immutable_origin, endpoint, str(message.get("id") or ""), now),
            )
            return cursor.rowcount == 1

    def traversals(self, immutable_origin: str) -> list[str]:
        with self._connect() as database:
            rows = database.execute(
                "SELECT endpoint_id FROM traversals WHERE origin_id = ? ORDER BY endpoint_id",
                (immutable_origin,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def release(self, message: dict[str, Any], endpoint: str) -> None:
        """Release a failed reservation so a later retry can deliver it."""
        with self._connect() as database:
            database.execute(
                "DELETE FROM traversals WHERE origin_id = ? AND endpoint_id = ?",
                (origin_id(message), endpoint),
            )


def with_origin(
    fields: dict[str, Any],
    *,
    adapter: str,
    connector_id: str,
    source_id: str,
    channel_id: str,
    presence_id: str = "",
) -> dict[str, Any]:
    enriched = dict(fields)
    origin = {
        "adapter": adapter,
        "connector_id": connector_id,
        "source_id": source_id,
        "channel_id": channel_id,
        "presence_id": presence_id,
    }
    enriched["origin"] = origin
    enriched["origin_id"] = origin_id({"origin": origin})
    enriched["relay_trace"] = [endpoint_id(
        adapter, connector_id=connector_id, channel_id=channel_id, presence_id=presence_id,
    )]
    return enriched
