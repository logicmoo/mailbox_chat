"""Cursor-driven pumps from retained mailboxes to external endpoints.

Relays live in the ``server_registry_relays`` registry. Two payload shapes are
tolerated:

* pump-style: ``{source_mailbox, cursor, destination}`` — actively pumped here.
* config-style: ``{connector, relay_kind, relay_chat: [...]}`` — descriptive
  relay wiring whose cursor is owned by the connector's bridge agent. These are
  returned by :func:`load_relays` but skipped by the pump until the config-style
  pump is implemented.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from threading import Lock
from typing import Any

from . import agent_mailbox
from . import mailbox_store
from .endpoint_address import parse_endpoint
from .connector_registry import store_root


SAFE_ID = re.compile(r"[^a-zA-Z0-9_.-]+")
_RELAY_WRITE_LOCK = Lock()


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def is_pumpable(relay: dict[str, Any]) -> bool:
    """A relay can be pumped only with explicit source/cursor/destination."""
    return bool(relay.get("source_mailbox") and relay.get("cursor")
                and relay.get("destination"))


def load_relays(path: Path | None = None) -> list[dict[str, Any]]:
    """Load relay entries, tolerating both pump-style and config-style payloads."""
    root = store_root(path)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, record in enumerate(
            mailbox_store.registry_entries(root, mailbox_store.RELAYS_REGISTRY)):
        raw = _payload(record)
        relay_id = str(record.get("id") or raw.get("id") or "").strip()
        if not relay_id or relay_id in seen:
            raise ValueError(f"relays[{index}].id must be non-empty and unique")
        seen.add(relay_id)
        source_mailbox = str(raw.get("source_mailbox") or "").strip()
        cursor = str(raw.get("cursor") or "").strip()
        destination = str(raw.get("destination") or "").strip()
        entry = {
            **raw,
            "id": relay_id,
            "kind": "relay",
            "enabled": bool(raw.get("enabled", True)),
            "source_mailbox": source_mailbox,
            "cursor": cursor,
            "destination": destination,
        }
        if source_mailbox and cursor and destination:
            endpoint = parse_endpoint(destination)
            if endpoint is None or endpoint.adapter == "local":
                raise ValueError(f"relays[{index}].destination must be an external endpoint")
            entry["destination"] = endpoint.canonical
            entry["pumpable"] = True
        else:
            entry["pumpable"] = False
        result.append(entry)
    return result


def add_relay(
    source_mailbox: str,
    destination: str,
    *,
    relay_id: str = "",
    start: str = "now",
    path: Path | None = None,
    mailbox_root: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Register a relay and initialize its source cursor exactly once."""
    source_mailbox = source_mailbox.strip()
    endpoint = parse_endpoint(destination)
    if not source_mailbox:
        raise ValueError("source mailbox is required")
    if source_mailbox == "server_outbound_relay_agent_to_mailbox":
        raise ValueError("server_outbound_relay_agent_to_mailbox cannot pump itself")
    if endpoint is None or endpoint.adapter == "local":
        raise ValueError("destination must be an external TYPE/INSTANCE/IDENTIFIER endpoint")
    clean_id = SAFE_ID.sub("-", relay_id.strip()) if relay_id else SAFE_ID.sub(
        "-", f"relay-{source_mailbox}-{endpoint.adapter}-{uuid.uuid4().hex[:8]}"
    )
    clean_id = clean_id.strip("-")
    if not clean_id:
        raise ValueError("relay id must be non-empty")
    cursor = f"mailbox-relay:{clean_id}"
    record = {
        "id": clean_id,
        "kind": "relay",
        "enabled": True,
        "source_mailbox": source_mailbox,
        "cursor": cursor,
        "destination": endpoint.canonical,
        "created_by": "mailbox-client relay",
    }
    root = store_root(path)
    with _RELAY_WRITE_LOCK:
        if mailbox_store.get_entry(root, mailbox_store.RELAYS_REGISTRY, clean_id):
            raise ValueError(f"relay id is already registered: {clean_id}")
        if dry_run:
            return {**record, "dry_run": True, "cursor_start": start}
        mailbox_store.put_entry(root, mailbox_store.RELAYS_REGISTRY, clean_id, record)
        try:
            cursor_state = agent_mailbox.initialize_cursor(
                source_mailbox, cursor=cursor, start=start, root=mailbox_root or root,
            )
        except Exception:
            mailbox_store.delete_entry(root, mailbox_store.RELAYS_REGISTRY, clean_id)
            raise
    return {**record, "subscription": {
        "mailbox": source_mailbox, "cursor": cursor, "offset": cursor_state["offset"],
    }}


def delete_relay(relay_id: str, *, path: Path | None = None,
                 dry_run: bool = False) -> dict[str, Any]:
    """Remove relay configuration while deliberately retaining its cursor history."""
    relay_id = relay_id.strip()
    root = store_root(path)
    with _RELAY_WRITE_LOCK:
        record = mailbox_store.get_entry(root, mailbox_store.RELAYS_REGISTRY, relay_id)
        if record is None:
            return {"id": relay_id, "deleted": False, "dry_run": dry_run}
        if dry_run:
            return {"id": relay_id, "deleted": False, "dry_run": True,
                    "would_delete": _payload(record), "cursor_retained": True}
        mailbox_store.delete_entry(root, mailbox_store.RELAYS_REGISTRY, relay_id)
    return {"id": relay_id, "deleted": True, "cursor_retained": True}


def pump_relay(relay: dict[str, Any], *, mailbox_root: Path | None = None,
               limit: int | None = None) -> dict[str, Any]:
    """Enqueue unread source records and advance only after each durable enqueue."""
    if not is_pumpable(relay):
        return {
            "id": relay["id"], "source_mailbox": relay.get("source_mailbox") or "",
            "destination": relay.get("destination") or "", "delivered": 0,
            "pending": 0, "error": "", "skipped": "config-style relay is not pumpable",
        }
    endpoint = parse_endpoint(str(relay["destination"]))
    if endpoint is None:
        raise ValueError(f"relay {relay['id']} has an invalid destination")
    records = agent_mailbox.peek(
        str(relay["source_mailbox"]), root=mailbox_root, cursor=str(relay["cursor"]),
    )
    if limit is not None:
        records = records[:max(0, limit)]
    delivered = 0
    error = ""
    for message in records:
        try:
            agent_mailbox.send(
                "server_outbound_relay_agent_to_mailbox", str(message.get("text") or ""),
                sender="mailbox-server",
                message_type="mailbox_relay_delivery",
                mailbox_type=endpoint.adapter,
                mailbox_id=endpoint.identifier,
                thread_id=str(message.get("thread_id") or "") or None,
                root_id=str(message.get("root_id") or "") or None,
                extra_fields={
                    "endpoint_address": endpoint.canonical,
                    "relay_id": relay["id"],
                    "relay_source_mailbox": relay["source_mailbox"],
                    "origin_id": message.get("origin_id") or message.get("id"),
                    "source_message_id": message.get("id"),
                    **({"attachments": message["attachments"]} if message.get("attachments") else {}),
                },
                root=mailbox_root,
            )
            if not agent_mailbox.acknowledge(
                str(relay["source_mailbox"]), str(message["id"]), root=mailbox_root,
                cursor=str(relay["cursor"]),
            ):
                raise RuntimeError(f"could not advance relay cursor through {message['id']}")
            delivered += 1
        except Exception as caught:
            error = str(caught)
            break
    return {
        "id": relay["id"], "source_mailbox": relay["source_mailbox"],
        "destination": endpoint.canonical, "delivered": delivered,
        "pending": max(0, len(records) - delivered), "error": error,
    }


def pump_relays(*, path: Path | None = None, mailbox_root: Path | None = None,
                relay_ids: set[str] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Pump all enabled, pumpable configured relays once."""
    return [
        pump_relay(relay, mailbox_root=mailbox_root, limit=limit)
        for relay in load_relays(path)
        if relay["enabled"] and is_pumpable(relay)
        and (relay_ids is None or relay["id"] in relay_ids)
    ]
