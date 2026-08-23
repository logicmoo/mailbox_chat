"""Durable subscriptions for fully qualified conversation addresses.

Channels live in the ``server_registry_channels`` registry. Each entry's
payload holds ``{subscribers, aliases, kind, ...}`` plus any connector/endpoint
metadata; the registry id is the canonical channel id.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import channel_store
from .connector_registry import store_root


SERVER_EVENTS_CHANNEL = "server_events_log"
SERVER_AGENT_TO_AGENT_CHANNEL = "server_outbound_relay_agent_to_agent"
SERVER_AGENT_TO_CHANNEL_CHANNEL = "server_outbound_relay_agent_to_channel"
BUILTIN_CHANNELS = {
    SERVER_EVENTS_CHANNEL, SERVER_AGENT_TO_AGENT_CHANNEL, SERVER_AGENT_TO_CHANNEL_CHANNEL,
}


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def canonical_channel(value: str, path: Path | None = None) -> str:
    from .endpoint_address import channel_resource_id, parse_endpoint

    value = value.strip()
    if "/" not in value:
        if not value:
            raise ValueError("channel must be non-empty")
        return value
    address = parse_endpoint(value)
    if address is None:
        raise ValueError("channel must use TYPE/INSTANCE/IDENTIFIER addressing")
    root = store_root(path)
    equivalent_ids: list[str] = []
    for record in channel_store.registry_entries(root, channel_store.CHANNELS_REGISTRY):
        payload = _payload(record)
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        aliases = [
            *(payload.get("aliases") or []),
            payload.get("external_address"),
            metadata.get("external_address"),
        ]
        configured_id = str(record.get("id") or "").strip()
        if value in aliases and configured_id:
            return configured_id
        for alias in aliases:
            try:
                alias_address = parse_endpoint(str(alias or ""))
            except ValueError:
                alias_address = None
            if (alias_address and alias_address.adapter == address.adapter
                    and alias_address.identifier == address.identifier
                    and (alias_address.instance == address.instance
                         or address.instance.isdigit())):
                if configured_id:
                    equivalent_ids.append(configured_id)
    equivalent_ids = list(dict.fromkeys(equivalent_ids))
    if len(equivalent_ids) == 1:
        return equivalent_ids[0]
    if len(equivalent_ids) > 1:
        raise ValueError(f"channel alias is ambiguous: {value}")
    return channel_resource_id(address.adapter, address.instance, address.identifier)


def _channel_record(payload: dict[str, Any], channel_id: str) -> dict[str, Any]:
    metadata = dict(payload.get("metadata") or {})
    aliases = list(dict.fromkeys(
        str(value).strip() for value in [
            *(payload.get("aliases") or []),
            payload.get("external_address"), payload.get("channel_name"),
            metadata.get("external_address"), metadata.get("channel_name"),
        ] if str(value or "").strip()
    ))
    return {
        **payload, "kind": "channel", "id": channel_id,
        "aliases": aliases,
        "subscribers": list(dict.fromkeys(
            str(item).strip() for item in payload.get("subscribers") or [] if str(item).strip()
        )),
    }


def channels(path: Path | None = None) -> list[dict[str, Any]]:
    root = store_root(path)
    result = []
    seen = set()
    for record in channel_store.registry_entries(root, channel_store.CHANNELS_REGISTRY):
        channel_id = canonical_channel(str(record.get("id") or ""), root)
        if not channel_id or channel_id in seen:
            raise ValueError("subscription addresses must be non-empty and unique")
        seen.add(channel_id)
        result.append(_channel_record(_payload(record), channel_id))
    if SERVER_EVENTS_CHANNEL not in seen:
        result.append({"id": SERVER_EVENTS_CHANNEL, "subscribers": []})
    return result


def subscribers(channel_id: str, path: Path | None = None) -> list[str]:
    root = store_root(path)
    channel_id = canonical_channel(channel_id, root)
    match = next((item for item in channels(root) if item["id"] == channel_id), None)
    return list(match["subscribers"]) if match else []


def subscriptions(identity: str, path: Path | None = None) -> list[str]:
    return [item["id"] for item in channels(path) if identity in item["subscribers"]]


def available_sources(path: Path | None = None) -> list[dict[str, Any]]:
    """List subscribable channels with stable IDs and explicit kinds."""
    from .connector_registry import load_agents, load_connectors

    root = store_root(path)
    configured_connectors = load_connectors(root)
    connector_adapters = {item["id"]: item["adapter"] for item in configured_connectors}

    def source_kind(item: dict[str, Any]) -> str:
        channel_id = item["id"]
        if channel_id == SERVER_EVENTS_CHANNEL:
            return "system"
        if channel_id in {SERVER_AGENT_TO_AGENT_CHANNEL, SERVER_AGENT_TO_CHANNEL_CHANNEL}:
            return "audit"
        connector_id = str(item.get("connector")
                           or (item.get("metadata") or {}).get("connector") or "")
        return str(connector_adapters.get(connector_id) or "local")

    result = [
        {
            "id": item["id"],
            "kind": "mailbox",
            "channel_type": source_kind(item),
            "aliases": list(item.get("aliases") or []),
            "subscribers": list(item["subscribers"]),
            "metadata": dict(item.get("metadata") or {}),
        }
        for item in channels(root)
    ]
    seen = {item["id"] for item in result}
    for audit in [
        {"id": SERVER_AGENT_TO_AGENT_CHANNEL, "kind": "mailbox", "channel_type": "audit",
         "aliases": [], "subscribers": [],
         "metadata": {"scope": "direct_agent"}},
        {"id": SERVER_AGENT_TO_CHANNEL_CHANNEL, "kind": "mailbox", "channel_type": "audit",
         "aliases": [], "subscribers": [],
         "metadata": {"scope": "all_sends"}},
    ]:
        existing = next((item for item in result if item["id"] == audit["id"]), None)
        if existing is None:
            result.append(audit)
            seen.add(audit["id"])
        else:
            existing["metadata"] = {**existing["metadata"], **audit["metadata"]}
    for agent in load_agents(root):
        channel = str(agent["mailbox"])
        if channel not in seen:
            result.append({
                "id": channel,
                "kind": "mailbox",
                "channel_type": "agent_direct",
                "aliases": [],
                "subscribers": [],
                "metadata": {"agent_id": agent["agent_id"]},
            })
            seen.add(channel)
    for connector in configured_connectors:
        if not connector.get("enabled") or connector.get("direction") not in {"inbound", "bidirectional"}:
            continue
        for identifier in connector.get("channel_ids") or []:
            if not identifier:
                continue
            from .endpoint_address import channel_resource_id, endpoint_instance
            channel = channel_resource_id(
                str(connector["adapter"]),
                endpoint_instance(str(connector["adapter"]), connector),
                str(identifier),
            )
            if channel not in seen:
                result.append({
                    "id": channel,
                    "kind": "mailbox",
                    "channel_type": ("mattermost" if connector["adapter"] == "mattermost"
                                     else connector["adapter"]),
                    "aliases": [str(identifier)],
                    "subscribers": [],
                    "metadata": {"connector": connector["id"]},
                })
                seen.add(channel)
    return result


def ensure_channel(channel_id: str, *, path: Path | None = None,
                   metadata: dict[str, Any] | None = None,
                   aliases: list[str] | None = None) -> dict[str, Any]:
    """Create a qualified subscription address without requiring a subscriber."""
    root = store_root(path)
    channel_id = canonical_channel(channel_id, root)
    with channel_store.STORE_LOCK:
        record = channel_store.get_entry(root, channel_store.CHANNELS_REGISTRY, channel_id)
        payload = _payload(record) if record else {"kind": "channel", "subscribers": []}
        if metadata:
            payload.update(metadata)
            payload["aliases"] = list(dict.fromkeys(
                str(value).strip() for value in [
                    *(payload.get("aliases") or []),
                    metadata.get("external_address"), metadata.get("channel_name"),
                ] if str(value or "").strip()
            ))
        if aliases:
            payload["aliases"] = list(dict.fromkeys([
                *(payload.get("aliases") or []),
                *(str(value).strip() for value in aliases if str(value).strip()),
            ]))
        payload.setdefault("kind", "channel")
        payload.setdefault("subscribers", [])
        channel_store.put_entry(root, channel_store.CHANNELS_REGISTRY, channel_id, payload)
    return {"kind": "channel", "id": channel_id, **payload}


def delete_channel(channel_id: str, *, force: bool = False,
                   path: Path | None = None) -> dict[str, Any]:
    """Delete a configured channel, protecting subscribers unless forced."""
    root = store_root(path)
    channel_id = canonical_channel(channel_id, root)
    if channel_id in BUILTIN_CHANNELS:
        raise ValueError(f"built-in channel cannot be deleted: {channel_id}")
    with channel_store.STORE_LOCK:
        record = channel_store.get_entry(root, channel_store.CHANNELS_REGISTRY, channel_id)
        if record is None:
            return {"id": channel_id, "deleted": False}
        members = list(_payload(record).get("subscribers") or [])
        if members and not force:
            raise ValueError("channel has subscribers; unsubscribe them or use --force")
        channel_store.delete_entry(root, channel_store.CHANNELS_REGISTRY, channel_id)
    return {"id": channel_id, "deleted": True, "removed_subscribers": members}


def set_subscription(channel_id: str, identity: str, *, enabled: bool,
                     path: Path | None = None) -> dict[str, Any]:
    root = store_root(path)
    channel_id, identity = canonical_channel(channel_id, root), identity.strip()
    if not channel_id or not identity:
        raise ValueError("channel and identity are required")
    with channel_store.STORE_LOCK:
        record = channel_store.get_entry(root, channel_store.CHANNELS_REGISTRY, channel_id)
        payload = _payload(record) if record else {"kind": "channel", "subscribers": []}
        members = list(dict.fromkeys(
            str(item).strip() for item in payload.get("subscribers") or [] if str(item).strip()
        ))
        if enabled and identity not in members:
            members.append(identity)
        if not enabled:
            members = [item for item in members if item != identity]
        payload["subscribers"] = members
        payload.setdefault("kind", "channel")
        channel_store.put_entry(root, channel_store.CHANNELS_REGISTRY, channel_id, payload)
    return {"channel": channel_id, "identity": identity, "subscribed": enabled}
