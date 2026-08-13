"""Durable subscriptions for fully qualified conversation addresses."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .listener_registry import relays_file


SERVER_EVENTS_CHANNEL = "local/0/server_events"
_SUBSCRIPTION_LOCK = threading.Lock()


def canonical_channel(value: str) -> str:
    from .endpoint_address import parse_endpoint

    address = parse_endpoint(value)
    if address is None:
        raise ValueError("channel must use TYPE/INSTANCE/IDENTIFIER addressing")
    return address.canonical


def channels(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or relays_file()
    payload = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {"version": 1}
    configured = payload.get("subscriptions") or []
    result = []
    seen = set()
    for raw in configured:
        channel_id = canonical_channel(str(raw.get("id") or ""))
        if not channel_id or channel_id in seen:
            raise ValueError("subscription addresses must be non-empty and unique")
        seen.add(channel_id)
        result.append({
            **raw, "id": channel_id,
            "subscribers": list(dict.fromkeys(
                str(item).strip() for item in raw.get("subscribers") or [] if str(item).strip()
            )),
        })
    if SERVER_EVENTS_CHANNEL not in seen:
        result.append({"id": SERVER_EVENTS_CHANNEL, "subscribers": []})
    return result


def subscribers(channel_id: str, path: Path | None = None) -> list[str]:
    channel_id = canonical_channel(channel_id)
    match = next((item for item in channels(path) if item["id"] == channel_id), None)
    return list(match["subscribers"]) if match else []


def subscriptions(identity: str, path: Path | None = None) -> list[str]:
    return [item["id"] for item in channels(path) if identity in item["subscribers"]]


def ensure_channel(channel_id: str, *, path: Path | None = None,
                   metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a qualified subscription address without requiring a subscriber."""
    channel_id = canonical_channel(channel_id)
    target = path or relays_file()
    with _SUBSCRIPTION_LOCK:
        payload = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {"version": 1}
        records = payload.setdefault("subscriptions", [])
        record = next((item for item in records if str(item.get("id") or "") == channel_id), None)
        if record is None:
            record = {"id": channel_id, "subscribers": []}
            records.append(record)
        if metadata:
            record["metadata"] = {**dict(record.get("metadata") or {}), **metadata}
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent, text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return record


def set_subscription(channel_id: str, identity: str, *, enabled: bool,
                     path: Path | None = None) -> dict[str, Any]:
    channel_id, identity = canonical_channel(channel_id), identity.strip()
    if not channel_id or not identity:
        raise ValueError("channel and identity are required")
    target = path or relays_file()
    with _SUBSCRIPTION_LOCK:
        payload = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {"version": 1}
        records = payload.setdefault("subscriptions", [])
        record = next((item for item in records if str(item.get("id") or "") == channel_id), None)
        if record is None:
            record = {"id": channel_id, "subscribers": []}
            records.append(record)
        members = list(dict.fromkeys(
            str(item).strip() for item in record.get("subscribers") or [] if str(item).strip()
        ))
        if enabled and identity not in members:
            members.append(identity)
        if not enabled:
            members = [item for item in members if item != identity]
        record["subscribers"] = members
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent, text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {"channel": channel_id, "identity": identity, "subscribed": enabled}
