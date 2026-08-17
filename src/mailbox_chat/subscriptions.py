"""Durable subscriptions for fully qualified conversation addresses."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .connector_registry import relays_file


SERVER_EVENTS_CHANNEL = "server_events"
SERVER_AGENT_TO_AGENT_CHANNEL = "agent_to_agent"
SERVER_AGENT_TO_CHANNEL_CHANNEL = "agent_to_channel"
_SUBSCRIPTION_LOCK = threading.Lock()


def canonical_channel(value: str) -> str:
    from .endpoint_address import parse_endpoint

    value = value.strip()
    if "/" not in value:
        if not value:
            raise ValueError("channel must be non-empty")
        return value
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
            **raw, "kind": "channel", "id": channel_id,
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


def available_sources(path: Path | None = None) -> list[dict[str, Any]]:
    """List subscribable channels with stable IDs and explicit kinds."""
    def source_kind(channel_id: str) -> str:
        if channel_id == SERVER_EVENTS_CHANNEL:
            return "system"
        if channel_id in {SERVER_AGENT_TO_AGENT_CHANNEL, SERVER_AGENT_TO_CHANNEL_CHANNEL}:
            return "audit"
        address = channel_id.split("/", 1)[0] if "/" in channel_id else "local"
        return "mattermost" if address == "mm" else address

    result = [
        {
            "id": item["id"],
            "kind": "channel",
            "channel_type": source_kind(item["id"]),
            "subscribers": list(item["subscribers"]),
            "metadata": dict(item.get("metadata") or {}),
        }
        for item in channels(path)
    ]
    seen = {item["id"] for item in result}
    for audit in [
        {"id": SERVER_AGENT_TO_AGENT_CHANNEL, "kind": "channel", "channel_type": "audit", "subscribers": [],
         "metadata": {"scope": "direct_agent"}},
        {"id": SERVER_AGENT_TO_CHANNEL_CHANNEL, "kind": "channel", "channel_type": "audit", "subscribers": [],
         "metadata": {"scope": "all_sends"}},
    ]:
        existing = next((item for item in result if item["id"] == audit["id"]), None)
        if existing is None:
            result.append(audit)
            seen.add(audit["id"])
        else:
            existing["metadata"] = {**existing["metadata"], **audit["metadata"]}
    from .endpoint_address import EndpointAddress, endpoint_instance
    from .connector_registry import load_agents, load_connectors
    for agent in load_agents(path):
        channel = str(agent["mailbox"])
        if channel not in seen:
            result.append({
                "id": channel,
                "kind": "channel",
                "channel_type": "agent_direct",
                "subscribers": [],
                "metadata": {"agent_id": agent["agent_id"]},
            })
            seen.add(channel)
    for connector in load_connectors(path):
        if not connector.get("enabled") or connector.get("direction") not in {"inbound", "bidirectional"}:
            continue
        for identifier in connector.get("channel_ids") or []:
            if not identifier:
                continue
            channel = EndpointAddress(
                str(connector["adapter"]), endpoint_instance(str(connector["adapter"]), connector),
                str(identifier),
            ).canonical
            if channel not in seen:
                result.append({
                    "id": channel,
                    "kind": "channel",
                    "channel_type": ("mattermost" if connector["adapter"] == "mattermost"
                                     else connector["adapter"]),
                    "subscribers": [],
                    "metadata": {"connector": connector["id"]},
                })
                seen.add(channel)
    return result


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
            record = {"kind": "channel", "id": channel_id, "subscribers": []}
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


def delete_channel(channel_id: str, *, force: bool = False,
                   path: Path | None = None) -> dict[str, Any]:
    """Delete a configured channel, protecting subscribers unless forced."""
    channel_id = canonical_channel(channel_id)
    if channel_id in {
        SERVER_EVENTS_CHANNEL, SERVER_AGENT_TO_AGENT_CHANNEL, SERVER_AGENT_TO_CHANNEL_CHANNEL,
    }:
        raise ValueError(f"built-in channel cannot be deleted: {channel_id}")
    target = path or relays_file()
    with _SUBSCRIPTION_LOCK:
        payload = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {"version": 1}
        records = payload.setdefault("subscriptions", [])
        record = next((item for item in records if str(item.get("id") or "") == channel_id), None)
        if record is None:
            return {"id": channel_id, "deleted": False}
        members = list(record.get("subscribers") or [])
        if members and not force:
            raise ValueError("channel has subscribers; unsubscribe them or use --force")
        payload["subscriptions"] = [item for item in records if item is not record]
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
    return {"id": channel_id, "deleted": True, "removed_subscribers": members}


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
            record = {"kind": "channel", "id": channel_id, "subscribers": []}
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
