"""Durable subscriptions for fully qualified conversation addresses."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

from .listener_registry import relays_file


SERVER_EVENTS_CHANNEL = "local/0/server_events"
SERVER_EVENTS_BUS = "mailbox-server-events-bus"
SERVER_AGENT_TO_AGENT_BUS = "mailbox-server-agent-to-agent-bus"
SERVER_AGENT_TO_CHANNEL_BUS = "mailbox-server-agent-to-channel-bus"
SERVER_AUDIT_BUSES = {SERVER_AGENT_TO_AGENT_BUS, SERVER_AGENT_TO_CHANNEL_BUS}
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


def _bus_slug(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(value).lower())).strip("-")


def _mattermost_namespace(instance: str) -> str:
    """Turn a Mattermost endpoint into its short, stable server namespace."""
    host = str(instance).lower().split(":", 1)[0].strip(".")
    return _bus_slug(host) or "mattermost"


def channel_bus(channel_id: str, *, workspace: str = "", channel_name: str = "") -> str:
    """Return the stable retained mailbox bus for one qualified conversation."""
    canonical = canonical_channel(channel_id)
    if canonical == SERVER_EVENTS_CHANNEL:
        return SERVER_EVENTS_BUS
    address = canonical.split("/", 2)
    adapter = "mm" if address[0] == "mm" else address[0].replace("_", "-")
    identifier = _bus_slug(channel_name or address[2]) or "channel"
    if address[0] == "mm":
        namespace = _mattermost_namespace(address[1])
        workspace_slug = _bus_slug(workspace)
        middle = f"{workspace_slug}-{identifier}" if workspace_slug else identifier
        return f"{namespace}-{adapter}-{middle}-bus"
    return f"snet-{adapter}-{identifier}-bus"


def available_buses(path: Path | None = None) -> list[dict[str, Any]]:
    result = [
        {"channel": item["id"],
         "bus": str((item.get("metadata") or {}).get("bus") or channel_bus(item["id"])),
         "metadata": dict(item.get("metadata") or {})}
        for item in channels(path)
    ]
    seen = {item["channel"] for item in result}
    result.extend([
        {"channel": "local/0/agent_to_agent", "bus": SERVER_AGENT_TO_AGENT_BUS,
         "metadata": {"kind": "server_audit", "scope": "direct_agent"}},
        {"channel": "local/0/agent_to_channel", "bus": SERVER_AGENT_TO_CHANNEL_BUS,
         "metadata": {"kind": "server_audit", "scope": "all_sends"}},
    ])
    from .endpoint_address import EndpointAddress, endpoint_instance
    from .listener_registry import agent_presence_bus, load_agents, load_listeners
    result.extend([
        {"channel": f"local/0/presence_to/{item['agent_id']}",
         "bus": agent_presence_bus(str(item["agent_id"])),
         "metadata": {"kind": "agent_presence_ingress", "agent_id": item["agent_id"]}}
        for item in load_agents(path)
    ])
    for listener in load_listeners(path):
        if not listener.get("enabled") or listener.get("direction") not in {"inbound", "bidirectional"}:
            continue
        for identifier in listener.get("channel_ids") or []:
            if not identifier:
                continue
            channel = EndpointAddress(
                str(listener["adapter"]), endpoint_instance(str(listener["adapter"]), listener),
                str(identifier),
            ).canonical
            if channel not in seen:
                result.append({"channel": channel, "bus": channel_bus(
                    channel, workspace=str(listener.get("workspace_name") or ""),
                ),
                               "metadata": {"listener": listener["id"]}})
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
