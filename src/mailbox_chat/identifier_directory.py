"""Durable readable labels for UUIDs and other platform identifiers."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .sqlite_limits import apply_sqlite_limit


class IdentifierDirectory:
    """Persist opaque identifiers while exposing text usable by simple transports."""

    def __init__(self, mailbox_root: Path) -> None:
        self.path = mailbox_root / "runtime" / "identifier-directory.sqlite3"

    @staticmethod
    def normalize(identifier: str) -> str:
        value = str(identifier).strip()
        if not value:
            raise ValueError("identifier is required")
        if len(value) > 512:
            raise ValueError("identifier must be at most 512 characters")
        try:
            return str(uuid.UUID(value))
        except ValueError:
            return value

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        apply_sqlite_limit(connection, self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS identifier_directory_entries (
                system TEXT NOT NULL,
                identifier TEXT NOT NULL,
                text TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (system, identifier, text, kind)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS identifier_directory_text "
            "ON identifier_directory_entries(system, text, kind)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS identifier_resolution_requests (
                system TEXT NOT NULL,
                identifier TEXT NOT NULL,
                resolver TEXT NOT NULL,
                status TEXT NOT NULL,
                request_count INTEGER NOT NULL,
                first_requested_at TEXT NOT NULL,
                last_requested_at TEXT NOT NULL,
                resolved_at TEXT,
                error TEXT,
                PRIMARY KEY (system, identifier, resolver)
            )
            """
        )
        return connection

    @staticmethod
    def _system(system: str) -> str:
        value = str(system).strip().lower()
        if not value:
            raise ValueError("identifier system is required")
        return value

    def remember(self, identifier: str, text: str, *, system: str, kind: str = "",
                 metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        canonical = self.normalize(identifier)
        source_system = self._system(system)
        readable = str(text).strip()
        if not readable:
            raise ValueError("identifier text is required")
        if len(readable) > 512:
            raise ValueError("identifier text must be at most 512 characters")
        clean_kind = str(kind).strip().lower()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        encoded_metadata = json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO identifier_directory_entries(system, identifier, text, kind, metadata, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(system, identifier, text, kind) DO UPDATE SET
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (source_system, canonical, readable, clean_kind, encoded_metadata, now),
            )
        return {
            "system": source_system,
            "identifier": canonical,
            "text": readable,
            "kind": clean_kind,
            "metadata": metadata or {},
            "updated_at": now,
        }

    def remember_many(self, entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.remember(
            str(entry.get("identifier") or ""),
            str(entry.get("text") or ""),
            system=str(entry.get("system") or ""),
            kind=str(entry.get("kind") or ""),
            metadata=entry.get("metadata") if isinstance(entry.get("metadata"), dict) else None,
        ) for entry in entries]

    def find(self, *, system: str = "", identifier: str = "", text: str = "", kind: str = "",
             limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if system:
            clauses.append("system = ?")
            values.append(self._system(system))
        if identifier:
            clauses.append("identifier = ?")
            values.append(self.normalize(identifier))
        if text:
            clauses.append("text = ? COLLATE NOCASE")
            values.append(str(text).strip())
        if kind:
            clauses.append("kind = ?")
            values.append(str(kind).strip().lower())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        bounded_limit = max(1, min(int(limit), 1000))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT system, identifier, text, kind, metadata, updated_at "
                f"FROM identifier_directory_entries{where} ORDER BY updated_at DESC LIMIT ?",
                (*values, bounded_limit),
            ).fetchall()
        return [{
            "system": row["system"],
            "identifier": row["identifier"],
            "text": row["text"],
            "kind": row["kind"],
            "metadata": json.loads(row["metadata"]),
            "updated_at": row["updated_at"],
        } for row in rows]

    def request_resolution(self, system: str, identifier: str, *, resolver: str,
                           force: bool = False) -> dict[str, Any]:
        source_system = self._system(system)
        canonical = self.normalize(identifier)
        clean_resolver = str(resolver).strip()
        if not clean_resolver:
            raise ValueError("identifier resolver is required")
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM identifier_resolution_requests "
                "WHERE system = ? AND identifier = ? AND resolver = ?",
                (source_system, canonical, clean_resolver),
            ).fetchone()
            should_request = existing is None or force
            if existing is None:
                connection.execute(
                    "INSERT INTO identifier_resolution_requests VALUES (?, ?, ?, 'pending', 1, ?, ?, NULL, NULL)",
                    (source_system, canonical, clean_resolver, now, now),
                )
            elif force:
                connection.execute(
                    "UPDATE identifier_resolution_requests SET status = 'pending', "
                    "request_count = request_count + 1, last_requested_at = ?, resolved_at = NULL, error = NULL "
                    "WHERE system = ? AND identifier = ? AND resolver = ?",
                    (now, source_system, canonical, clean_resolver),
                )
        return {"system": source_system, "identifier": canonical, "resolver": clean_resolver,
                "should_request": should_request,
                "request_count": 1 if existing is None else int(existing["request_count"]) + int(force),
                "status": "pending" if should_request else str(existing["status"])}

    def finish_resolution(self, system: str, identifier: str, *, resolver: str,
                          text: str = "", kind: str = "", error: str = "") -> dict[str, Any]:
        source_system = self._system(system)
        canonical = self.normalize(identifier)
        if text:
            self.remember(canonical, text, system=source_system, kind=kind)
        status = "resolved" if text and not error else "failed"
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            connection.execute(
                "UPDATE identifier_resolution_requests SET status = ?, resolved_at = ?, error = ? "
                "WHERE system = ? AND identifier = ? AND resolver = ?",
                (status, now if status == "resolved" else None, error or None,
                 source_system, canonical, str(resolver).strip()),
            )
        return {"system": source_system, "identifier": canonical, "resolver": resolver,
                "status": status, "text": text, "error": error}

    def resolution_requests(self, *, system: str = "", identifier: str = "") -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[str] = []
        if system:
            clauses.append("system = ?")
            values.append(self._system(system))
        if identifier:
            clauses.append("identifier = ?")
            values.append(self.normalize(identifier))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM identifier_resolution_requests{where} ORDER BY last_requested_at DESC",
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def enrich(self, record: dict[str, Any], *, system: str = "") -> dict[str, Any]:
        enriched = dict(record)
        source_system = system or str(record.get("channel_type") or "mailbox")
        for field, value in record.items():
            if field != "id" and not field.endswith("_id"):
                continue
            try:
                field_system = "mailbox" if field == "id" else source_system
                matches = self.find(system=field_system, identifier=str(value), limit=1)
            except ValueError:
                continue
            if matches:
                enriched.setdefault(f"{field}_text", matches[0]["text"])
        return enriched
