"""Load and validate the non-secret mailbox connector registry."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from threading import Lock
from typing import Any

from . import mailbox_store


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR_ENV = "MAILBOX_RELAY_CONFIG_DIR"
VALID_DIRECTIONS = {"inbound", "outbound", "bidirectional"}
_REGISTRY_WRITE_LOCK = Lock()


def store_root(path: Path | None = None) -> Path:
    """Root directory of the mailbox/registry store.

    The registries used to live inside a single ``relays.json`` document; they
    are now individual ``server_registry_*`` mailbox files under the mailbox
    root. Callers may still pass an explicit root (chiefly tests).
    """
    if path is not None:
        return Path(path)
    from . import agent_mailbox
    return agent_mailbox.mailbox_dir()


def underscored_mailbox_twin(agent_id: str) -> str:
    """Underscored twin of a hyphens-only agent id (agent ``a-b-c`` reads ``a_b_c``)."""
    identifier = agent_id.strip()
    if not identifier:
        raise ValueError("agent_id is required")
    return identifier.replace("-", "_")


def agent_mailbox_address(agent_id: str, path: Path | None = None) -> str:
    """Return the stable writable mailbox address (DM mailbox) for an agent.

    The agents registry is the first authority: an agent entry may declare its
    ``mailbox`` mailbox explicitly. Otherwise the direct-message mailbox is the
    underscored twin of the hyphens-only agent id.
    """
    identifier = agent_id.strip()
    if not identifier:
        raise ValueError("agent_id is required")
    try:
        for item in load_agents(path):
            if str(item.get("agent_id") or "") == identifier:
                return str(item["mailbox"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass
    return underscored_mailbox_twin(identifier)


def config_dir() -> Path:
    configured = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local = Path.cwd() / "config"
    return local.resolve() if local.is_dir() else PROJECT_ROOT / "config"


def _expand_mailbox_ids(value: str) -> list[str]:
    if value.startswith("$"):
        value = os.environ.get(value[1:], "")
    return [item.strip() for item in value.split(",") if item.strip()]


def load_agents(path: Path | None = None) -> list[dict[str, Any]]:
    """Load stable agents and their possibly concurrent presences."""
    root = store_root(path)
    agents: list[dict[str, Any]] = []
    agent_ids: set[str] = set()
    presence_ids: set[str] = set()
    records = mailbox_store.registry_entries(root, mailbox_store.AGENTS_REGISTRY)
    for index, record in enumerate(records):
        raw = record.get("payload")
        if not isinstance(raw, dict):
            raise ValueError(f"agents[{index}] must have an object payload")
        # The agents registry also stores durable cursor owners; skip those
        # auto-created bookkeeping entries. Real agents use richer payload kinds
        # (e.g. "agent", "agent_entry").
        if str(raw.get("kind") or "") == "cursor_owner":
            continue
        # The registry key (envelope id) is the authoritative agent id; payload
        # agent names are a separate, untracked namespace and may be stale.
        agent_id = str(record.get("id") or raw.get("agent_id") or "").strip()
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
            # Registry-declared mailbox wins; else the underscored twin.
            "mailbox": str(raw.get("mailbox") or "").strip() or underscored_mailbox_twin(agent_id),
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

    root = store_root(path)
    with _REGISTRY_WRITE_LOCK:
        agents = load_agents(root)
        presence_owner = {
            presence["presence_id"]: agent["agent_id"]
            for agent in agents
            for presence in agent["presences"]
        }
        if presence_id and presence_id in presence_owner and presence_owner[presence_id] != agent_id:
            raise ValueError(f"presence {presence_id} is already registered to {presence_owner[presence_id]}")

        record = mailbox_store.get_entry(root, mailbox_store.AGENTS_REGISTRY, agent_id)
        payload = dict(record.get("payload") or {}) if record else {}
        agent_created = record is None
        raw_presences = list(payload.get("presences") or [])
        existing_presence = next(
            (item for item in raw_presences if isinstance(item, dict)
             and str(item.get("presence_id") or "").strip() == presence_id),
            None,
        ) if presence_id else None
        presence_created = bool(presence_id and existing_presence is None)

        if dry_run:
            return {
                "dry_run": True,
                "agent_id": agent_id,
                "presence_id": presence_id,
                "would_create_agent": agent_created,
                "would_create_presence": presence_created,
                "would_add_mailboxes": [agent_mailbox_address(agent_id, root)] if agent_created else [],
                "initial_mailboxes": [agent_mailbox_address(agent_id, root)],
            }

        if presence_created:
            raw_presences.append({"presence_id": presence_id})
        payload = {
            "kind": "agent",
            "agent_id": agent_id,
            **{key: value for key, value in payload.items()
               if key not in {"kind", "agent_id", "presences"}},
            "presences": raw_presences,
        }
        mailbox_store.put_entry(root, mailbox_store.AGENTS_REGISTRY, agent_id, payload)

    if agent_created or presence_created:
        from . import agent_mailbox
        from .subscriptions import SERVER_EVENTS_MAILBOX

        event_mailbox = SERVER_EVENTS_MAILBOX
        if agent_created:
            agent_mailbox.send(
                event_mailbox,
                f"Agent registered: {agent_id}",
                sender="mailbox-server",
                message_type="agent_registered",
                extra_fields={"agent_id": agent_id},
                root=mailbox_root or root,
            )
        if presence_created:
            agent_mailbox.send(
                event_mailbox,
                f"Presence registered: {presence_id} for {agent_id}",
                sender="mailbox-server",
                message_type="presence_registered",
                extra_fields={
                    "agent_id": agent_id,
                    "presence_id": presence_id,
                },
                root=mailbox_root or root,
            )

    return {
        "agent": next(item for item in load_agents(root) if item["agent_id"] == agent_id),
        "agent_created": agent_created,
        "presence_created": presence_created,
        "initial_mailboxes": [agent_mailbox_address(agent_id, root)],
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

    root = store_root(path)
    with _REGISTRY_WRITE_LOCK:
        record = mailbox_store.get_entry(root, mailbox_store.AGENTS_REGISTRY, agent_id)
        if record is None:
            return {"agent_id": agent_id, "presence_id": presence_id,
                    "agent_removed": False, "presence_removed": False}
        payload = dict(record.get("payload") or {})
        owned_presences = {
            str(item.get("presence_id") or "").strip()
            for item in payload.get("presences") or [] if isinstance(item, dict)
        }
        connectors = load_connectors(root)
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
                    agent_mailbox=agent_mailbox_address(agent_id, root), remove_agent=False,
                    dry_run=True, root=mailbox_root or root,
                )
                return {
                    "agent_id": agent_id, "presence_id": presence_id,
                    "would_remove_agent": False, "would_remove_presence": True,
                    **preview,
                }
            payload["presences"] = [
                item for item in payload.get("presences") or []
                if not (isinstance(item, dict)
                        and str(item.get("presence_id") or "").strip() == presence_id)
            ]
            mailbox_store.put_entry(root, mailbox_store.AGENTS_REGISTRY, agent_id, payload)
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
                    agent_mailbox=agent_mailbox_address(agent_id, root), remove_agent=True,
                    dry_run=True, root=mailbox_root or root,
                )
                return {
                    "agent_id": agent_id, "presence_id": "",
                    "would_remove_agent": True, "would_remove_presence": False,
                    **preview,
                }
            mailbox_store.delete_entry(root, mailbox_store.AGENTS_REGISTRY, agent_id)
            agent_removed, presence_removed = True, False

    from . import agent_mailbox
    from .subscriptions import SERVER_EVENTS_MAILBOX

    event_type = "presence_unregistered" if presence_removed else "agent_unregistered"
    event_text = (f"Presence unregistered: {presence_id} for {agent_id}" if presence_removed
                  else f"Agent unregistered: {agent_id}")
    event_fields = {"agent_id": agent_id}
    if presence_removed:
        event_fields["presence_id"] = presence_id
    agent_mailbox.send(
        SERVER_EVENTS_MAILBOX, event_text, sender="mailbox-server",
        message_type=event_type, extra_fields=event_fields, root=mailbox_root or root,
    )
    purge_result: dict[str, Any] = {"purged": False}
    if purge:
        purge_result = agent_mailbox.purge_agent_data(
            agent_id,
            presence_ids={presence_id} if presence_removed else owned_presences,
            agent_mailbox=agent_mailbox_address(agent_id, root),
            remove_agent=agent_removed,
            dry_run=False,
            root=mailbox_root or root,
        )
    remaining = next((item for item in load_agents(root) if item["agent_id"] == agent_id), None)
    return {
        "agent_id": agent_id,
        "presence_id": presence_id,
        "agent": remaining,
        "agent_removed": agent_removed,
        "presence_removed": presence_removed,
        **purge_result,
    }


def load_connectors(path: Path | None = None) -> list[dict[str, Any]]:
    root = store_root(path)
    connectors: list[dict[str, Any]] = []
    agents = {item["agent_id"]: item for item in load_agents(root)}
    presence_owners = {
        presence["presence_id"]: agent_id
        for agent_id, agent in agents.items()
        for presence in agent["presences"]
    }
    seen: set[str] = set()
    records = mailbox_store.registry_entries(root, mailbox_store.CONNECTORS_REGISTRY)
    for index, record in enumerate(records):
        raw = record.get("payload")
        if not isinstance(raw, dict):
            raise ValueError(f"connectors[{index}] must have an object payload")
        connector_id = str(record.get("id") or raw.get("id") or "").strip()
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
        mailbox_ids: list[str] = []
        for value in raw.get("mailbox_ids") or []:
            mailbox_ids.extend(_expand_mailbox_ids(str(value)))
        connectors.append({
            **raw,
            "kind": "connector",
            "id": connector_id,
            "adapter": adapter,
            "direction": direction,
            "enabled": bool(raw.get("enabled", True)),
            "mailbox_ids": list(dict.fromkeys(mailbox_ids)),
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


def add_connector_mailbox(adapter: str, instance: str, mailbox_id: str,
                          *, path: Path | None = None) -> dict[str, Any]:
    """Persist one externally monitored mailbox on a selected connector."""
    adapter, instance, mailbox_id = adapter.strip(), instance.strip(), mailbox_id.strip()
    if not adapter or not instance or not mailbox_id:
        raise ValueError("adapter, instance, and mailbox_id are required")
    root = store_root(path)
    with _REGISTRY_WRITE_LOCK:
        raw_matches = [
            item for item in load_connectors(root)
            if item["adapter"] == adapter.lower()
            and item["direction"] in {"inbound", "bidirectional"}
            and item["enabled"]
        ]
        if instance.isdigit():
            index = int(instance)
            if index >= len(raw_matches):
                raise ValueError(f"no {adapter} connector at index {instance}")
            record = raw_matches[index]
        else:
            matches = [
                item for item in raw_matches
                if str(item.get("instance") or item.get("base_url") or "").rstrip("/")
                   .removeprefix("https://").removeprefix("http://") == instance
            ]
            if len(matches) != 1:
                raise ValueError(f"could not uniquely select {adapter} connector {instance}")
            record = matches[0]
        connector_id = str(record.get("id") or "")
        stored = mailbox_store.get_entry(root, mailbox_store.CONNECTORS_REGISTRY, connector_id)
        payload = dict((stored or {}).get("payload") or {})
        mailbox_ids = list(payload.get("mailbox_ids") or [])
        if mailbox_id not in mailbox_ids:
            mailbox_ids.append(mailbox_id)
            payload["mailbox_ids"] = mailbox_ids
            mailbox_store.put_entry(root, mailbox_store.CONNECTORS_REGISTRY,
                                    connector_id, payload)
    return {"connector_id": connector_id, "mailbox_id": mailbox_id, "monitored": True}


def register_connector(
    connector_id: str,
    kind: str,
    *,
    direction: str = "bidirectional",
    instance: str = "",
    mailbox_ids: list[str] | None = None,
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
    root = store_root(path)
    with _REGISTRY_WRITE_LOCK:
        if mailbox_store.get_entry(root, mailbox_store.CONNECTORS_REGISTRY, connector_id):
            raise ValueError(f"connector id is already registered: {connector_id}")
        record = {
            **dict(config or {}), "kind": "connector", "id": connector_id, "adapter": kind,
            "direction": direction, "enabled": True,
        }
        if instance:
            record["instance"] = instance
        if mailbox_ids:
            record["mailbox_ids"] = list(dict.fromkeys(mailbox_ids))
        if dry_run:
            return {**record, "dry_run": True}
        mailbox_store.put_entry(root, mailbox_store.CONNECTORS_REGISTRY, connector_id, record)
        load_connectors(root)
    return record


def unregister_connector(connector_id: str, *, dry_run: bool = False,
                         path: Path | None = None) -> dict[str, Any]:
    """Remove one connector."""
    connector_id = connector_id.strip()
    root = store_root(path)
    with _REGISTRY_WRITE_LOCK:
        record = mailbox_store.get_entry(root, mailbox_store.CONNECTORS_REGISTRY, connector_id)
        if record is None:
            return {"id": connector_id, "deleted": False, "dry_run": dry_run}
        if dry_run:
            return {"id": connector_id, "deleted": False, "dry_run": True,
                    "would_delete": record.get("payload") or {}}
        mailbox_store.delete_entry(root, mailbox_store.CONNECTORS_REGISTRY, connector_id)
    return {"id": connector_id, "deleted": True}


def public_registry(path: Path | None = None) -> dict[str, Any]:
    """Return the public agents, connectors, and subscribable mailboxes."""
    from .retained_relay import load_relays
    from .subscriptions import available_sources

    root = store_root(path)
    connectors = [
        {
            "id": item["id"],
            "kind": "connector",
            "adapter": item["adapter"],
            "instance": str(item.get("instance") or ""),
            "enabled": item["enabled"],
            "can_listen": item["direction"] in {"inbound", "bidirectional"},
            "can_send": item["direction"] in {"outbound", "bidirectional"},
            "mailbox_ids": item["mailbox_ids"],
        }
        for item in load_connectors(root)
    ]
    relays = load_relays(root)
    return {
        "version": 1,
        "agents": load_agents(root),
        "connectors": connectors,
        "mailboxes": available_sources(root),
        "relays": relays,
    }
