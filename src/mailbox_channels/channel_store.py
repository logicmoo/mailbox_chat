"""Primitives for the per-channel JSON document store.

Every durable record lives in ``<root>/<channel_id>.json``. Two shapes share
one container format (``kind: "channel"``):

* message channels: ``{id, kind, sub_kind, count, messages: {entry_N: {...}}}``
* id-keyed registries (``sub_kind: "ordered_id_channel"``):
  ``{id, kind, sub_kind, count, ordinal: {entry_N: id}, entries: {id: {...}}}``

``count`` is a monotonically increasing allocation counter: the next appended
record always takes ``entry_<count>`` even if earlier entries were trimmed.

Durable cursors are stored only in the ``server_registry_agents`` registry as
``entries[<owner>].cursors = {"<channel>": {"next": "entry_N", "consumed": N}}``.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

AGENTS_REGISTRY = "server_registry_agents"
CHANNELS_REGISTRY = "server_registry_channels"
CONNECTORS_REGISTRY = "server_registry_connectors"
IDENTIFIERS_REGISTRY = "server_registry_identifiers"
RELAYS_REGISTRY = "server_registry_relays"

_SAFE_CHANNEL = re.compile(r"[^A-Za-z0-9_.-]")
_ENTRY_KEY = re.compile(r"^entry_(\d+)$")

STORE_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def entry_index(entry_key: str) -> int:
    match = _ENTRY_KEY.match(str(entry_key))
    if not match:
        raise ValueError(f"invalid entry key: {entry_key!r}")
    return int(match.group(1))


def channel_path(root: Path, channel_id: str) -> Path:
    clean = _SAFE_CHANNEL.sub("_", str(channel_id).strip())
    if not clean:
        raise ValueError("channel id is required")
    return root / f"{clean}.json"


def write_document(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8", newline="\n",
    )
    os.replace(temporary, path)


def load_channel(root: Path, channel_id: str, *, sub_kind: str = "") -> dict[str, Any]:
    """Load a channel document, or return an unsaved empty skeleton."""
    path = channel_path(root, channel_id)
    if path.exists():
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"{path.name} must contain a JSON object")
        return document
    skeleton: dict[str, Any] = {
        "id": str(channel_id), "kind": "channel",
        "sub_kind": sub_kind or "channel", "count": 0,
    }
    if (sub_kind or "") == "ordered_id_channel":
        skeleton["ordinal"] = {}
        skeleton["entries"] = {}
    else:
        skeleton["messages"] = {}
    return skeleton


def save_channel(root: Path, document: dict[str, Any]) -> None:
    write_document(channel_path(root, str(document["id"])), document)


def channel_ids(root: Path) -> list[str]:
    """All channel ids present in the store."""
    if not root.is_dir():
        return []
    result = []
    for path in sorted(root.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(document, dict) and document.get("kind") == "channel":
            result.append(str(document.get("id") or path.stem))
    return result


def ordered_messages(document: dict[str, Any],
                     *, start_index: int = 0) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (entry_key, record) in entry-number order from ``start_index``."""
    messages = document.get("messages") or {}
    keyed = []
    for key, record in messages.items():
        try:
            index = entry_index(key)
        except ValueError:
            continue
        if index >= start_index and isinstance(record, dict):
            keyed.append((index, key, record))
    for _index, key, record in sorted(keyed):
        yield key, record


def append_message(root: Path, channel_id: str, record: dict[str, Any],
                   *, sub_kind: str = "") -> dict[str, Any]:
    """Durably append one record to a channel, returning the stored record."""
    with STORE_LOCK:
        document = load_channel(root, channel_id, sub_kind=sub_kind)
        if "messages" not in document:
            raise ValueError(f"channel {channel_id} is not a message channel")
        count = int(document.get("count") or 0)
        key = f"entry_{count}"
        stored = {"entry_number": key, **record}
        document["messages"][key] = stored
        document["count"] = count + 1
        save_channel(root, document)
    return stored


# --- ordered_id_channel registries -----------------------------------------

def load_registry(root: Path, registry_id: str) -> dict[str, Any]:
    return load_channel(root, registry_id, sub_kind="ordered_id_channel")


def registry_entries(root: Path, registry_id: str) -> list[dict[str, Any]]:
    """Entries in ordinal order."""
    document = load_registry(root, registry_id)
    ordinal = document.get("ordinal") or {}
    entries = document.get("entries") or {}
    keyed = []
    for entry_key, entry_id in ordinal.items():
        try:
            index = entry_index(entry_key)
        except ValueError:
            continue
        record = entries.get(str(entry_id))
        if isinstance(record, dict):
            keyed.append((index, record))
    return [record for _index, record in sorted(keyed, key=lambda item: item[0])]


def get_entry(root: Path, registry_id: str, entry_id: str) -> dict[str, Any] | None:
    document = load_registry(root, registry_id)
    record = (document.get("entries") or {}).get(str(entry_id))
    return record if isinstance(record, dict) else None


def put_entry(root: Path, registry_id: str, entry_id: str,
              payload: dict[str, Any] | None = None, *,
              envelope: dict[str, Any] | None = None,
              merge_payload: bool = False) -> dict[str, Any]:
    """Create or update one registry entry, preserving its ordinal position."""
    entry_id = str(entry_id).strip()
    if not entry_id:
        raise ValueError("registry entry id is required")
    with STORE_LOCK:
        document = load_registry(root, registry_id)
        entries = document.setdefault("entries", {})
        ordinal = document.setdefault("ordinal", {})
        record = entries.get(entry_id)
        if not isinstance(record, dict):
            count = int(document.get("count") or 0)
            entry_key = f"entry_{count}"
            record = {
                "id": entry_id,
                "type": f"{registry_id.removeprefix('server_registry_').rstrip('s')}_entry",
                "timestamp": now_iso(),
                "from": "mailbox-server-app",
                "to": registry_id,
                "text": "",
                "channel_id": registry_id,
                "entry_number": entry_key,
            }
            ordinal[entry_key] = entry_id
            document["count"] = count + 1
            entries[entry_id] = record
        if envelope:
            record.update({key: value for key, value in envelope.items()
                           if key not in {"id", "entry_number", "channel_id", "to"}})
        if payload is not None:
            if merge_payload and isinstance(record.get("payload"), dict):
                record["payload"] = {**record["payload"], **payload}
            else:
                record["payload"] = payload
        save_channel(root, document)
        return record


def delete_entry(root: Path, registry_id: str, entry_id: str) -> bool:
    """Remove one registry entry and its ordinal slot (count is not reused)."""
    with STORE_LOCK:
        document = load_registry(root, registry_id)
        entries = document.get("entries") or {}
        if str(entry_id) not in entries:
            return False
        del entries[str(entry_id)]
        document["ordinal"] = {
            key: value for key, value in (document.get("ordinal") or {}).items()
            if str(value) != str(entry_id)
        }
        save_channel(root, document)
    return True


# --- durable cursors (server_registry_agents only) --------------------------

def get_cursor(root: Path, owner: str, channel_id: str) -> dict[str, Any] | None:
    record = get_entry(root, AGENTS_REGISTRY, owner)
    if not record:
        return None
    cursor = (record.get("cursors") or {}).get(str(channel_id))
    return cursor if isinstance(cursor, dict) else None


def set_cursor(root: Path, owner: str, channel_id: str, next_index: int) -> dict[str, Any]:
    """Persist one owner's cursor for a channel; creates the owner if needed."""
    owner = str(owner).strip()
    if not owner:
        raise ValueError("cursor owner is required")
    with STORE_LOCK:
        record = get_entry(root, AGENTS_REGISTRY, owner)
        if record is None:
            record = put_entry(root, AGENTS_REGISTRY, owner,
                               {"kind": "cursor_owner", "agent_id": owner, "presences": []})
        document = load_registry(root, AGENTS_REGISTRY)
        entry = document["entries"][owner]
        cursors = entry.setdefault("cursors", {})
        state = {"next": f"entry_{max(0, int(next_index))}",
                 "consumed": max(0, int(next_index))}
        cursors[str(channel_id)] = state
        save_channel(root, document)
    return state


def delete_cursors(root: Path, *, owners: set[str] | None = None,
                   channels: set[str] | None = None) -> int:
    """Drop cursor state by owner and/or channel; returns removed cursor count."""
    removed = 0
    with STORE_LOCK:
        document = load_registry(root, AGENTS_REGISTRY)
        for entry_id, record in (document.get("entries") or {}).items():
            if not isinstance(record, dict) or "cursors" not in record:
                continue
            if owners is not None and entry_id in owners:
                removed += len(record["cursors"])
                record["cursors"] = {}
                continue
            if channels:
                kept = {key: value for key, value in record["cursors"].items()
                        if key not in channels}
                removed += len(record["cursors"]) - len(kept)
                record["cursors"] = kept
        save_channel(root, document)
    return removed


def cursor_table(root: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Map of owner -> channel -> cursor state."""
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for record in registry_entries(root, AGENTS_REGISTRY):
        cursors = record.get("cursors")
        if isinstance(cursors, dict) and cursors:
            result[str(record["id"])] = {
                str(channel): dict(state) for channel, state in cursors.items()
                if isinstance(state, dict)
            }
    return result
