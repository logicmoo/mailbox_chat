"""Durable readable labels for UUIDs and other platform identifiers."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Iterable

from .sqlite_limits import apply_sqlite_limit
from .endpoint_address import ADAPTER_ALIASES


_SAFE_FILE = re.compile(r"[^A-Za-z0-9_.-]")


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
        # A former generic JSON walker paired creator_id/owner_id with the
        # containing mailbox's display_name. Remove only those demonstrably
        # mismatched relationship aliases; genuine user records have id equal
        # to the registered identifier.
        stale_relationship_rows = connection.execute(
            "SELECT rowid, identifier, text, metadata FROM identifier_directory_entries "
            "WHERE kind = 'user'"
        ).fetchall()
        for row in stale_relationship_rows:
            try:
                metadata = json.loads(row["metadata"])
            except (TypeError, json.JSONDecodeError):
                continue
            relationship_match = any(
                str(metadata.get(field) or "") == row["identifier"]
                for field in ("creator_id", "owner_id")
            )
            containing_name = row["text"] in {
                str(metadata.get("display_name") or ""),
                str(metadata.get("name") or ""),
            }
            if (relationship_match and containing_name and
                    str(metadata.get("id") or "") != row["identifier"]):
                connection.execute(
                    "DELETE FROM identifier_directory_entries WHERE rowid = ?", (row["rowid"],)
                )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS identifier_directory_text "
            "ON identifier_directory_entries(system, text, kind)"
        )
        # Older releases stored each Mattermost discovery both under its real
        # instance and under mm/0. Keep legacy-only records, but discard an
        # alias row when the identical canonical record is present.
        connection.execute(
            """
            DELETE FROM identifier_directory_entries AS alias
            WHERE alias.system = 'mm/0'
              AND EXISTS (
                SELECT 1 FROM identifier_directory_entries AS canonical
                WHERE canonical.system LIKE 'mm/%'
                  AND canonical.system != 'mm/0'
                  AND canonical.identifier = alias.identifier
                  AND canonical.text = alias.text
                  AND canonical.kind = alias.kind
              )
            """
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

    @staticmethod
    def _resolver_key(system: str) -> str:
        source = str(system).strip().lower()
        if "/" not in source:
            return source
        adapter, instance = source.split("/", 1)
        canonical = ADAPTER_ALIASES.get(adapter, adapter)
        return f"{canonical}-{instance}"

    @staticmethod
    def _safe_name(value: str) -> str:
        clean = _SAFE_FILE.sub("_", str(value).strip())
        return clean or "resolver"

    def _resolver_path(self, system: str) -> Path:
        mailbox_root = self.path.parent.parent
        key = self._resolver_key(system)
        return mailbox_root / "resolver" / f"resolver_{self._safe_name(key)}.json"

    def _remember_resolver_entry(
        self,
        *,
        system: str,
        identifier: str,
        text: str,
        kind: str,
        metadata: dict[str, Any],
        updated_at: str,
    ) -> None:
        target = self._resolver_path(system)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            document = json.loads(target.read_text(encoding="utf-8"))
        else:
            document = {
                "id": self._resolver_key(system),
                "kind": "resolver_cache",
                "entries": [],
            }
        if not isinstance(document, dict):
            raise ValueError(f"resolver file must contain a JSON object: {target}")
        entries = document.get("entries")
        if not isinstance(entries, list):
            raise ValueError(f"resolver entries must be a list: {target}")
        record = {
            "system": system,
            "identifier": identifier,
            "text": text,
            "kind": kind,
            "metadata": metadata,
            "updated_at": updated_at,
        }
        replaced = False
        for index, item in enumerate(entries):
            if not isinstance(item, dict):
                continue
            if (str(item.get("system") or "") == system
                    and str(item.get("identifier") or "") == identifier
                    and str(item.get("kind") or "") == kind):
                entries[index] = record
                replaced = True
                break
        if not replaced:
            entries.append(record)
        document["id"] = str(document.get("id") or self._resolver_key(system))
        document["kind"] = str(document.get("kind") or "resolver_cache")
        document["entries"] = entries
        temporary = target.with_name(
            f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            replaced = False
            last_error: OSError | None = None
            for _ in range(8):
                try:
                    temporary.replace(target)
                    replaced = True
                    break
                except OSError as error:
                    last_error = error
                    if getattr(error, "winerror", None) not in {5, 32}:
                        raise
                    time.sleep(0.05)
            if not replaced:
                raise last_error or OSError(f"failed to replace resolver cache {target}")
        finally:
            if temporary.exists():
                temporary.unlink()

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
        self._remember_resolver_entry(
            system=source_system,
            identifier=canonical,
            text=readable,
            kind=clean_kind,
            metadata=metadata or {},
            updated_at=now,
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

    def remember_alias(self, identifier: str, text: str, *, system: str,
                       kind: str = "", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Save a manually chosen alias only when it cannot name another ID."""
        canonical = self.normalize(identifier)
        source_system = self._system(system)
        readable = str(text).strip()
        collisions = {
            entry["identifier"] for entry in self.find(
                system=source_system, text=readable, limit=1000,
            )
            if entry["identifier"] != canonical
        }
        if collisions:
            raise ValueError(
                f"alias {readable!r} already names another identifier in {source_system}: "
                f"{', '.join(sorted(collisions))}"
            )
        return self.remember(
            canonical, readable, system=source_system, kind=kind,
            metadata={**(metadata or {}), "manual_alias": True},
        )

    def entry_count(self, *, system: str = "") -> int:
        """Count durable identifier/name/kind aliases, optionally for one system."""
        if system:
            query = "SELECT COUNT(*) FROM identifier_directory_entries WHERE system = ?"
            values: tuple[str, ...] = (self._system(system),)
        else:
            query = "SELECT COUNT(*) FROM identifier_directory_entries"
            values = ()
        with self._connect() as connection:
            return int(connection.execute(query, values).fetchone()[0])

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
        source_system = system or str(record.get("mailbox_type") or "mailbox")
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
