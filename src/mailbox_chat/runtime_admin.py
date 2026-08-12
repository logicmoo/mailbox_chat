"""Trusted chat-admin commands that persist channel route configuration."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .listener_registry import listeners_file, load_listeners


DEFAULT_COMMAND_PREFIX = "!relay"
SAFE_ID = re.compile(r"[^a-zA-Z0-9_.-]+")


def _write_registry(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _reply(mailbox: Any, *, listener: dict[str, Any], channel_id: str, text: str) -> None:
    mailbox.send(
        "channel-relay", text, sender="relay-admin", message_type="relay_admin_response",
        channel_type=listener["adapter"], channel_id=channel_id,
        extra_fields={"listener_id": listener["id"], "presence_id": listener.get("presence_id") or ""},
    )


def handle_admin_command(mailbox: Any, *, listener_id: str, channel_id: str,
                         author: str, text: str) -> bool:
    listener = next((item for item in load_listeners() if item["id"] == listener_id), None)
    prefix = str((listener or {}).get("command_prefix")
                 or os.environ.get("MAILBOX_RELAY_COMMAND_PREFIX") or DEFAULT_COMMAND_PREFIX)
    if not text.strip().startswith(prefix):
        return False
    trusted = {str(item).casefold() for item in (listener or {}).get("trusted_admins", [])}
    if not listener or author.casefold() not in trusted:
        if listener:
            _reply(mailbox, listener=listener, channel_id=channel_id,
                   text=f"Relay administration denied for {author or 'unknown speaker'}")
        return True
    parts = text.strip().split()
    if len(parts) == 2 and parts[1] == "routes":
        payload = json.loads(listeners_file().read_text(encoding="utf-8"))
        route_ids = [str(item.get("id")) for item in payload.get("routes") or [] if item.get("enabled", True)]
        _reply(mailbox, listener=listener, channel_id=channel_id,
               text="Active routes: " + (", ".join(route_ids) if route_ids else "none"))
        return True
    if len(parts) >= 4 and parts[1] == "attach":
        destination_listener_id, destination_channel_id = parts[2], parts[3]
        controller_spec = parts[4] if len(parts) > 4 else "presence"
        destination = next((item for item in load_listeners() if item["id"] == destination_listener_id), None)
        if destination is None:
            _reply(mailbox, listener=listener, channel_id=channel_id,
                   text=f"Unknown destination listener: {destination_listener_id}")
            return True
        controller = ({"type": "relay_agent", "mailbox_recipient": controller_spec[6:]}
                      if controller_spec.startswith("agent:") else
                      {"type": "presence_controller",
                       "presence_id": str(destination.get("presence_id") or destination_listener_id)})
        path = listeners_file()
        payload = json.loads(path.read_text(encoding="utf-8"))
        route_id = SAFE_ID.sub("-", f"runtime-{listener_id}-{destination_listener_id}-{uuid.uuid4().hex[:8]}")
        payload.setdefault("routes", []).append({
            "id": route_id, "enabled": True,
            "source": {"listener_id": listener_id, "channel_id": channel_id},
            "destinations": [{"adapter": destination["adapter"],
                              "listener_id": destination_listener_id,
                              "channel_id": destination_channel_id}],
            "controller": controller,
            "created_at_runtime": True,
            "created_by": author,
        })
        _write_registry(path, payload)
        _reply(mailbox, listener=listener, channel_id=channel_id,
               text=f"Attached route {route_id} to {destination_listener_id}:{destination_channel_id}")
        return True
    if len(parts) == 3 and parts[1] == "detach":
        path = listeners_file()
        payload = json.loads(path.read_text(encoding="utf-8"))
        changed = False
        for route in payload.get("routes") or []:
            if route.get("id") == parts[2]:
                route["enabled"] = False
                changed = True
        if changed:
            _write_registry(path, payload)
        _reply(mailbox, listener=listener, channel_id=channel_id,
               text=(f"Detached route {parts[2]}" if changed else f"Unknown route: {parts[2]}"))
        return True
    _reply(mailbox, listener=listener, channel_id=channel_id,
           text=(f"Usage: {prefix} routes | {prefix} attach LISTENER CHANNEL "
                 f"[presence|agent:RECIPIENT] | {prefix} detach ROUTE"))
    return True
