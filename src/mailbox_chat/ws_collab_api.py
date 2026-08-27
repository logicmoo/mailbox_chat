"""ws_collab-compatible mailbox v1 API for mailbox_chat.

This module provides a stdlib-server friendly implementation of the
``/ws_collab`` and ``/ws_collab/v1`` mailbox surfaces documented by
ws_collab's MAILBOX_SERVER.md contract, backed by mailbox_chat's durable
mailbox JSON store.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from . import __version__, mailbox_store
from .connector_registry import load_agents
from .subscriptions import available_sources, delete_mailbox, ensure_mailbox, set_subscription
from .websocket_chat import encode_frame, read_frame


WS_COLLAB_MOUNT = "/ws_collab"
WS_COLLAB_VERSIONED = "/ws_collab/v1"
CONVERSATION_MAILBOX = "conversation"
CONVERSATION_EVENT_TYPE = "CONVERSATION_MESSAGE"
MAILBOX_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_LIMIT = 2000


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def is_ws_collab_path(path: str) -> bool:
    return path == WS_COLLAB_MOUNT or path.startswith(WS_COLLAB_MOUNT + "/")


def is_ws_collab_ws_path(path: str) -> bool:
    return path in {f"{WS_COLLAB_MOUNT}/ws", f"{WS_COLLAB_VERSIONED}/ws"}


def _normalized_route(path: str) -> str:
    if path == WS_COLLAB_MOUNT:
        return "/"
    if path.startswith(WS_COLLAB_VERSIONED + "/"):
        return path[len(WS_COLLAB_VERSIONED):]
    if path == WS_COLLAB_VERSIONED:
        return "/"
    if path.startswith(WS_COLLAB_MOUNT + "/"):
        return path[len(WS_COLLAB_MOUNT):]
    return ""


def _state_path(root: Path) -> Path:
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime / "ws_collab_v1_state.json"


def _default_state() -> dict[str, Any]:
    return {"mailboxes": {}, "mailbox_config": {}, "agents": {}}


def _load_state(root: Path) -> dict[str, Any]:
    path = _state_path(root)
    if not path.is_file():
        return _default_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_state()
    if not isinstance(payload, dict):
        return _default_state()
    return {
        "mailboxes": dict(payload.get("mailboxes") or {}),
        "mailbox_config": dict(payload.get("mailbox_config") or {}),
        "agents": dict(payload.get("agents") or {}),
        "idempotency": dict(payload.get("idempotency") or {}),
    }


def _save_state(root: Path, state: dict[str, Any]) -> None:
    path = _state_path(root)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _global_name_prefix() -> str:
    return str(os.environ.get("WS_COLLAB_GLOBAL_NAME") or "ws_collab").strip()


def _global_name(name: str) -> str:
    prefix = _global_name_prefix()
    return f"{prefix}/{name}" if prefix else name


def _is_loopback(client_ip: str | None) -> bool:
    if not client_ip:
        return False
    try:
        return ipaddress.ip_address(client_ip).is_loopback
    except ValueError:
        return False


def _parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", ""}


def _entry_token_to_index(token: str | None) -> int:
    if token is None or token == "":
        return 0
    return mailbox_store.entry_index(str(token)) + 1


def _event_to_message(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") or {}
    text = ""
    for key in ("text", "message", "content", "summary", "utterance"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            text = value
            break
    source = str(event.get("source_id") or "")
    stream = str(event.get("stream") or "")
    return {
        "id": event.get("id"),
        "timestamp": event.get("ts"),
        "from": source,
        "to": data.get("to") or data.get("target") or stream,
        "send_to": data.get("send_to"),
        "text": text,
        "type": event.get("type"),
        "mailboxId": stream,
        "mailboxName": stream,
        "author": source,
        "authorName": data.get("author_name") or source,
        "raw": event,
    }


def _record_to_event(stream: str, key: str, record: dict[str, Any]) -> dict[str, Any]:
    ts = str(record.get("ts") or record.get("timestamp") or utc_now_iso())
    source_id = str(record.get("source_id") or record.get("from") or "operator")
    source_kind = str(record.get("source_kind") or "agent")
    data = record.get("data")
    if not isinstance(data, dict):
        data = {"text": str(record.get("text") or "")}
        if record.get("to"):
            data["to"] = record.get("to")
        if record.get("send_to"):
            data["send_to"] = record.get("send_to")
    event = {
        "id": str(record.get("id") or uuid.uuid4()),
        "stream": stream,
        "seq": mailbox_store.entry_index(key) + 1,
        "cursor": key,
        "type": str(record.get("type") or "message"),
        "ts": ts,
        "schema_version": int(record.get("schema_version") or 1),
        "source_id": source_id,
        "source_kind": source_kind,
        "data": data,
    }
    for field in ("idempotency_key", "correlation_id"):
        value = record.get(field)
        if value:
            event[field] = value
    return event


def _event_matches_filters(event: dict[str, Any], filters: dict[str, Any] | None = None) -> bool:
    if not filters:
        return True
    source_id = str(event.get("source_id") or "")
    source_kind = str(event.get("source_kind") or "")
    event_type = str(event.get("type") or "")
    correlation = str(event.get("correlation_id") or "")
    ts = str(event.get("ts") or "")
    text = str((event.get("data") or {}).get("text") or "")
    if filters.get("type") and event_type != str(filters["type"]):
        return False
    if filters.get("source_id") and source_id != str(filters["source_id"]):
        return False
    if filters.get("source_kind") and source_kind != str(filters["source_kind"]):
        return False
    if filters.get("correlation_id") and correlation != str(filters["correlation_id"]):
        return False
    if filters.get("since") and ts < str(filters["since"]):
        return False
    if filters.get("until") and ts > str(filters["until"]):
        return False
    if filters.get("text") and str(filters["text"]).lower() not in text.lower():
        return False
    return True


def _known_mailbox(root: Path, name: str, state: dict[str, Any]) -> bool:
    if not name:
        return False
    if name in state.get("mailboxes", {}):
        return True
    if name == CONVERSATION_MAILBOX:
        return True
    path = mailbox_store.mailbox_path(root, name)
    if path.is_file():
        return True
    return any(str(item.get("id") or "") == name for item in available_sources(root))


def _mailbox_writable(root: Path, name: str, state: dict[str, Any]) -> bool:
    if not _known_mailbox(root, name, state):
        return False
    metadata = dict((state.get("mailboxes") or {}).get(name) or {})
    source = str(metadata.get("source") or "jsonl")
    if source.startswith(("merge:", "self:", "disk:", "http://", "https://")):
        return False
    return bool(metadata.get("writable", True))


def _ensure_message_mailbox(root: Path, mailbox_id: str) -> None:
    with mailbox_store.STORE_LOCK:
        document = mailbox_store.load_mailbox(root, mailbox_id)
        if "messages" not in document:
            raise ValueError(f"{mailbox_id!r} is not a message mailbox")
        mailbox_store.save_mailbox(root, document)


def _read_events(
    root: Path,
    stream: str,
    *,
    after: str | None = None,
    limit: int = 100,
    filters: dict[str, Any] | None = None,
    allow_missing: bool = False,
) -> dict[str, Any]:
    stream = str(stream or "").strip()
    if not stream:
        raise ValueError("stream is required")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    mailbox_file = mailbox_store.mailbox_path(root, stream)
    if not mailbox_file.is_file() and allow_missing:
        return {
            "stream": stream,
            "events": [],
            "next_cursor": after,
            "has_more": False,
            "server_time": utc_now_iso(),
        }
    if not mailbox_file.is_file():
        raise ValueError(f"unknown stream: {stream!r}")
    start_index = _entry_token_to_index(after)
    document = mailbox_store.load_mailbox(root, stream)
    found: list[dict[str, Any]] = []
    for key, record in mailbox_store.ordered_messages(document, start_index=start_index):
        event = _record_to_event(stream, key, record)
        if _event_matches_filters(event, filters):
            found.append(event)
        if len(found) > limit:
            break
    has_more = len(found) > limit
    events = found[:limit]
    next_cursor = events[-1]["cursor"] if events else after
    return {
        "stream": stream,
        "events": events,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "server_time": utc_now_iso(),
    }


def _publish_event(
    root: Path,
    state: dict[str, Any],
    *,
    stream: str,
    event_type: str,
    data: dict[str, Any],
    source_id: str,
    source_kind: str,
    idempotency_key: str = "",
    correlation_id: str = "",
) -> dict[str, Any]:
    _ensure_message_mailbox(root, stream)
    if idempotency_key:
        document = mailbox_store.load_mailbox(root, stream)
        for key, record in mailbox_store.ordered_messages(document):
            if str(record.get("idempotency_key") or "") == idempotency_key:
                existing = _record_to_event(stream, key, record)
                return {
                    "id": existing["id"],
                    "seq": existing["seq"],
                    "cursor": existing["cursor"],
                    "duplicate": True,
                    "stream": stream,
                    "event_type": str(existing.get("type") or event_type),
                    "server_time": str(existing.get("ts") or utc_now_iso()),
                }
    event = {
        "id": str(uuid.uuid4()),
        "stream": stream,
        "type": str(event_type or CONVERSATION_EVENT_TYPE),
        "ts": utc_now_iso(),
        "schema_version": 1,
        "source_id": source_id,
        "source_kind": source_kind,
        "data": dict(data or {}),
    }
    if idempotency_key:
        event["idempotency_key"] = idempotency_key
    if correlation_id:
        event["correlation_id"] = correlation_id
    stored = mailbox_store.append_message(root, stream, event)
    token = str(stored.get("entry_number") or "")
    seq = mailbox_store.entry_index(token) + 1
    return {
        "id": event["id"],
        "seq": seq,
        "cursor": token,
        "duplicate": False,
        "stream": stream,
        "event_type": event["type"],
        "server_time": event["ts"],
    }


def _mailbox_cursor_payload(root: Path, mailbox: str, agent: str) -> dict[str, Any]:
    document = mailbox_store.load_mailbox(root, mailbox)
    total = int(document.get("count") or 0) if "messages" in document else 0
    position = mailbox_store.get_cursor(root, agent, mailbox)
    consumed = int(position.get("consumed") or 0) if position else 0
    consumed = max(0, min(total, consumed))
    last_read_id = None
    next_unread_id = None
    if consumed > 0:
        for index, (_key, record) in enumerate(mailbox_store.ordered_messages(document)):
            if index == consumed - 1:
                last_read_id = record.get("id")
                break
    if consumed < total:
        for index, (_key, record) in enumerate(mailbox_store.ordered_messages(document)):
            if index == consumed:
                next_unread_id = record.get("id")
                break
    return {
        "mailbox": mailbox,
        "agent": agent,
        "initialized": position is not None,
        "offset": consumed,
        "size": total,
        "behind": max(0, total - consumed),
        "entries_consumed": consumed,
        "entry_next": next_unread_id,
        "entries_total": total,
        "last_read_id": last_read_id,
        "next_unread_id": next_unread_id,
    }


def capabilities_payload() -> dict[str, Any]:
    return {
        "version": __version__,
        "product": "WS_COLLAB",
        "transports": ["http", "https", "ws", "wss"],
        "rest_base": "/ws_collab",
        "versioned_base": "/ws_collab/v1",
        "features": {
            "cursor_pagination": True,
            "idempotent_writes": True,
            "websocket_resume": True,
            "mailboxes": True,
        },
    }


def _mailbox_descriptor(root: Path, mailbox_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    document = mailbox_store.load_mailbox(root, mailbox_id)
    messages = int(document.get("count") or 0) if "messages" in document else 0
    source = str(metadata.get("source") or "jsonl")
    writable = bool(metadata.get("writable", True))
    if source.startswith(("merge:", "self:", "disk:", "http://", "https://")):
        writable = False
    return {
        "id": mailbox_id,
        "name": mailbox_id,
        "global_name": _global_name(mailbox_id),
        "purpose": str(metadata.get("purpose") or ""),
        "kind": "stream",
        "source": source,
        "transports": ["jsonl", "ws"],
        "hidden": bool(metadata.get("hidden", False)),
        "writable": writable,
        "messages": messages,
        "filename": f"{mailbox_id}.json",
        "endpoints": {
            "read": f"/ws_collab/v1/mailbox/messages?mailbox={mailbox_id}",
            "send": "/ws_collab/v1/mailbox/send",
            "tail": f"/ws_collab/v1/streams/{mailbox_id}/tail",
            "ws": "/ws_collab/ws",
        },
    }


def handle_ws_collab_get(path: str, query: str, *, root: Path) -> tuple[int, dict[str, Any]] | None:
    if not is_ws_collab_path(path):
        return None
    route = _normalized_route(path)
    params = parse_qs(query or "", keep_blank_values=True)
    state = _load_state(root)
    try:
        if route in {"/", "/health", "/status", "/ready", "/endpoints"}:
            return 200, {
                "status": "ok",
                "version": __version__,
                "server_time": utc_now_iso(),
                "endpoints": {
                    "mailboxes": "/ws_collab/v1/mailbox/mailboxes",
                    "messages": "/ws_collab/v1/mailbox/messages",
                    "send": "/ws_collab/v1/mailbox/send",
                    "ws": "/ws_collab/ws",
                },
            }
        if route == "/capabilities":
            return 200, capabilities_payload()
        if route == "/mailbox/mailboxes":
            agent = str((params.get("agent") or [""])[0] or "")
            include_activity = _parse_bool((params.get("include_activity") or ["false"])[0], False)
            names = {CONVERSATION_MAILBOX}
            names.update(str(item.get("id") or "") for item in available_sources(root))
            names.update(str(key) for key in (state.get("mailboxes") or {}).keys())
            mailboxes = []
            for mailbox_id in sorted(name for name in names if name):
                metadata = dict((state.get("mailboxes") or {}).get(mailbox_id) or {})
                if metadata.get("hidden"):
                    continue
                descriptor = _mailbox_descriptor(root, mailbox_id, metadata)
                if include_activity:
                    document = mailbox_store.load_mailbox(root, mailbox_id)
                    last_timestamp = None
                    for _key, record in mailbox_store.ordered_messages(document):
                        last_timestamp = record.get("timestamp") or record.get("ts") or last_timestamp
                    descriptor["last_message_at"] = last_timestamp
                if agent:
                    descriptor.update(_mailbox_cursor_payload(root, mailbox_id, agent))
                mailboxes.append(descriptor)
            return 200, {
                "place": "ws_collab",
                "global_name": _global_name_prefix(),
                "mailboxes": mailboxes,
                "server_time": utc_now_iso(),
            }
        if route == "/mailbox/agents":
            directory: dict[str, dict[str, Any]] = {"operator": {"id": "operator", "kind": "operator"}}
            for item in load_agents(root):
                agent_id = str(item.get("agent_id") or "").strip()
                if agent_id:
                    directory.setdefault(agent_id, {"id": agent_id, "kind": "agent"}).update({
                        "mailbox": item.get("mailbox"),
                        "presences": item.get("presences") or [],
                    })
            for agent_id, properties in dict(state.get("agents") or {}).items():
                if not agent_id:
                    continue
                entry = directory.setdefault(str(agent_id), {"id": str(agent_id), "kind": "agent"})
                if isinstance(properties, dict):
                    entry.update(properties)
            conversation = mailbox_store.load_mailbox(root, CONVERSATION_MAILBOX)
            for _key, record in mailbox_store.ordered_messages(conversation):
                source = str(record.get("source_id") or record.get("from") or "").strip()
                if source:
                    directory.setdefault(source, {"id": source, "kind": "agent"})
            return 200, {"agents": [item for item in directory.values() if item.get("id")]}
        if route == "/mailbox/messages":
            mailbox = str((params.get("mailbox") or [""])[0] or "")
            send_to = str((params.get("send_to") or [""])[0] or "")
            to = str((params.get("to") or [""])[0] or "")
            sender = str((params.get("from") or [""])[0] or "")
            text = str((params.get("text") or [""])[0] or "")
            do_filter = _parse_bool((params.get("filter") or ["false"])[0], False)
            limit = max(1, min(MAX_LIMIT, int((params.get("limit") or ["300"])[0] or "300")))
            stream = mailbox or send_to or CONVERSATION_MAILBOX
            events = _read_events(root, stream, limit=limit, allow_missing=True)
            messages = [_event_to_message(event) for event in events["events"]]
            if do_filter:
                needle = text.lower()

                def keep(message: dict[str, Any]) -> bool:
                    if sender and str(message.get("from") or "") != sender:
                        return False
                    if to and str(message.get("to") or "") != to:
                        return False
                    if send_to and str(message.get("send_to") or message.get("mailboxId") or "") != send_to:
                        return False
                    if needle and needle not in str(message.get("text") or "").lower():
                        return False
                    return True

                messages = [item for item in messages if keep(item)]
            return 200, {"messages": messages, "user": sender, "peer": stream}
        if route == "/mailbox/mailbox-config":
            mailbox = str((params.get("mailbox") or [""])[0] or "")
            return 200, {"mailbox": mailbox, "config": dict((state.get("mailbox_config") or {}).get(mailbox) or {})}
        if route == "/mailbox/cursor":
            mailbox = str((params.get("mailbox") or [""])[0] or "")
            agent = str((params.get("agent") or [""])[0] or "")
            if not mailbox or not agent:
                raise ValueError("mailbox and agent are required")
            return 200, _mailbox_cursor_payload(root, mailbox, agent)
        if route.startswith("/streams/") and route.endswith("/tail"):
            mailbox = route[len("/streams/"):-len("/tail")].strip("/")
            if not mailbox:
                raise ValueError("mailbox is required")
            count = max(1, min(MAX_LIMIT, int((params.get("count") or ["50"])[0] or "50")))
            document = mailbox_store.load_mailbox(root, mailbox)
            all_events = [_record_to_event(mailbox, key, record)
                          for key, record in mailbox_store.ordered_messages(document)]
            return 200, {"stream": mailbox, "events": all_events[-count:], "server_time": utc_now_iso()}
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return 400, {"error": str(error)}
    return 404, {"error": "not found"}


def handle_ws_collab_post(path: str, payload: dict[str, Any], *, root: Path) -> tuple[int, dict[str, Any]] | None:
    if not is_ws_collab_path(path):
        return None
    route = _normalized_route(path)
    state = _load_state(root)
    try:
        if route == "/mailbox/send":
            text = str(payload.get("text") or "")
            if not text.strip():
                raise ValueError("message 'text' is required")
            to = str(payload.get("to") or "")
            send_to = str(payload.get("send_to") or "")
            sender = str(payload.get("sender") or payload.get("source_id") or "operator")
            source_kind = str(payload.get("source_kind") or "agent")
            topic = next(
                (name for name in (send_to, to) if name and _mailbox_writable(root, name, state)),
                CONVERSATION_MAILBOX,
            )
            ensure_mailbox(topic, path=root)
            _ensure_message_mailbox(root, topic)
            data = {"text": text}
            if to and to != topic:
                data["to"] = to
            if send_to:
                data["send_to"] = send_to
            published = _publish_event(
                root,
                state,
                stream=topic,
                event_type=CONVERSATION_EVENT_TYPE,
                data=data,
                source_id=sender,
                source_kind=source_kind,
                idempotency_key=str(payload.get("idempotency_key") or ""),
            )
            event = {
                "id": published["id"],
                "stream": topic,
                "type": CONVERSATION_EVENT_TYPE,
                "ts": published["server_time"],
                "source_id": sender,
                "source_kind": source_kind,
                "data": data,
            }
            message = _event_to_message(event)
            return 200, {"message": {**message, "seq": published["seq"], "cursor": published["cursor"]}}
        if route in {"/mailbox/create", "/mailbox/mailboxes"}:
            name = str(payload.get("id") or payload.get("name") or "").strip()
            if not MAILBOX_NAME_RE.match(name):
                raise ValueError("mailbox name must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
            ensure_mailbox(name, path=root)
            _ensure_message_mailbox(root, name)
            metadata = dict((state.get("mailboxes") or {}).get(name) or {})
            metadata.update({
                "purpose": str(payload.get("purpose") or metadata.get("purpose") or ""),
                "hidden": bool(payload.get("hidden", metadata.get("hidden", False))),
                "writable": bool(payload.get("writable", metadata.get("writable", True))),
                "global_name": str(payload.get("global_name") or metadata.get("global_name") or ""),
                "source": str(payload.get("source") or metadata.get("source") or "jsonl"),
            })
            state.setdefault("mailboxes", {})[name] = metadata
            _save_state(root, state)
            return 200, {"created": True, "mailbox": _mailbox_descriptor(root, name, metadata)}
        if route == "/mailbox/delete":
            mailbox_id = str(payload.get("id") or payload.get("name") or "").strip()
            if not mailbox_id:
                raise ValueError("id is required")
            result = delete_mailbox(mailbox_id, force=True, path=root)
            state.get("mailboxes", {}).pop(mailbox_id, None)
            state.get("mailbox_config", {}).pop(mailbox_id, None)
            _save_state(root, state)
            return 200, result
        if route == "/mailbox/mailbox-config":
            mailbox = str(payload.get("mailbox") or "").strip()
            config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
            state.setdefault("mailbox_config", {})[mailbox] = dict(config)
            _save_state(root, state)
            return 200, {"mailbox": mailbox, "config": config, "subscribed": []}
        if route == "/mailbox/cursor":
            mailbox = str(payload.get("mailbox") or "").strip()
            agent = str(payload.get("agent") or "").strip()
            start = str(payload.get("start") or "now").strip().lower()
            if not mailbox or not agent:
                raise ValueError("mailbox and agent are required")
            document = mailbox_store.load_mailbox(root, mailbox)
            total = int(document.get("count") or 0) if "messages" in document else 0
            next_index = 0 if start in {"beginning", "start"} else total
            mailbox_store.set_cursor(root, agent, mailbox, next_index)
            return 200, _mailbox_cursor_payload(root, mailbox, agent)
        if route == "/mailbox/subscription":
            mailbox = str(payload.get("mailbox") or "").strip()
            agent = str(payload.get("agent") or payload.get("identity") or "").strip()
            subscribed = bool(payload.get("subscribed", payload.get("state", True)))
            if not mailbox or not agent:
                raise ValueError("mailbox and agent are required")
            set_subscription(mailbox, agent, enabled=subscribed, path=root)
            return 200, {"agent": agent, "mailbox": mailbox, "state": subscribed}
        if route == "/mailbox/agents":
            agent_id = str(payload.get("id") or "").strip()
            if not agent_id:
                raise ValueError("id is required")
            properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
            current = dict((state.get("agents") or {}).get(agent_id) or {})
            current.update(properties)
            current["id"] = agent_id
            state.setdefault("agents", {})[agent_id] = current
            _save_state(root, state)
            return 200, current
        if route == "/mailbox/entity":
            kind = str(payload.get("kind") or "").strip()
            entity_id = str(payload.get("id") or "").strip()
            entry = payload.get("entry") if isinstance(payload.get("entry"), dict) else {}
            if kind == "agent" and entity_id:
                current = dict((state.get("agents") or {}).get(entity_id) or {})
                current.update(entry)
                current["id"] = entity_id
                state.setdefault("agents", {})[entity_id] = current
                _save_state(root, state)
            if kind == "mailbox" and entity_id:
                current = dict((state.get("mailboxes") or {}).get(entity_id) or {})
                current.update(entry)
                state.setdefault("mailboxes", {})[entity_id] = current
                _save_state(root, state)
            return 200, {"kind": kind, "id": entity_id, "entry": entry}
        if route == "/mailbox/record":
            record_id = str(payload.get("id") or "").strip()
            record = payload.get("record")
            mode = str(payload.get("mode") or "at-end")
            if not record_id or not isinstance(record, dict):
                raise ValueError("id and record object are required")
            raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
            inner = record.get("data") if isinstance(record.get("data"), dict) else None
            if inner is None and isinstance(raw.get("data"), dict):
                inner = raw["data"]
            if inner is None:
                inner = {"text": record.get("text") or ""}
            stream = str(record.get("stream") or record.get("mailboxId") or raw.get("stream") or CONVERSATION_MAILBOX)
            if not _mailbox_writable(root, stream, state):
                raise ValueError(f"mailbox {stream!r} is not writable")
            event_type = str(record.get("type") or raw.get("type") or CONVERSATION_EVENT_TYPE)
            source_id = str(
                record.get("source_id")
                or record.get("author")
                or record.get("from")
                or raw.get("source_id")
                or "operator"
            )
            source_kind = str(record.get("source_kind") or raw.get("source_kind") or "operator")
            published = _publish_event(
                root,
                state,
                stream=stream,
                event_type=event_type,
                data={**inner, "edited_from": record_id, "edit_mode": mode},
                source_id=source_id,
                source_kind=source_kind,
            )
            event = {
                "id": published["id"],
                "stream": stream,
                "type": event_type,
                "ts": published["server_time"],
                "source_id": source_id,
                "source_kind": source_kind,
                "data": {**inner, "edited_from": record_id, "edit_mode": mode},
            }
            return 200, {"entryKey": published["id"], "mailbox": stream, "record": _event_to_message(event)}
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return 400, {"error": str(error)}
    return 404, {"error": "not found"}


def handle_ws_collab_delete(path: str, query: str, *, root: Path) -> tuple[int, dict[str, Any]] | None:
    if not is_ws_collab_path(path):
        return None
    route = _normalized_route(path)
    params = parse_qs(query or "", keep_blank_values=True)
    state = _load_state(root)
    try:
        if route == "/mailbox/mailboxes":
            mailbox_id = str((params.get("id") or [""])[0] or "").strip()
            if not mailbox_id:
                raise ValueError("id is required")
            result = delete_mailbox(mailbox_id, force=True, path=root)
            state.get("mailboxes", {}).pop(mailbox_id, None)
            state.get("mailbox_config", {}).pop(mailbox_id, None)
            _save_state(root, state)
            return 200, result
        if route == "/mailbox/cursor":
            mailbox = str((params.get("mailbox") or [""])[0] or "").strip()
            agent = str((params.get("agent") or [""])[0] or "").strip()
            if not mailbox or not agent:
                raise ValueError("mailbox and agent are required")
            mailbox_store.delete_cursors(root, owners={agent}, mailboxes={mailbox})
            return 200, _mailbox_cursor_payload(root, mailbox, agent)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return 400, {"error": str(error)}
    return 404, {"error": "not found"}


def _send_ws_json(connection: socket.socket, payload: dict[str, Any]) -> None:
    connection.sendall(encode_frame(json.dumps(payload, ensure_ascii=False).encode("utf-8")))


def run_ws_collab_websocket(
    connection: socket.socket,
    *,
    root: Path,
    client_ip: str | None,
    required_token: str = "",
) -> None:
    """Serve one /ws_collab/ws WebSocket session using mailbox-backed streams."""
    connection.settimeout(0.25)
    authed = _is_loopback(client_ip) or not required_token
    streams: dict[str, str | None] = {}
    filters: dict[str, Any] = {}
    last_ping = 0.0
    if authed:
        _send_ws_json(connection, {
            "type": "auth_ok",
            "principal": {"id": "local", "role": "admin", "label": "local"},
            "capabilities": capabilities_payload(),
        })
    while True:
        now = time.time()
        if now - last_ping >= 20:
            _send_ws_json(connection, {"type": "ping", "server_time": utc_now_iso()})
            last_ping = now
        if authed:
            for stream, cursor in list(streams.items()):
                page = _read_events(root, stream, after=cursor, limit=200, filters=filters, allow_missing=True)
                for event in page["events"]:
                    _send_ws_json(connection, {"type": "event", "event": event})
                streams[stream] = page.get("next_cursor")
        try:
            opcode, payload = read_frame(connection)
        except socket.timeout:
            continue
        if opcode == 8:
            connection.sendall(encode_frame(payload, opcode=8))
            return
        if opcode == 9:
            connection.sendall(encode_frame(payload, opcode=10))
            continue
        if opcode != 1:
            _send_ws_json(connection, {"type": "error", "error": {"code": "invalid", "message": "Only JSON text frames are supported"}})
            continue
        try:
            frame = json.loads(payload.decode("utf-8"))
            if not isinstance(frame, dict):
                raise ValueError("frame must be a JSON object")
        except (ValueError, json.JSONDecodeError) as error:
            _send_ws_json(connection, {"type": "error", "error": {"code": "invalid", "message": str(error)}})
            continue
        kind = str(frame.get("type") or "")
        ref = frame.get("ack_id") or frame.get("ref")
        if kind == "auth":
            token = str(frame.get("token") or "")
            if _is_loopback(client_ip) or not required_token or token == required_token:
                authed = True
                _send_ws_json(connection, {
                    "type": "auth_ok",
                    "principal": {"id": "local", "role": "admin", "label": "local"},
                    "capabilities": capabilities_payload(),
                })
            else:
                _send_ws_json(connection, {"type": "error", "error": {"code": "auth", "message": "invalid token"}, "ref": ref})
            continue
        if not authed:
            _send_ws_json(connection, {"type": "error", "error": {"code": "auth", "message": "authenticate first"}, "ref": ref})
            continue
        if kind in {"subscribe", "resume"}:
            requested = frame.get("streams") or []
            if not isinstance(requested, list):
                _send_ws_json(connection, {"type": "error", "error": {"code": "invalid", "message": "streams must be a list"}, "ref": ref})
                continue
            cursors = frame.get("cursors") if isinstance(frame.get("cursors"), dict) else {}
            filters = frame.get("filters") if isinstance(frame.get("filters"), dict) else {}
            for stream in requested:
                stream_name = str(stream)
                streams[stream_name] = cursors.get(stream_name)
            _send_ws_json(connection, {"type": "subscribed", "streams": sorted(streams), "server_time": utc_now_iso()})
            for stream in requested:
                stream_name = str(stream)
                cursor = streams.get(stream_name)
                page_count = 0
                while page_count < 200:
                    page = _read_events(
                        root,
                        stream_name,
                        after=cursor,
                        limit=200,
                        filters=filters,
                        allow_missing=True,
                    )
                    for event in page["events"]:
                        _send_ws_json(connection, {"type": "event", "event": event})
                    cursor = page.get("next_cursor")
                    page_count += 1
                    if not page.get("has_more"):
                        break
                streams[stream_name] = cursor
                _send_ws_json(connection, {"type": "caught_up", "stream": stream_name, "cursor": cursor})
            continue
        if kind == "unsubscribe":
            requested = frame.get("streams") or []
            for stream in requested:
                streams.pop(str(stream), None)
            _send_ws_json(connection, {"type": "unsubscribed", "streams": sorted(streams)})
            continue
        if kind == "publish":
            state = _load_state(root)
            stream = str(frame.get("stream") or "")
            if not stream:
                _send_ws_json(connection, {"type": "error", "error": {"code": "invalid", "message": "stream is required"}, "ref": ref})
                continue
            ensure_mailbox(stream, path=root)
            _ensure_message_mailbox(root, stream)
            result = _publish_event(
                root,
                state,
                stream=stream,
                event_type=str(frame.get("event_type") or frame.get("event") or frame.get("kind") or CONVERSATION_EVENT_TYPE),
                data=dict(frame.get("data") or {}),
                source_id=str(frame.get("source_id") or "local"),
                source_kind=str(frame.get("source_kind") or "agent"),
                idempotency_key=str(frame.get("idempotency_key") or ""),
                correlation_id=str(frame.get("correlation_id") or ""),
            )
            _send_ws_json(connection, {"type": "ack", "ack_id": frame.get("ack_id"), **result})
            continue
        if kind == "cursor":
            action = str(frame.get("action") or "")
            stream = str(frame.get("stream") or "")
            consumer = str(frame.get("consumer") or "")
            if not stream or not consumer:
                _send_ws_json(connection, {"type": "error", "error": {"code": "invalid", "message": "stream and consumer are required"}, "ref": ref})
                continue
            if action == "get":
                result = _mailbox_cursor_payload(root, stream, consumer)
            elif action == "commit":
                token = str(frame.get("token") or "")
                mailbox_store.set_cursor(root, consumer, stream, _entry_token_to_index(token))
                result = _mailbox_cursor_payload(root, stream, consumer)
            elif action == "reposition":
                token = frame.get("token")
                if token is None and frame.get("seq") is None:
                    _send_ws_json(connection, {"type": "error", "error": {"code": "invalid", "message": "token or seq is required"}, "ref": ref})
                    continue
                if token is None:
                    token = f"entry_{max(0, int(frame.get('seq')) - 1)}"
                mailbox_store.set_cursor(root, consumer, stream, _entry_token_to_index(str(token)))
                result = _mailbox_cursor_payload(root, stream, consumer)
            elif action == "reset":
                document = mailbox_store.load_mailbox(root, stream)
                total = int(document.get("count") or 0) if "messages" in document else 0
                target = str(frame.get("to") or "start")
                mailbox_store.set_cursor(root, consumer, stream, total if target == "end" else 0)
                result = _mailbox_cursor_payload(root, stream, consumer)
            else:
                _send_ws_json(connection, {"type": "error", "error": {"code": "invalid", "message": f"unknown cursor action: {action!r}"}, "ref": ref})
                continue
            _send_ws_json(connection, {"type": "cursor_result", "action": action, "result": result, "ref": frame.get("ack_id")})
            continue
        if kind == "ping":
            _send_ws_json(connection, {"type": "pong", "server_time": utc_now_iso()})
            continue
        if kind == "pong":
            continue
        _send_ws_json(connection, {"type": "error", "error": {"code": "invalid", "message": f"unknown message type: {kind!r}"}, "ref": ref})
