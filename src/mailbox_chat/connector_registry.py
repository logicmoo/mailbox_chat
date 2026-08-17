"""Load and validate the non-secret channel connector registry."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from threading import Lock
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR_ENV = "MAILBOX_RELAY_CONFIG_DIR"
VALID_DIRECTIONS = {"inbound", "outbound", "bidirectional"}
_REGISTRY_WRITE_LOCK = Lock()


def agent_mailbox_address(agent_id: str) -> str:
    """Return the stable writable mailbox address for an agent."""
    identifier = agent_id.strip()
    if not identifier:
        raise ValueError("agent_id is required")
    return identifier


def config_dir() -> Path:
    configured = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local = Path.cwd() / "config"
    return local.resolve() if local.is_dir() else PROJECT_ROOT / "config"


def relays_file() -> Path:
    """Return the canonical registry, falling back to its legacy filename."""
    canonical = config_dir() / "relays.json"
    legacy = config_dir() / "connectors.json"
    return legacy if not canonical.exists() and legacy.exists() else canonical


def connectors_file() -> Path:
    """Compatibility alias for callers using the former helper name."""
    return relays_file()


def _expand_channel_ids(value: str) -> list[str]:
    if value.startswith("$"):
        value = os.environ.get(value[1:], "")
    return [item.strip() for item in value.split(",") if item.strip()]


def load_agents(path: Path | None = None) -> list[dict[str, Any]]:
    """Load stable agents and their possibly concurrent presences."""
    path = path or connectors_file()
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
            if not presence_id or presence_id in presence_ids:
                raise ValueError("presence_id values must be non-empty and globally unique")
            presence_ids.add(presence_id)
            normalized_presences.append({
                key: value for key, value in {**presence, "presence_id": presence_id}.items()
                if key != "kind"
            })
        agent_ids.add(agent_id)
        agents.append({
            **raw,
            "kind": "agent",
            "agent_id": agent_id,
            "mailbox": agent_mailbox_address(agent_id),
            "presences": normalized_presences,
        })
    return agents


def agent_for_presence(presence_id: str, path: Path | None = None) -> str:
    """Resolve a presence back to the stable mailbox agent that owns it."""
    for agent in load_agents(path):
        if any(item["presence_id"] == presence_id for item in agent["presences"]):
            return str(agent["agent_id"])
    return ""


def register_agent(
    agent_id: str,
    *,
    presence_id: str = "",
    dry_run: bool = False,
    path: Path | None = None,
    mailbox_root: Path | None = None,
) -> dict[str, Any]:
    """Create an agent and/or one of its presences, announcing each new record."""
    agent_id = agent_id.strip()
    presence_id = presence_id.strip()
    if not agent_id:
        raise ValueError("agent_id is required")

    target = path or connectors_file()
    with _REGISTRY_WRITE_LOCK:
        if target.exists():
            payload = json.loads(target.read_text(encoding="utf-8"))
        else:
            payload = {"version": 1, "agents": [], "connectors": []}
        if payload.get("version") != 1:
            raise ValueError(f"{target.name} must contain version 1")
        raw_agents = payload.setdefault("agents", [])
        if not isinstance(raw_agents, list):
            raise ValueError(f"{target.name} agents must be an array")

        agents = load_agents(target) if target.exists() else []
        presence_owner = {
            presence["presence_id"]: agent["agent_id"]
            for agent in agents
            for presence in agent["presences"]
        }
        if presence_id and presence_id in presence_owner and presence_owner[presence_id] != agent_id:
            raise ValueError(f"presence {presence_id} is already registered to {presence_owner[presence_id]}")

        raw_agent = next(
            (item for item in raw_agents if isinstance(item, dict)
             and str(item.get("agent_id") or "").strip() == agent_id),
            None,
        )
        agent_created = raw_agent is None
        if raw_agent is None:
            raw_agent = {"kind": "agent", "agent_id": agent_id, "presences": []}
            raw_agents.append(raw_agent)
        raw_presences = raw_agent.setdefault("presences", [])
        if not isinstance(raw_presences, list):
            raise ValueError(f"agent {agent_id} presences must be an array")

        existing_presence = next(
            (item for item in raw_presences if isinstance(item, dict)
             and str(item.get("presence_id") or "").strip() == presence_id),
            None,
        ) if presence_id else None
        presence_created = bool(presence_id and existing_presence is None)
        if presence_created:
            raw_presences.append({"presence_id": presence_id})

        if dry_run:
            return {
                "dry_run": True,
                "agent_id": agent_id,
                "presence_id": presence_id,
                "would_create_agent": agent_created,
                "would_create_presence": presence_created,
                "would_add_mailboxes": [agent_mailbox_address(agent_id)] if agent_created else [],
                "initial_mailboxes": [agent_mailbox_address(agent_id)],
            }

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
        temporary.replace(target)

    if agent_created or presence_created:
        from . import agent_mailbox
        from .subscriptions import SERVER_EVENTS_CHANNEL

        event_channel = SERVER_EVENTS_CHANNEL
        if agent_created:
            agent_mailbox.send(
                event_channel,
                f"Agent registered: {agent_id}",
                sender="mailbox-server",
                message_type="agent_registered",
                extra_fields={"agent_id": agent_id},
                root=mailbox_root,
            )
        if presence_created:
            agent_mailbox.send(
                event_channel,
                f"Presence registered: {presence_id} for {agent_id}",
                sender="mailbox-server",
                message_type="presence_registered",
                extra_fields={
                    "agent_id": agent_id,
                    "presence_id": presence_id,
                },
                root=mailbox_root,
            )

    return {
        "agent": next(item for item in load_agents(target) if item["agent_id"] == agent_id),
        "agent_created": agent_created,
        "presence_created": presence_created,
        "initial_mailboxes": [agent_mailbox_address(agent_id)],
    }


def unregister_agent(
    agent_id: str,
    *,
    presence_id: str = "",
    purge: bool = False,
    dry_run: bool = False,
    path: Path | None = None,
    mailbox_root: Path | None = None,
) -> dict[str, Any]:
    """Remove an agent or presence unless a configured connector still references it."""
    agent_id = agent_id.strip()
    presence_id = presence_id.strip()
    if not agent_id:
        raise ValueError("agent_id is required")

    target = path or connectors_file()
    with _REGISTRY_WRITE_LOCK:
        if not target.exists():
            return {"agent_id": agent_id, "presence_id": presence_id,
                    "agent_removed": False, "presence_removed": False}
        payload = json.loads(target.read_text(encoding="utf-8"))
        if payload.get("version") != 1:
            raise ValueError(f"{target.name} must contain version 1")
        raw_agents = payload.get("agents") or []
        raw_agent = next(
            (item for item in raw_agents if isinstance(item, dict)
             and str(item.get("agent_id") or "").strip() == agent_id),
            None,
        )
        if raw_agent is None:
            return {"agent_id": agent_id, "presence_id": presence_id,
                    "agent_removed": False, "presence_removed": False}

        owned_presences = {
            str(item.get("presence_id") or "").strip()
            for item in raw_agent.get("presences") or [] if isinstance(item, dict)
        }
        connectors = [item for item in payload.get("connectors") or [] if isinstance(item, dict)]
        if presence_id:
            if presence_id not in owned_presences:
                return {"agent_id": agent_id, "presence_id": presence_id,
                        "agent_removed": False, "presence_removed": False}
            referencing = [
                str(item.get("id") or "") for item in connectors
                if str(item.get("presence_id") or "").strip() == presence_id
            ]
            if referencing:
                raise ValueError(
                    f"presence {presence_id} is referenced by connector(s): {', '.join(referencing)}"
                )
            if dry_run:
                from . import agent_mailbox
                preview = agent_mailbox.purge_agent_data(
                    agent_id, presence_ids={presence_id},
                    agent_mailbox=agent_mailbox_address(agent_id), remove_agent=False,
                    dry_run=True, root=mailbox_root,
                )
                return {
                    "agent_id": agent_id, "presence_id": presence_id,
                    "would_remove_agent": False, "would_remove_presence": True,
                    **preview,
                }
            raw_agent["presences"] = [
                item for item in raw_agent.get("presences") or []
                if not (isinstance(item, dict)
                        and str(item.get("presence_id") or "").strip() == presence_id)
            ]
            agent_removed, presence_removed = False, True
        else:
            referencing = [
                str(item.get("id") or "") for item in connectors
                if (str(item.get("agent_id") or "").strip() == agent_id
                    or str(item.get("presence_id") or "").strip() in owned_presences)
            ]
            if referencing:
                raise ValueError(
                    f"agent {agent_id} is referenced by connector(s): {', '.join(referencing)}"
                )
            if dry_run:
                from . import agent_mailbox
                preview = agent_mailbox.purge_agent_data(
                    agent_id, presence_ids=owned_presences,
                    agent_mailbox=agent_mailbox_address(agent_id), remove_agent=True,
                    dry_run=True, root=mailbox_root,
                )
                return {
                    "agent_id": agent_id, "presence_id": "",
                    "would_remove_agent": True, "would_remove_presence": False,
                    **preview,
                }
            payload["agents"] = [item for item in raw_agents if item is not raw_agent]
            agent_removed, presence_removed = True, False

        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
        temporary.replace(target)

    from . import agent_mailbox
    from .subscriptions import SERVER_EVENTS_CHANNEL

    event_type = "presence_unregistered" if presence_removed else "agent_unregistered"
    event_text = (f"Presence unregistered: {presence_id} for {agent_id}" if presence_removed
                  else f"Agent unregistered: {agent_id}")
    event_fields = {"agent_id": agent_id}
    if presence_removed:
        event_fields["presence_id"] = presence_id
    agent_mailbox.send(
        SERVER_EVENTS_CHANNEL, event_text, sender="mailbox-server",
        message_type=event_type, extra_fields=event_fields, root=mailbox_root,
    )
    purge_result: dict[str, Any] = {"purged": False}
    if purge:
        purge_result = agent_mailbox.purge_agent_data(
            agent_id,
            presence_ids={presence_id} if presence_removed else owned_presences,
            agent_mailbox=agent_mailbox_address(agent_id),
            remove_agent=agent_removed,
            dry_run=False,
            root=mailbox_root,
        )
    remaining = next((item for item in load_agents(target) if item["agent_id"] == agent_id), None)
    return {
        "agent_id": agent_id,
        "presence_id": presence_id,
        "agent": remaining,
        "agent_removed": agent_removed,
        "presence_removed": presence_removed,
        **purge_result,
    }


def load_connectors(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or connectors_file()
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("connectors"), list):
        raise ValueError(f"{path.name} must contain version 1 and a connectors array")
    connectors: list[dict[str, Any]] = []
    agents = {item["agent_id"]: item for item in load_agents(path)}
    presence_owners = {
        presence["presence_id"]: agent_id
        for agent_id, agent in agents.items()
        for presence in agent["presences"]
    }
    seen: set[str] = set()
    for index, raw in enumerate(payload["connectors"]):
        if not isinstance(raw, dict):
            raise ValueError(f"connectors[{index}] must be an object")
        connector_id = str(raw.get("id") or "").strip()
        adapter = str(raw.get("adapter") or "").strip().lower()
        direction = str(raw.get("direction") or "bidirectional").strip().lower()
        agent_id = str(raw.get("agent_id") or "").strip()
        presence_id = str(raw.get("presence_id") or "").strip()
        if not connector_id or connector_id in seen:
            raise ValueError(f"connectors[{index}].id must be non-empty and unique")
        if not adapter:
            raise ValueError(f"connectors[{index}].adapter is required")
        if direction not in VALID_DIRECTIONS:
            raise ValueError(f"connectors[{index}].direction must be inbound, outbound, or bidirectional")
        if agent_id and agent_id not in agents:
            raise ValueError(f"connectors[{index}].agent_id does not name a configured agent")
        if agent_id and presence_id and presence_id not in presence_owners:
            raise ValueError(f"connectors[{index}].presence_id does not name a configured presence")
        if presence_id and agent_id and presence_owners[presence_id] != agent_id:
            raise ValueError(f"connectors[{index}] presence belongs to a different agent")
        if presence_id in presence_owners and not agent_id:
            agent_id = presence_owners[presence_id]
        seen.add(connector_id)
        channel_ids: list[str] = []
        for value in raw.get("channel_ids") or []:
            channel_ids.extend(_expand_channel_ids(str(value)))
        connectors.append({
            **raw,
            "kind": "connector",
            "id": connector_id,
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
    return connectors


def connectors_for(adapter: str, *, direction: str | None = None) -> list[dict[str, Any]]:
    matches = [item for item in load_connectors() if item["enabled"] and item["adapter"] == adapter]
    if direction:
        matches = [item for item in matches if item["direction"] in {direction, "bidirectional"}]
    return matches


def _write_registry(target: Path, payload: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8", newline="\n")
    os.replace(temporary, target)


def register_connector(
    connector_id: str,
    kind: str,
    *,
    direction: str = "bidirectional",
    instance: str = "",
    channel_ids: list[str] | None = None,
    config: dict[str, Any] | None = None,
    dry_run: bool = False,
    path: Path | None = None,
) -> dict[str, Any]:
    """Register one external-system connector in the canonical registry."""
    connector_id, kind, direction = connector_id.strip(), kind.strip(), direction.strip()
    if not connector_id or not kind:
        raise ValueError("connector id and kind are required")
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of: {', '.join(sorted(VALID_DIRECTIONS))}")
    target = path or relays_file()
    with _REGISTRY_WRITE_LOCK:
        payload = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {"version": 1}
        records = payload.setdefault("connectors", [])
        if any(str(item.get("id") or "").strip() == connector_id for item in records):
            raise ValueError(f"connector id is already registered: {connector_id}")
        record = {
            **dict(config or {}), "kind": "connector", "id": connector_id, "adapter": kind,
            "direction": direction, "enabled": True,
        }
        if instance:
            record["instance"] = instance
        if channel_ids:
            record["channel_ids"] = list(dict.fromkeys(channel_ids))
        if dry_run:
            return {**record, "dry_run": True}
        records.append(record)
        _write_registry(target, payload)
        load_connectors(target)
    return record


def unregister_connector(connector_id: str, *, dry_run: bool = False,
                         path: Path | None = None) -> dict[str, Any]:
    """Remove one connector."""
    connector_id = connector_id.strip()
    target = path or relays_file()
    with _REGISTRY_WRITE_LOCK:
        payload = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {"version": 1}
        records = payload.setdefault("connectors", [])
        record = next((item for item in records if str(item.get("id") or "").strip() == connector_id), None)
        if record is None:
            return {"id": connector_id, "deleted": False, "dry_run": dry_run}
        if dry_run:
            return {"id": connector_id, "deleted": False, "dry_run": True, "would_delete": record}
        payload["connectors"] = [item for item in records if item is not record]
        _write_registry(target, payload)
    return {"id": connector_id, "deleted": True}


def public_registry() -> dict[str, Any]:
    """Return the public agents, connectors, and subscribable channels."""
    from .retained_relay import load_relays
    from .subscriptions import available_sources

    connectors = [
        {
            "id": item["id"],
            "kind": "connector",
            "adapter": item["adapter"],
            "instance": str(item.get("instance") or ""),
            "enabled": item["enabled"],
            "can_listen": item["direction"] in {"inbound", "bidirectional"},
            "can_send": item["direction"] in {"outbound", "bidirectional"},
            "channel_ids": item["channel_ids"],
        }
        for item in load_connectors()
    ]
    relays = load_relays()
    return {
        "version": 1,
        "agents": load_agents(),
        "connectors": connectors,
        "channels": available_sources(),
        "relays": relays,
    }
