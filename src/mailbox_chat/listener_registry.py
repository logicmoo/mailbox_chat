"""Load and validate the non-secret channel listener registry."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR_ENV = "MAILBOX_RELAY_CONFIG_DIR"
VALID_DIRECTIONS = {"inbound", "outbound", "bidirectional"}
VALID_PRESENCE_KINDS = {"mailbox", "console", "codex", "platform"}


def config_dir() -> Path:
    configured = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local = Path.cwd() / "config"
    return local.resolve() if local.is_dir() else PROJECT_ROOT / "config"


def relays_file() -> Path:
    """Return the canonical registry, falling back to its legacy filename."""
    canonical = config_dir() / "relays.json"
    legacy = config_dir() / "listeners.json"
    return legacy if not canonical.exists() and legacy.exists() else canonical


def listeners_file() -> Path:
    """Compatibility alias for callers using the former helper name."""
    return relays_file()


def _expand_channel_ids(value: str) -> list[str]:
    if value.startswith("$"):
        value = os.environ.get(value[1:], "")
    return [item.strip() for item in value.split(",") if item.strip()]


def load_agents(path: Path | None = None) -> list[dict[str, Any]]:
    """Load stable agents and their possibly concurrent presences."""
    path = path or listeners_file()
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    agents: list[dict[str, Any]] = []
    agent_ids: set[str] = set()
    presence_ids: set[str] = set()
    for index, raw in enumerate(payload.get("agents") or []):
        if not isinstance(raw, dict):
            raise ValueError(f"agents[{index}] must be an object")
        agent_id = str(raw.get("agent_id") or "").strip()
        if not agent_id or agent_id in agent_ids:
            raise ValueError(f"agents[{index}].agent_id must be non-empty and unique")
        normalized_presences = []
        for presence_index, presence in enumerate(raw.get("presences") or []):
            if not isinstance(presence, dict):
                raise ValueError(f"agents[{index}].presences[{presence_index}] must be an object")
            presence_id = str(presence.get("presence_id") or "").strip()
            kind = str(presence.get("kind") or "platform").strip().lower()
            if not presence_id or presence_id in presence_ids:
                raise ValueError("presence_id values must be non-empty and globally unique")
            if kind not in VALID_PRESENCE_KINDS:
                raise ValueError(f"unsupported presence kind: {kind}")
            presence_ids.add(presence_id)
            normalized_presences.append({**presence, "presence_id": presence_id, "kind": kind})
        agent_ids.add(agent_id)
        agents.append({**raw, "agent_id": agent_id, "presences": normalized_presences})
    return agents


def agent_for_presence(presence_id: str, path: Path | None = None) -> str:
    """Resolve a presence back to the stable mailbox agent that owns it."""
    for agent in load_agents(path):
        if any(item["presence_id"] == presence_id for item in agent["presences"]):
            return str(agent["agent_id"])
    return ""


def load_listeners(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or listeners_file()
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("listeners"), list):
        raise ValueError(f"{path.name} must contain version 1 and a listeners array")
    listeners: list[dict[str, Any]] = []
    agents = {item["agent_id"]: item for item in load_agents(path)}
    presence_owners = {
        presence["presence_id"]: agent_id
        for agent_id, agent in agents.items()
        for presence in agent["presences"]
    }
    seen: set[str] = set()
    for index, raw in enumerate(payload["listeners"]):
        if not isinstance(raw, dict):
            raise ValueError(f"listeners[{index}] must be an object")
        listener_id = str(raw.get("id") or "").strip()
        adapter = str(raw.get("adapter") or "").strip().lower()
        direction = str(raw.get("direction") or "bidirectional").strip().lower()
        agent_id = str(raw.get("agent_id") or "").strip()
        presence_id = str(raw.get("presence_id") or "").strip()
        if not listener_id or listener_id in seen:
            raise ValueError(f"listeners[{index}].id must be non-empty and unique")
        if not adapter:
            raise ValueError(f"listeners[{index}].adapter is required")
        if direction not in VALID_DIRECTIONS:
            raise ValueError(f"listeners[{index}].direction must be inbound, outbound, or bidirectional")
        if agent_id and agent_id not in agents:
            raise ValueError(f"listeners[{index}].agent_id does not name a configured agent")
        if agent_id and presence_id and presence_id not in presence_owners:
            raise ValueError(f"listeners[{index}].presence_id does not name a configured presence")
        if presence_id and agent_id and presence_owners[presence_id] != agent_id:
            raise ValueError(f"listeners[{index}] presence belongs to a different agent")
        if presence_id in presence_owners and not agent_id:
            agent_id = presence_owners[presence_id]
        seen.add(listener_id)
        channel_ids: list[str] = []
        for value in raw.get("channel_ids") or []:
            channel_ids.extend(_expand_channel_ids(str(value)))
        listeners.append({
            **raw,
            "id": listener_id,
            "adapter": adapter,
            "direction": direction,
            "enabled": bool(raw.get("enabled", True)),
            "channel_ids": list(dict.fromkeys(channel_ids)),
            "mailbox_recipients": list(dict.fromkeys([
                *(
                    str(item).strip() for item in raw.get("mailbox_recipients") or []
                    if str(item).strip()
                ),
                *([agent_id] if agent_id else []),
            ])),
            "bridge_agent": str(raw.get("bridge_agent") or "").strip(),
            "agent_id": agent_id,
            "presence_id": presence_id,
        })
    return listeners


def load_routes(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or listeners_file()
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    routes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload.get("routes") or []):
        if not isinstance(raw, dict):
            raise ValueError(f"routes[{index}] must be an object")
        route_id = str(raw.get("id") or "").strip()
        source = raw.get("source") or {}
        destinations = raw.get("destinations") or []
        controller = raw.get("controller") or {}
        controller_type = str(controller.get("type") or "").strip()
        if not route_id or route_id in seen:
            raise ValueError(f"routes[{index}].id must be non-empty and unique")
        if not source.get("listener_id") or not destinations:
            raise ValueError(f"routes[{index}] requires source.listener_id and destinations")
        if controller_type not in {"relay_agent", "presence_controller"}:
            raise ValueError(f"routes[{index}].controller.type must be relay_agent or presence_controller")
        if controller_type == "relay_agent" and not controller.get("mailbox_recipient"):
            raise ValueError(f"routes[{index}] relay_agent requires mailbox_recipient")
        seen.add(route_id)
        source = dict(source)
        if str(source.get("channel_id") or "").startswith("$"):
            source["channel_id"] = os.environ.get(str(source["channel_id"])[1:], "")
        normalized_destinations = []
        for destination in destinations:
            destination = dict(destination)
            if str(destination.get("channel_id") or "").startswith("$"):
                destination["channel_id"] = os.environ.get(str(destination["channel_id"])[1:], "")
            normalized_destinations.append(destination)
        routes.append({**raw, "id": route_id, "enabled": bool(raw.get("enabled", True)),
                       "source": source, "destinations": normalized_destinations,
                       "controller": controller})
    return routes


def listeners_for(adapter: str, *, direction: str | None = None) -> list[dict[str, Any]]:
    matches = [item for item in load_listeners() if item["enabled"] and item["adapter"] == adapter]
    if direction:
        matches = [item for item in matches if item["direction"] in {direction, "bidirectional"}]
    return matches


def public_registry() -> dict[str, Any]:
    """Return listener configuration with no credential values."""
    from .subscriptions import channels

    return {"version": 1, "agents": load_agents(), "listeners": load_listeners(), "routes": load_routes(),
            "subscriptions": channels()}
