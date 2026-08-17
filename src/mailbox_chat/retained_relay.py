"""Cursor-driven pumps from retained channels to external endpoints."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from threading import Lock
from typing import Any

from . import agent_mailbox
from .endpoint_address import parse_endpoint
from .connector_registry import relays_file


SAFE_ID = re.compile(r"[^a-zA-Z0-9_.-]+")
_RELAY_WRITE_LOCK = Lock()


def load_relays(path: Path | None = None) -> list[dict[str, Any]]:
    """Load cursor relays without conflating them with legacy connector routes."""
    target = path or relays_file()
    if not target.exists():
        return []
    payload = json.loads(target.read_text(encoding="utf-8"))
    records = payload.get("relays") or []
    if not isinstance(records, list):
        raise ValueError(f"{target.name} relays must be an array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(records):
        if not isinstance(raw, dict):
            raise ValueError(f"relays[{index}] must be an object")
        relay_id = str(raw.get("id") or "").strip()
        source_channel = str(raw.get("source_channel") or "").strip()
        cursor = str(raw.get("cursor") or "").strip()
        destination = str(raw.get("destination") or "").strip()
        if not relay_id or relay_id in seen:
            raise ValueError(f"relays[{index}].id must be non-empty and unique")
        if not source_channel or not cursor or not destination:
            raise ValueError(
                f"relays[{index}] requires source_channel, cursor, and destination"
            )
        endpoint = parse_endpoint(destination)
        if endpoint is None or endpoint.adapter == "local":
            raise ValueError(f"relays[{index}].destination must be an external endpoint")
        seen.add(relay_id)
        result.append({
            **raw,
            "id": relay_id,
            "kind": "relay",
            "enabled": bool(raw.get("enabled", True)),
            "source_channel": source_channel,
            "cursor": cursor,
            "destination": endpoint.canonical,
        })
    return result


def _document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "agents": [], "subscriptions": [], "connectors": [],
                "relays": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1:
        raise ValueError(f"{path.name} must contain version 1")
    if not isinstance(payload.setdefault("relays", []), list):
        raise ValueError(f"{path.name} relays must be an array")
    return payload


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def add_relay(
    source_channel: str,
    destination: str,
    *,
    relay_id: str = "",
    start: str = "now",
    path: Path | None = None,
    mailbox_root: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Register a relay and initialize its source cursor exactly once."""
    source_channel = source_channel.strip()
    endpoint = parse_endpoint(destination)
    if not source_channel:
        raise ValueError("source channel is required")
    if source_channel == "outbound_delivery":
        raise ValueError("outbound_delivery cannot pump itself")
    if endpoint is None or endpoint.adapter == "local":
        raise ValueError("destination must be an external TYPE/INSTANCE/IDENTIFIER endpoint")
    clean_id = SAFE_ID.sub("-", relay_id.strip()) if relay_id else SAFE_ID.sub(
        "-", f"relay-{source_channel}-{endpoint.adapter}-{uuid.uuid4().hex[:8]}"
    )
    clean_id = clean_id.strip("-")
    if not clean_id:
        raise ValueError("relay id must be non-empty")
    cursor = f"mailbox-relay:{clean_id}"
    record = {
        "id": clean_id,
        "kind": "relay",
        "enabled": True,
        "source_channel": source_channel,
        "cursor": cursor,
        "destination": endpoint.canonical,
        "created_by": "mailbox-client relay",
    }
    target = path or relays_file()
    with _RELAY_WRITE_LOCK:
        payload = _document(target)
        if any(str(item.get("id") or "") == clean_id for item in payload["relays"]):
            raise ValueError(f"relay id is already registered: {clean_id}")
        if dry_run:
            return {**record, "dry_run": True, "cursor_start": start}
        payload["relays"].append(record)
        _write(target, payload)
        try:
            cursor_state = agent_mailbox.initialize_cursor(
                source_channel, cursor=cursor, start=start, root=mailbox_root,
            )
        except Exception:
            payload["relays"] = [
                item for item in payload["relays"] if str(item.get("id") or "") != clean_id
            ]
            _write(target, payload)
            raise
    return {**record, "subscription": {
        "channel": source_channel, "cursor": cursor, "offset": cursor_state["offset"],
    }}


def delete_relay(relay_id: str, *, path: Path | None = None,
                 dry_run: bool = False) -> dict[str, Any]:
    """Remove relay configuration while deliberately retaining its cursor history."""
    relay_id = relay_id.strip()
    target = path or relays_file()
    with _RELAY_WRITE_LOCK:
        payload = _document(target)
        record = next(
            (item for item in payload["relays"] if str(item.get("id") or "") == relay_id),
            None,
        )
        if record is None:
            return {"id": relay_id, "deleted": False, "dry_run": dry_run}
        if dry_run:
            return {"id": relay_id, "deleted": False, "dry_run": True,
                    "would_delete": record, "cursor_retained": True}
        payload["relays"] = [item for item in payload["relays"] if item is not record]
        _write(target, payload)
    return {"id": relay_id, "deleted": True, "cursor_retained": True}


def pump_relay(relay: dict[str, Any], *, mailbox_root: Path | None = None,
               limit: int | None = None) -> dict[str, Any]:
    """Enqueue unread source records and advance only after each durable enqueue."""
    endpoint = parse_endpoint(str(relay["destination"]))
    if endpoint is None:
        raise ValueError(f"relay {relay['id']} has an invalid destination")
    records = agent_mailbox.peek(
        str(relay["source_channel"]), root=mailbox_root, cursor=str(relay["cursor"]),
    )
    if limit is not None:
        records = records[:max(0, limit)]
    delivered = 0
    error = ""
    for message in records:
        try:
            agent_mailbox.send(
                "outbound_delivery", str(message.get("text") or ""),
                sender="mailbox-server",
                message_type="channel_relay_delivery",
                channel_type=endpoint.adapter,
                channel_id=endpoint.identifier,
                thread_id=str(message.get("thread_id") or "") or None,
                root_id=str(message.get("root_id") or "") or None,
                extra_fields={
                    "endpoint_address": endpoint.canonical,
                    "relay_id": relay["id"],
                    "relay_source_channel": relay["source_channel"],
                    "origin_id": message.get("origin_id") or message.get("id"),
                    "source_message_id": message.get("id"),
                    **({"attachments": message["attachments"]} if message.get("attachments") else {}),
                },
                root=mailbox_root,
            )
            if not agent_mailbox.acknowledge(
                str(relay["source_channel"]), str(message["id"]), root=mailbox_root,
                cursor=str(relay["cursor"]),
            ):
                raise RuntimeError(f"could not advance relay cursor through {message['id']}")
            delivered += 1
        except Exception as caught:
            error = str(caught)
            break
    return {
        "id": relay["id"], "source_channel": relay["source_channel"],
        "destination": endpoint.canonical, "delivered": delivered,
        "pending": max(0, len(records) - delivered), "error": error,
    }


def pump_relays(*, path: Path | None = None, mailbox_root: Path | None = None,
                relay_ids: set[str] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Pump all enabled configured relays once."""
    return [
        pump_relay(relay, mailbox_root=mailbox_root, limit=limit)
        for relay in load_relays(path)
        if relay["enabled"] and (relay_ids is None or relay["id"] in relay_ids)
    ]
