"""Minimal append-only JSONL mailbox shared by local agents."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shlex
import shutil
import socket
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from .attachment_storage import copy_file
except ImportError:  # Standalone copy downloaded from /agent_mailbox.py.
    def copy_file(root: Path, source: Path, destination: Path) -> None:
        maximum_file = int(os.environ.get("MAILBOX_RELAY_MAX_ATTACHMENT_BYTES", 1024 * 1024 * 1024))
        maximum_total = int(os.environ.get(
            "MAILBOX_RELAY_MAX_ATTACHMENT_STORAGE_BYTES", 25 * 1024 * 1024 * 1024,
        ))
        size = source.stat().st_size
        if size > maximum_file:
            raise ValueError(f"attachment is {size} bytes; maximum is {maximum_file} bytes")
        attachment_root = root / "attachments"
        used = sum(path.stat().st_size for path in attachment_root.rglob("*") if path.is_file())
        if used + size > maximum_total:
            raise ValueError(
                f"attachment storage quota exceeded: {used} + {size} bytes is greater than {maximum_total} bytes"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


DEFAULT_SENDER = "console-default-agent"
MAILBOX_ENV = "AGENT_MAILBOX_DIR"
MAILBOX_URL_ENV = "AGENT_MAILBOX_URL"
MAILBOX_TOKEN_ENV = "AGENT_MAILBOX_TOKEN"
DEFAULT_MAILBOX_URL = "http://127.0.0.1:46667"
VERSION = "0.2.0"
REST_TIMEOUT = 15.0
REST_RETRIES = 0
REST_RETRY_DELAY = 1.0
REST_TOKEN: str | None = None
MAX_JSONL_ENV = "MAILBOX_RELAY_MAX_JSONL_BYTES"
DEFAULT_MAX_JSONL_BYTES = 5 * 1024 * 1024 * 1024
SERVER_AGENT_TO_AGENT_CHANNEL = "agent_to_agent"
SERVER_AGENT_TO_CHANNEL_CHANNEL = "agent_to_channel"
SERVER_AUDIT_CHANNELS = {SERVER_AGENT_TO_AGENT_CHANNEL, SERVER_AGENT_TO_CHANNEL_CHANNEL}
IRC_COMMANDS = ("ping", "list", "names", "join", "part", "topic", "nick", "whois",
                "mode", "invite", "kick", "message", "notice", "raw")
MATTERMOST_COMMANDS = ("ping", "teams", "list", "names", "threads", "join", "part",
                       "topic", "nick", "whois", "mode", "invite", "kick", "message",
                       "notice", "raw")
CHAT_COMMANDS = tuple(dict.fromkeys((*IRC_COMMANDS, *MATTERMOST_COMMANDS)))
CHAT_COMMAND_HELP = {
    "ping": "check connectivity to a configured chat platform",
    "teams": "list visible teams (where supported)",
    "list": "list channels across configured providers, or one selected with --on",
    "names": "list users in a channel; registered names, IDs, addresses, and URLs resolve",
    "threads": "list followed threads (where supported)",
    "join": "join a channel or add the connected account",
    "part": "leave a channel",
    "topic": "read or change a channel topic/header",
    "nick": "change the connected account nickname",
    "whois": "show a user; registered names and bare IDs infer their provider",
    "mode": "inspect or change channel visibility, roles, or modes",
    "invite": "add or invite a user to a channel",
    "kick": "remove a user from a channel",
    "message": "send a message to a channel or user",
    "notice": "send an informational or user-only notice",
    "raw": "perform an advanced platform-specific operation",
}
_MESSAGE_WRITE_LOCK = threading.Lock()
UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
GLOBAL_RUN_FIELDS = (
    "dir", "url", "mailbox", "config", "as", "from", "to", "subscribed", "format", "output",
    "timeout", "token", "curl", "input", "retry", "retry_delay", "quiet", "verbose", "nobuffer",
)
COMMAND_POSITIONALS = {
    "send": ("recipient", "text"),
    "receive": ("recipient",),
    "peek": ("recipient",),
    "poll": ("recipient",),
    "poll-many": ("recipients",),
    "history": ("recipients",),
    "cursor-init": ("recipients",),
    "cursors": (),
    "agents": (),
    "agent-add": ("agent_id",),
    "agent-del": ("agent_id",),
    "follow": ("recipient",),
    "unread-count": ("recipient",),
    "ack": ("recipient", "message_id"),
    "status": (),
    "check": (),
}

RELATIVE_TIME = re.compile(r"^(?P<amount>\d+(?:\.\d+)?)(?P<unit>[smhdw])$")


def default_mailbox_dir() -> Path:
    return Path.cwd() / "mailbox"


def mailbox_dir() -> Path:
    configured = os.environ.get(MAILBOX_ENV)
    return Path(configured).expanduser().resolve() if configured else default_mailbox_dir()


def _cursor_path(root: Path, recipient: str) -> Path:
    digest = hashlib.sha256(recipient.encode("utf-8")).hexdigest()[:16]
    return root / "cursors" / f"{digest}.cursor"


def _read_cursor(path: Path) -> int:
    try:
        return max(0, int(path.read_text(encoding="ascii").strip()))
    except (FileNotFoundError, ValueError):
        return 0


def _write_cursor(path: Path, offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(str(offset), encoding="ascii")
    os.replace(temporary, path)


def cursor_subscriptions(cursor: str, *, root: Path | None = None) -> list[str]:
    target = (root or mailbox_dir()) / "cursor_subscriptions.json"
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return list(dict.fromkeys(str(item) for item in document.get("cursors", {}).get(cursor, [])))


def cursor_positions(cursor: str, *, root: Path | None = None) -> list[dict[str, Any]]:
    """List every retained channel initialized for a cursor and its byte position."""
    target = root or mailbox_dir()
    return [
        {
            "recipient": recipient,
            "cursor": cursor,
            "offset": _read_cursor(_cursor_path(target, f"{recipient}:{cursor}")),
        }
        for recipient in cursor_subscriptions(cursor, root=target)
    ]


def _remember_cursor_subscription(root: Path, cursor: str, recipient: str) -> None:
    target = root / "cursor_subscriptions.json"
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        document = {"version": 1, "cursors": {}}
    members = document.setdefault("cursors", {}).setdefault(cursor, [])
    if recipient not in members:
        members.append(recipient)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def purge_agent_data(
    agent_id: str,
    *,
    presence_ids: set[str],
    agent_mailbox: str,
    remove_agent: bool,
    dry_run: bool = False,
    root: Path | None = None,
) -> dict[str, Any]:
    """Remove private agent records and cursor state while preserving global audits."""
    target = root or mailbox_dir()
    private_recipients = set(presence_ids)
    if remove_agent:
        private_recipients.update({agent_id, agent_mailbox})
    messages_path = target / "messages.jsonl"
    removed_records = 0
    retained_bytes = 0

    def should_remove(record: Any) -> bool:
        return bool(
            isinstance(record, dict)
            and (
                str(record.get("to") or "") in private_recipients
                or (
                    not remove_agent
                    and str(record.get("to") or "") == agent_mailbox
                    and str(record.get("audit_recipient") or "") in presence_ids
                )
            )
        )

    if dry_run:
        if messages_path.exists():
            with messages_path.open("rb") as source:
                for line in source:
                    try:
                        record = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    removed_records += int(should_remove(record))
        cursor_candidates = {
            _cursor_path(target, recipient) for recipient in private_recipients
        }
        try:
            subscriptions = json.loads(
                (target / "cursor_subscriptions.json").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, json.JSONDecodeError):
            subscriptions = {}
        cursors = subscriptions.get("cursors") if isinstance(subscriptions, dict) else {}
        if isinstance(cursors, dict):
            for cursor, recipients in cursors.items():
                if not isinstance(recipients, list):
                    continue
                for recipient in recipients:
                    if str(recipient) in private_recipients or (remove_agent and cursor == agent_id):
                        cursor_candidates.add(_cursor_path(target, f"{recipient}:{cursor}"))
        return {
            "purged": False,
            "dry_run": True,
            "would_purge_channels": sorted(private_recipients),
            "would_purge_records": removed_records,
            "would_purge_cursor_files": len([path for path in cursor_candidates if path.exists()]),
        }

    with _MESSAGE_WRITE_LOCK:
        cursor_offsets: dict[Path, int] = {
            path: _read_cursor(path) for path in (target / "cursors").glob("*.cursor")
        } if (target / "cursors").is_dir() else {}
        mapped_offsets: dict[Path, int] = {}
        if messages_path.exists():
            temporary = messages_path.with_name(
                f".{messages_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            old_position = 0
            with messages_path.open("rb") as source, temporary.open("wb") as destination:
                for line in source:
                    line_start = old_position
                    line_end = line_start + len(line)
                    try:
                        record = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        record = None
                    remove = should_remove(record)
                    for path, offset in cursor_offsets.items():
                        if path in mapped_offsets or offset > line_end:
                            continue
                        if offset <= line_start or remove:
                            mapped_offsets[path] = retained_bytes
                        else:
                            mapped_offsets[path] = retained_bytes + offset - line_start
                    if remove:
                        removed_records += 1
                    else:
                        destination.write(line)
                        retained_bytes += len(line)
                    old_position = line_end
            os.replace(temporary, messages_path)
            for path, offset in cursor_offsets.items():
                _write_cursor(path, mapped_offsets.get(path, retained_bytes))

        subscriptions_path = target / "cursor_subscriptions.json"
        try:
            subscriptions = json.loads(subscriptions_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            subscriptions = None
        removed_cursor_files: set[Path] = set()
        if isinstance(subscriptions, dict):
            cursors = subscriptions.get("cursors")
            if isinstance(cursors, dict):
                for cursor, recipients in list(cursors.items()):
                    if not isinstance(recipients, list):
                        continue
                    if remove_agent and cursor == agent_id:
                        for recipient in recipients:
                            removed_cursor_files.add(_cursor_path(target, f"{recipient}:{cursor}"))
                        del cursors[cursor]
                        continue
                    retained = [item for item in recipients if str(item) not in private_recipients]
                    for recipient in set(map(str, recipients)) - set(map(str, retained)):
                        removed_cursor_files.add(_cursor_path(target, f"{recipient}:{cursor}"))
                    cursors[cursor] = retained
                subscriptions_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = subscriptions_path.with_suffix(
                    f".{os.getpid()}.{uuid.uuid4().hex}.tmp"
                )
                temporary.write_text(
                    json.dumps(subscriptions, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, subscriptions_path)
        for recipient in private_recipients:
            removed_cursor_files.add(_cursor_path(target, recipient))
        existing_cursor_files = {path for path in removed_cursor_files if path.exists()}
        for path in existing_cursor_files:
            path.unlink(missing_ok=True)

    return {
        "purged": True,
        "purged_channels": sorted(private_recipients),
        "purged_records": removed_records,
        "purged_cursor_files": len(existing_cursor_files),
    }


def _time_boundary(value: str, *, now: datetime | None = None) -> str:
    """Resolve a caller-provided timestamp or relative duration to UTC ISO text."""
    match = RELATIVE_TIME.fullmatch(value.strip().lower())
    if not match:
        return value
    seconds_per_unit = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    seconds = float(match.group("amount")) * seconds_per_unit[match.group("unit")]
    boundary = (now or datetime.now(timezone.utc)) - timedelta(seconds=seconds)
    return boundary.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def initialize_cursor(
    recipient: str,
    *,
    cursor: str,
    start: str = "now",
    root: Path | None = None,
) -> dict[str, Any]:
    """Position one durable cursor without changing or deleting mailbox history."""
    if not cursor.strip():
        raise ValueError("cursor name is required")
    target = root or mailbox_dir()
    messages_path = target / "messages.jsonl"
    cursor_path = _cursor_path(target, f"{recipient}:{cursor}")
    if cursor_path.exists():
        raise ValueError(f"cursor {cursor!r} is already initialized for {recipient!r}")
    normalized = start.strip().lower()
    if normalized in {"beginning", "start"}:
        offset = 0
    else:
        try:
            file_size = messages_path.stat().st_size
        except FileNotFoundError:
            file_size = 0
        if normalized == "now":
            offset = file_size
        else:
            boundary = _time_boundary(start)
            offset = file_size
            try:
                with messages_path.open("rb") as stream:
                    while True:
                        line_start = stream.tell()
                        line = stream.readline()
                        if not line:
                            break
                        try:
                            record = json.loads(line.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if (isinstance(record, dict) and record.get("to") == recipient
                                and str(record.get("timestamp", "")) >= boundary):
                            offset = line_start
                            break
            except FileNotFoundError:
                pass
    _write_cursor(cursor_path, offset)
    _remember_cursor_subscription(target, cursor, recipient)
    return {"recipient": recipient, "cursor": cursor, "start": start, "offset": offset}


def _safe_attachment_name(path: Path, used_names: set[str]) -> str:
    name = UNSAFE_FILENAME.sub("_", path.name).strip(" .") or "attachment"
    candidate = name
    suffix = 2
    while candidate.casefold() in used_names:
        candidate = f"{Path(name).stem}-{suffix}{Path(name).suffix}"
        suffix += 1
    used_names.add(candidate.casefold())
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_attachments(target: Path, message_id: str, paths: list[Path]) -> list[dict[str, Any]]:
    if not paths:
        return []
    destination_dir = target / "attachments" / message_id
    destination_dir.mkdir(parents=True, exist_ok=False)
    attachments: list[dict[str, Any]] = []
    used_names: set[str] = set()
    try:
        for source in paths:
            resolved = source.expanduser().resolve(strict=True)
            if not resolved.is_file():
                raise ValueError(f"attachment is not a file: {source}")
            name = _safe_attachment_name(resolved, used_names)
            destination = destination_dir / name
            copy_file(target, resolved, destination)
            mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            attachments.append(
                {
                    "path": str(destination),
                    "name": name,
                    "mime_type": mime_type,
                    "size": destination.stat().st_size,
                    "sha256": _sha256(destination),
                }
            )
    except Exception:
        shutil.rmtree(destination_dir, ignore_errors=True)
        raise
    return attachments


def send(
    recipient: str,
    text: str,
    *,
    sender: str = DEFAULT_SENDER,
    message_type: str = "message",
    metadata: dict[str, Any] | None = None,
    extra_fields: dict[str, Any] | None = None,
    attachments: list[Path] | None = None,
    channel_id: str | None = None,
    channel_type: str | None = None,
    source_id: str | None = None,
    thread_id: str | None = None,
    root_id: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    target = root or mailbox_dir()
    target.mkdir(parents=True, exist_ok=True)
    message_id = str(uuid.uuid4())
    record: dict[str, Any] = {
        "id": message_id,
        "dedupe_id": message_id,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "from": sender,
        "to": recipient,
        "type": message_type,
        "text": text,
    }
    if metadata is not None:
        record["metadata"] = metadata
    if extra_fields:
        record.update(extra_fields)
    routing_context = {
        "channel_id": channel_id,
        "channel_type": channel_type,
        "source_id": source_id,
        "thread_id": thread_id,
        "root_id": root_id,
    }
    record.update({key: value for key, value in routing_context.items() if value})
    copied_attachments = _copy_attachments(target, message_id, attachments or [])
    if copied_attachments:
        record["attachments"] = copied_attachments
    records = [record]
    if recipient not in SERVER_AUDIT_CHANNELS:
        audit_recipients = [SERVER_AGENT_TO_CHANNEL_CHANNEL]
        resolved_agent_id = ""
        addressed_presence_id = ""
        try:
            try:
                from .connector_registry import agent_mailbox_address, load_agents
            except ImportError:
                from mailbox_channels.connector_registry import agent_mailbox_address, load_agents
            agents = load_agents()
            if recipient in {item["agent_id"] for item in agents}:
                resolved_agent_id = recipient
            else:
                resolved_agent_id = next((
                    str(item["agent_id"])
                    for item in agents
                    if any(presence["presence_id"] == recipient for presence in item["presences"])
                ), "")
                addressed_presence_id = recipient if resolved_agent_id else ""
            if resolved_agent_id:
                audit_recipients.append(SERVER_AGENT_TO_AGENT_CHANNEL)
                if addressed_presence_id:
                    audit_recipients.append(agent_mailbox_address(resolved_agent_id))
        except (ImportError, OSError, ValueError, json.JSONDecodeError):
            pass
        for audit_recipient in audit_recipients:
            audit_record = {
                **record,
                "id": str(uuid.uuid4()),
                "to": audit_recipient,
                "audit_of": record["id"],
                "dedupe_id": record["dedupe_id"],
                "audit_recipient": recipient,
            }
            if resolved_agent_id:
                audit_record["resolved_agent_id"] = resolved_agent_id
            if addressed_presence_id:
                audit_record["addressed_presence_id"] = addressed_presence_id
            records.append(audit_record)
    payload = b"".join(
        (json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        for item in records
    )
    messages_path = target / "messages.jsonl"
    maximum = int(os.environ.get(MAX_JSONL_ENV, DEFAULT_MAX_JSONL_BYTES))
    if maximum < 1:
        raise ValueError("JSONL mailbox size limit must be positive")
    with _MESSAGE_WRITE_LOCK:
        current = messages_path.stat().st_size if messages_path.exists() else 0
        if current + len(payload) > maximum:
            if copied_attachments:
                shutil.rmtree(target / "attachments" / message_id, ignore_errors=True)
            raise ValueError(
                f"JSONL mailbox quota exceeded: {current} + {len(payload)} bytes is greater than {maximum} bytes"
            )
        fd = os.open(messages_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    return record


def receive(
    recipient: str,
    *,
    root: Path | None = None,
    advance: bool = True,
    cursor: str | None = None,
) -> list[dict[str, Any]]:
    target = root or mailbox_dir()
    messages_path = target / "messages.jsonl"
    cursor_path = _cursor_path(target, f"{recipient}:{cursor}" if cursor else recipient)
    start = _read_cursor(cursor_path)
    try:
        file_size = messages_path.stat().st_size
    except FileNotFoundError:
        return []
    if start > file_size:
        start = 0

    found: list[dict[str, Any]] = []
    committed_offset = start
    with messages_path.open("rb") as stream:
        stream.seek(start)
        while True:
            line_start = stream.tell()
            line = stream.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                committed_offset = line_start
                break
            committed_offset = stream.tell()
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and record.get("to") == recipient:
                found.append(record)

    if advance and (committed_offset != start or not cursor_path.exists()):
        _write_cursor(cursor_path, committed_offset)
    return found


def peek(recipient: str, *, root: Path | None = None, cursor: str | None = None) -> list[dict[str, Any]]:
    """Return unread messages without advancing the recipient cursor."""
    return receive(recipient, root=root, advance=False, cursor=cursor)


def acknowledge(recipient: str, message_id: str, *, root: Path | None = None,
                cursor: str | None = None) -> bool:
    target = root or mailbox_dir()
    messages_path = target / "messages.jsonl"
    try:
        with messages_path.open("rb") as stream:
            while line := stream.readline():
                try:
                    record = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if record.get("to") == recipient and record.get("id") == message_id:
                    key = f"{recipient}:{cursor}" if cursor else recipient
                    _write_cursor(_cursor_path(target, key), stream.tell())
                    return True
    except FileNotFoundError:
        pass
    return False


def _port_is_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def poll(
    recipient: str,
    *,
    interval_seconds: float = 30.0,
    max_checks: int = 10,
    required_ports: tuple[int, ...] = (),
    root: Path | None = None,
    advance: bool = True,
    cursor: str | None = None,
    sleep: Any = time.sleep,
    port_probe: Any = _port_is_listening,
) -> tuple[list[dict[str, Any]], list[int]]:
    if interval_seconds < 0:
        raise ValueError("interval_seconds must be non-negative")
    if max_checks < 1:
        raise ValueError("max_checks must be at least 1")
    for check in range(max_checks):
        if check:
            sleep(interval_seconds)
        found = receive(recipient, root=root, advance=advance, cursor=cursor)
        if found:
            return found, []
        missing = [port for port in required_ports if not port_probe(port)]
        if missing:
            return [], missing
    return [], []


def status(*, root: Path | None = None) -> dict[str, Any]:
    target = root or mailbox_dir()
    path = target / "messages.jsonl"
    agents: list[dict[str, Any]] = []
    connectors: list[dict[str, Any]] = []
    channels: list[dict[str, Any]] = []
    relays: list[dict[str, Any]] = []
    try:
        from .retained_relay import load_relays
        from .connector_registry import load_agents, load_connectors
        from .subscriptions import available_sources

        agents = [
            {
                **agent,
                "cursor": agent["agent_id"],
                "subscriptions": [
                    {
                        "channel": position["recipient"],
                        "cursor": position["cursor"],
                        "offset": position["offset"],
                    }
                    for position in cursor_positions(agent["agent_id"], root=target)
                ],
            }
            for agent in load_agents()
        ]
        configured_connectors = [item for item in load_connectors() if item["enabled"]]
        connectors = []
        for connector in configured_connectors:
            connectors.append({
                "id": connector["id"],
                "kind": "connector",
                "adapter": connector["adapter"],
                "instance": str(connector.get("instance") or ""),
                "can_listen": connector["direction"] in {"inbound", "bidirectional"},
                "can_send": connector["direction"] in {"outbound", "bidirectional"},
                "channel_ids": connector["channel_ids"],
            })
        channels = available_sources()
        relays = [item for item in load_relays() if item["enabled"]]
    except ImportError:
        # A downloaded /agent_mailbox.py can run without the server package.
        pass
    return {
        "directory": str(target),
        "messages_file": str(path),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "default_sender": DEFAULT_SENDER,
        "agents": agents,
        "connectors": connectors,
        "channels": channels,
        "relays": relays,
    }


def _rest_request(method: str, path: str, payload: dict[str, Any] | None = None, *, base_url: str | None = None) -> Any:
    url = (base_url or os.environ.get(MAILBOX_URL_ENV) or DEFAULT_MAILBOX_URL).rstrip("/") + path
    encoded = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=encoded, method=method)
    token = REST_TOKEN or os.environ.get(MAILBOX_TOKEN_ENV)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if encoded is not None:
        request.add_header("Content-Type", "application/json")
    for attempt in range(REST_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=REST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError):
            if attempt >= REST_RETRIES:
                raise
            time.sleep(REST_RETRY_DELAY)
    raise RuntimeError("REST request exhausted retries")


def send_rest(recipient: str, text: str, *, sender: str = DEFAULT_SENDER, message_type: str = "message",
              attachments: list[Path] | None = None, channel_id: str | None = None,
              channel_type: str | None = None, source_id: str | None = None,
              thread_id: str | None = None, root_id: str | None = None,
              extra_fields: dict[str, Any] | None = None,
              base_url: str | None = None) -> dict[str, Any]:
    payload = {"to": recipient, "text": text, "from": sender, "type": message_type,
               "attachments": [str(path.expanduser().resolve()) for path in (attachments or [])],
               "channel_id": channel_id, "channel_type": channel_type, "source_id": source_id,
               "thread_id": thread_id, "root_id": root_id}
    payload.update(extra_fields or {})
    return dict(_rest_request("POST", "/v1/messages", payload, base_url=base_url)["message"])


def receive_rest(
    recipient: str,
    *,
    base_url: str | None = None,
    advance: bool = True,
    cursor: str | None = None,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"recipient": recipient, "advance": str(advance).lower(),
                                    "cursor": cursor or ""})
    return list(_rest_request("GET", f"/v1/messages?{query}", base_url=base_url)["messages"])


def peek_rest(recipient: str, *, base_url: str | None = None,
              cursor: str | None = None) -> list[dict[str, Any]]:
    return receive_rest(recipient, base_url=base_url, advance=False, cursor=cursor)


def acknowledge_rest(recipient: str, message_id: str, *, base_url: str | None = None,
                     cursor: str | None = None) -> bool:
    result = _rest_request("POST", "/v1/ack", {"recipient": recipient, "message_id": message_id,
                                                "cursor": cursor}, base_url=base_url)
    return bool(result.get("acknowledged"))


def initialize_cursor_rest(recipient: str, *, cursor: str, start: str = "now",
                           base_url: str | None = None) -> dict[str, Any]:
    return dict(_rest_request("POST", "/v1/cursors", {
        "recipient": recipient, "cursor": cursor, "start": start,
    }, base_url=base_url))


def register_agent_rest(agent_id: str, *, presence_id: str = "",
                        dry_run: bool = False,
                        base_url: str | None = None) -> dict[str, Any]:
    return dict(_rest_request("POST", "/v1/agents", {
        "agent_id": agent_id, "presence_id": presence_id,
        "dry_run": dry_run,
    }, base_url=base_url))


def unregister_agent_rest(agent_id: str, *, presence_id: str = "", purge: bool = False,
                          dry_run: bool = False,
                          base_url: str | None = None) -> dict[str, Any]:
    return dict(_rest_request("POST", "/v1/agents", {
        "action": "delete", "agent_id": agent_id, "presence_id": presence_id,
        "purge": purge, "dry_run": dry_run,
    }, base_url=base_url))


def status_rest(*, base_url: str | None = None) -> dict[str, Any]:
    return dict(_rest_request("GET", "/v1/status", base_url=base_url))


def default_config_path() -> Path:
    return Path.cwd() / "config" / "mailboxes.json"


def named_mailbox(name: str, path: Path | None = None) -> tuple[Path | None, str | None]:
    config_path = (path or default_config_path()).expanduser().resolve()
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
        entry = document["mailboxes"][name]
    except FileNotFoundError as error:
        raise ValueError(f"mailbox config not found: {config_path}") from error
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"named mailbox not found or invalid: {name}") from error
    if isinstance(entry, str):
        entry = {"url": entry} if entry.startswith(("http://", "https://")) else {"dir": entry}
    if not isinstance(entry, dict) or bool(entry.get("dir")) == bool(entry.get("url")):
        raise ValueError(f"mailbox {name!r} must define exactly one of 'dir' or 'url'")
    if entry.get("url"):
        return None, str(entry["url"])
    directory = Path(str(entry["dir"])).expanduser()
    if not directory.is_absolute():
        directory = config_path.parent / directory
    return directory.resolve(), None


def _where_filters(values: list[str]) -> dict[str, str]:
    filters: dict[str, str] = {}
    for value in values:
        key, separator, expected = value.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"invalid --where filter: {value!r}; expected FIELD=VALUE")
        filters[key.strip()] = expected
    return filters


def _select_records(records: list[dict[str, Any]], *, since: str | None = None,
                    until: str | None = None, limit: int | None = None,
                    where: list[str] | None = None) -> list[dict[str, Any]]:
    filters = _where_filters(where or [])
    since = _time_boundary(since) if since else None
    until = _time_boundary(until) if until else None
    selected: list[dict[str, Any]] = []
    after_id = since is None
    for record in records:
        if since and not after_id:
            if record.get("id") == since:
                after_id = True
                continue
            if str(record.get("timestamp", "")) <= since:
                continue
            after_id = True
        if until and str(record.get("timestamp", "")) > until:
            continue
        if all(str(record.get(key, "")) == expected for key, expected in filters.items()):
            selected.append(record)
            if limit is not None and len(selected) >= limit:
                break
    return selected


def _render_text_record(item: dict[str, Any]) -> str:
    line = f"[{item.get('timestamp', '')}] {item.get('from', '')}: {item.get('text', '')}"
    if item.get("type") != "chat_server_status":
        return line
    context = item.get("service_context") if isinstance(item.get("service_context"), dict) else {}
    diagnostic = item.get("diagnostic") if isinstance(item.get("diagnostic"), dict) else {}
    details = [
        f"adapter={item.get('adapter') or context.get('adapter') or item.get('channel_type', '')}",
        f"state={item.get('connection_state', '')}",
    ]
    if context.get("connector_ids"):
        details.append(f"connectors={','.join(map(str, context['connector_ids']))}")
    if context.get("channel_ids"):
        details.append(f"channels={','.join(map(str, context['channel_ids']))}")
    if diagnostic:
        details.append(f"operation={diagnostic.get('operation', '')}")
        details.append(f"error={diagnostic.get('error_type', '')}: {diagnostic.get('error_message', '')}")
        details.append(f"will_retry={str(bool(diagnostic.get('will_retry'))).lower()}")
    return f"{line}\n  {'; '.join(details)}"


def _render_records(records: list[dict[str, Any]], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(records, ensure_ascii=False, indent=2)
    if output_format == "text":
        return "\n".join(_render_text_record(item) for item in records)
    return "\n".join(json.dumps(item, ensure_ascii=False) for item in records)


def _emit(text: str, *, output: Path | None, quiet: bool, append: bool = False) -> None:
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a" if append else "w", encoding="utf-8") as stream:
            if text:
                stream.write(text + "\n")
    if text and not quiet:
        print(text, flush=True)


def _add_read_options(parser: argparse.ArgumentParser, *, waiting: bool = True) -> None:
    parser.add_argument("--cursor", help="independent cursor name")
    parser.add_argument("--since", help="only return records after a timestamp or message ID")
    parser.add_argument("--until", help="only return records through a timestamp or relative boundary")
    parser.add_argument("--limit", type=int, help="maximum records to return")
    parser.add_argument("--where", action="append", default=[], metavar="FIELD=VALUE",
                        help="filter by an envelope field; repeatable")
    parser.add_argument("--no-advance", action="store_true", help="do not advance the cursor")
    if waiting:
        parser.add_argument("--wait", type=float, default=0.0, help="wait up to this many seconds for mail")


def curl_command(args: argparse.Namespace, base_url: str, *, token: bool) -> str:
    base = base_url.rstrip("/")
    method = "GET"
    payload: dict[str, Any] | None = None
    if args.command in {"status", "check"}:
        url = f"{base}/v1/status"
    elif args.command == "send":
        url = f"{base}/v1/messages"
        method = "POST"
        payload = {
            "to": args.recipient, "text": args.text,
            "from": args.sender or args.global_sender or DEFAULT_SENDER,
            "type": args.message_type, "attachments": [str(path.resolve()) for path in args.attach],
            "channel_id": args.channel_id, "channel_type": args.channel_type,
            "source_id": args.source_id, "thread_id": args.thread_id, "root_id": args.root_id,
        }
    elif args.command == "ack" or (args.command == "receive" and args.ack):
        url = f"{base}/v1/ack"
        method = "POST"
        payload = {"recipient": args.recipient,
                   "message_id": args.message_id if args.command == "ack" else args.ack,
                   "cursor": args.cursor}
    elif args.command == "agent-add":
        url = f"{base}/v1/agents"
        method = "POST"
        payload = {
            "agent_id": args.agent_id, "presence_id": args.presence_id,
            "dry_run": args.dry_run,
        }
    elif args.command == "agent-del":
        url = f"{base}/v1/agents"
        method = "POST"
        payload = {
            "action": "delete", "agent_id": args.agent_id, "presence_id": args.presence_id,
            "purge": args.purge, "dry_run": args.dry_run,
        }
    else:
        advance = args.command not in {"peek", "unread-count"} and not getattr(args, "no_advance", False)
        query = urllib.parse.urlencode({"recipient": args.recipient, "advance": str(advance).lower(),
                                        "cursor": getattr(args, "cursor", None) or ""})
        url = f"{base}/v1/messages?{query}"
    parts = ["curl", "-sS", "-X", method]
    if token:
        parts.extend(["-H", "Authorization: Bearer <REDACTED_TOKEN>"])
    if payload is not None:
        parts.extend(["-H", "Content-Type: application/json", "--data", json.dumps(payload, ensure_ascii=False)])
    parts.append(url)
    return " ".join(shlex.quote(part) for part in parts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailbox-client",
        description=__doc__,
        epilog=(
            "Chat commands select a platform from a TYPE/INSTANCE/ID address or --on "
            "TYPE/INSTANCE; IRC is the default when neither is supplied.\n"
            "Mattermost mode supports public/private and +o/-o channel-operator roles; "
            "notice supports tagged channel posts and --user ephemeral posts."
            + "\n\nUse 'mailbox-client COMMAND --help' for command-specific options. "
              "Global options may appear before or after COMMAND; -- stops option processing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--run", type=Path, metavar="COMMAND.json",
                        help="execute the entire command from a JSON document")
    parser.add_argument("--batch", type=Path, metavar="COMMANDS.txt",
                        help="run each nonblank line as one mailbox-client command")
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--dir", type=Path, help="use this JSONL mailbox directory")
    transport.add_argument("--url", help=f"use REST instead of JSONL (default service: {DEFAULT_MAILBOX_URL})")
    transport.add_argument("--mailbox", help="use a named mailbox from the mailbox config")
    parser.add_argument("--config", type=Path, help="mailbox configuration file")
    parser.add_argument("--as", dest="local_identity",
                        help="local mailbox identity performing the operation")
    parser.add_argument("--from", dest="global_sender",
                        help="source endpoint (for example mm/chat.snt/ID), or legacy sending identity")
    parser.add_argument("--to", dest="global_recipient",
                        help="mailbox identity or external endpoint (for example mm/chat.snt/ID)")
    parser.add_argument("--subscribed", metavar="CHANNELS",
                        help="comma-separated channel endpoints that --to/--as must be subscribed to")
    parser.add_argument("--format", choices=("jsonl", "json", "text"), default="jsonl",
                        help="output rendering format (default: jsonl)")
    parser.add_argument("--output", type=Path, help="write rendered output to this file instead of stdout")
    parser.add_argument("--timeout", type=float, default=15.0, help="REST request timeout in seconds")
    parser.add_argument("--token", help=f"REST Bearer token (or {MAILBOX_TOKEN_ENV})")
    parser.add_argument("--curl", action="store_true",
                        help="print the equivalent REST curl command without executing it")
    parser.add_argument(
        "--input", dest="input_file", type=Path,
        help="read send message text from a UTF-8 file",
    )
    parser.add_argument("--retry", type=int, default=0, help="number of REST retries")
    parser.add_argument("--retry-delay", type=float, default=1.0, help="delay between REST retries")
    parser.add_argument("--quiet", action="store_true", help="suppress normal rendered output")
    parser.add_argument("--verbose", action="store_true", help="report selected transport and target")
    parser.add_argument("--nobuffer", action="store_true",
                        help="write stdout and stderr immediately without block buffering")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    send_parser = commands.add_parser(
        "send", help="append a message", description="Append one durable mailbox message.",
        epilog=("Example: mailbox-client send --as symbolic-workbench-codex --to "
                "omegaclaw-core-codex 'Task complete'; mailbox-client send --as "
                "symbolic-workbench-codex --to mm/chat.snt/CHANNEL_OR_PERSON_ID 'Hello'"),
    )
    send_parser.add_argument("recipient", nargs="?", help="destination identity; alternatively use --to")
    send_parser.add_argument("text", nargs="?", help="message text; alternatively use global --input PATH")
    send_parser.add_argument("--sender", help="sender identity overriding global --from")
    send_parser.add_argument("--type", dest="message_type", default="message",
                             help="mailbox message type (default: message)")
    send_parser.add_argument("--channel-id", help="external channel or conversation identifier")
    send_parser.add_argument("--channel-type", help="external adapter type, such as telegram or slack")
    send_parser.add_argument("--source-id", help="source platform message/event identifier")
    send_parser.add_argument("--thread-id", help="source or destination thread identifier")
    send_parser.add_argument("--root-id", help="root message identifier for threaded transports")
    send_parser.add_argument(
        "--attach",
        action="append",
        default=[],
        type=Path,
        help="copy a file into this message (repeatable)",
    )
    receive_parser = commands.add_parser(
        "receive", help="consume unread messages",
        description="Read unread messages and advance the selected durable cursor.",
        epilog="Example: mailbox-client receive --to worker-1 --limit 10 --where type=result",
    )
    receive_parser.add_argument("recipient", nargs="?",
                                help="mailbox identity whose unread messages are consumed; alternatively use --to")
    _add_read_options(receive_parser)
    receive_parser.add_argument("--ack", metavar="MESSAGE_ID", help="acknowledge through this message ID")
    peek_parser = commands.add_parser(
        "peek", help="show unread messages without advancing the cursor",
        description="Inspect unread messages without changing any cursor.",
        epilog="Example: mailbox-client peek --to worker-1 --cursor audit --limit 20",
    )
    peek_parser.add_argument("recipient", nargs="?", help="mailbox identity to inspect; alternatively use --to")
    _add_read_options(peek_parser, waiting=False)
    poll_parser = commands.add_parser(
        "poll", help="poll for unread messages",
        description="Check repeatedly until mail arrives or the bounded check count is exhausted.",
        epilog=("Example: mailbox-client poll --to symbolic-workbench-codex; mailbox-client poll "
                "--as symbolic-workbench-codex --from mm/chat.snt/CHANNEL_OR_PERSON_ID"),
    )
    poll_parser.add_argument("recipient", nargs="?", help="mailbox identity to poll; alternatively use --to")
    poll_parser.add_argument("--interval", type=float, default=30.0,
                             help="seconds between checks (default: 30)")
    poll_parser.add_argument("--checks", type=int, default=10,
                             help="maximum checks before exiting (default: 10)")
    poll_parser.add_argument("--require-port", action="append", default=[], type=int,
                             help="fail when this local TCP port stops listening; repeatable")
    poll_parser.add_argument("--subscriptions", action="store_true", dest="poll_subscriptions",
                             help="poll every channel initialized for this cursor/agent identity")
    _add_read_options(poll_parser, waiting=False)
    poll_many_parser = commands.add_parser(
        "poll-many", help="poll several retained channels in one command",
        description="Poll multiple recipients using one independently saved cursor name.",
        epilog=("Example: mailbox-client poll-many mm/example.test/channel-a "
                "mm/example.test/channel-b --cursor workbench-codex"),
    )
    poll_many_parser.add_argument("recipients", nargs="+", help="channel addresses to poll")
    poll_many_parser.add_argument("--interval", type=float, default=30.0,
                                  help="seconds between checks (default: 30)")
    poll_many_parser.add_argument("--checks", type=int, default=10,
                                  help="maximum checks before exiting (default: 10)")
    poll_many_parser.add_argument("--require-port", action="append", default=[], type=int,
                                  help="fail when this local TCP port stops listening; repeatable")
    _add_read_options(poll_many_parser, waiting=False)
    history_parser = commands.add_parser(
        "history", help="search retained history across channels",
        description="Read retained records independently of every live cursor.",
        epilog=("Example: mailbox-client history mm/example.test/channel-a "
                "mm/example.test/channel-b --since 7d"),
    )
    history_parser.add_argument("recipients", nargs="+", help="channel addresses to search")
    history_parser.add_argument("--since", help="records after a timestamp, message ID, or duration such as 7d")
    history_parser.add_argument("--until", help="records through a timestamp or relative duration")
    history_parser.add_argument("--limit", type=int, help="maximum merged records to return")
    history_parser.add_argument("--where", action="append", default=[], metavar="FIELD=VALUE",
                                help="filter by an envelope field; repeatable")
    cursor_parser = commands.add_parser(
        "cursor-init", help="set a new cursor's initial history position",
        description="Create a cursor once at the beginning, now, a timestamp, or a relative time.",
        epilog=("Example: mailbox-client cursor-init mm/example.test/channel-a "
                "mm/example.test/channel-b --cursor workbench-codex --start now"),
    )
    cursor_parser.add_argument("recipients", nargs="+", help="channel addresses sharing this cursor name")
    cursor_parser.add_argument("--cursor", required=True, help="new independent cursor name")
    cursor_parser.add_argument("--start", default="now",
                               help="beginning, now, UTC timestamp, or relative duration such as 7d")
    cursors_parser = commands.add_parser(
        "cursors", help="list channels and positions initialized for one cursor",
        description="List every retained channel initialized for one cursor or agent identity.",
        epilog=("Example: mailbox-client --url http://127.0.0.1:46667 "
                "cursors --cursor symbolic-workbench-codex"),
    )
    cursors_parser.add_argument("--cursor", required=True, help="cursor or agent identity to inspect")
    commands.add_parser(
        "agents", help="list registered messageable agents",
        description="List registered agent identities and their available presences.",
        epilog="Example: mailbox-client --url http://127.0.0.1:46667 agents",
    )
    agent_add_parser = commands.add_parser(
        "agent-add", help="register an agent and optional presence",
        description=("Create a stable messageable agent identity and optionally attach one "
                     "globally unique presence."),
        epilog=("Example: mailbox-client --url http://127.0.0.1:46667 agent-add "
                "review-agent --presence review-agent-app"),
    )
    agent_add_parser.add_argument("agent_id", help="stable agent identity to create or extend")
    agent_add_parser.add_argument("--presence", dest="presence_id",
                                  help="globally unique presence identity to attach")
    agent_add_parser.add_argument(
        "--dry-run", action="store_true",
        help="show the agent, presence, and initial channels that would be added",
    )
    agent_del_parser = commands.add_parser(
        "agent-del", help="remove an agent or one of its presences",
        description=("Remove one presence, or remove the agent and all presences when "
                     "--presence is omitted. Referenced identities are protected."),
        epilog=("Example: mailbox-client --url http://127.0.0.1:46667 agent-del "
                "review-agent --presence review-agent-codex"),
    )
    agent_del_parser.add_argument("agent_id", help="stable agent identity to remove or modify")
    agent_del_parser.add_argument("--presence", dest="presence_id",
                                  help="remove only this presence instead of the whole agent")
    agent_del_parser.add_argument(
        "--purge", action="store_true",
        help="also erase private channel records and cursor state (history is retained by default)",
    )
    agent_del_parser.add_argument(
        "--dry-run", action="store_true",
        help="preview --purge without deleting the agent, records, or cursor state",
    )
    commands.add_parser(
        "connectors", help="list configured external-system connectors",
        description="List connectors and their inbound/outbound capabilities.",
        epilog="Example: mailbox-client connectors",
    )
    connector_add = commands.add_parser(
        "connector-add", help="register an external-system connector",
        description="Register one connector with optional adapter-specific settings.",
        epilog="Example: mailbox-client connector-add mm-primary mattermost --instance chat.example",
    )
    connector_add.add_argument("connector_id", help="stable connector identity")
    connector_add.add_argument("kind", help="adapter kind, such as mattermost, discord, or irc")
    connector_add.add_argument("--direction", choices=["inbound", "outbound", "bidirectional"],
                               default="bidirectional", help="connector traffic capability")
    connector_add.add_argument("--instance", default="", help="external server or account instance")
    connector_add.add_argument("--channel", action="append", default=[], dest="channel_ids",
                               help="monitored channel identifier; repeatable")
    connector_add.add_argument("--config", default="{}", help="adapter-specific JSON object")
    connector_add.add_argument("--set", action="append", default=[], dest="config_values",
                               help="adapter setting as KEY=VALUE; repeatable and .cmd-safe")
    connector_add.add_argument("--dry-run", action="store_true", help="validate without saving")
    connector_del = commands.add_parser(
        "connector-del", help="delete an external-system connector",
        description="Delete one connector from the canonical registry.",
        epilog="Example: mailbox-client connector-del mm-primary",
    )
    connector_del.add_argument("connector_id", help="connector identity to delete")
    connector_del.add_argument("--dry-run", action="store_true", help="preview without deleting")

    commands.add_parser(
        "channels", help="list configured and discovered retained channels",
        description="List every retained channel available to agents and relays.",
        epilog="Example: mailbox-client channels",
    )
    channel_add = commands.add_parser(
        "channel-add", help="register a retained channel",
        description="Add one retained channel with optional metadata.",
        epilog="Example: mailbox-client channel-add team_updates --set title=Updates",
    )
    channel_add.add_argument("channel_id", help="stable retained channel address")
    channel_add.add_argument("--metadata", default="{}", help="channel metadata JSON object")
    channel_add.add_argument("--alias", action="append", default=[], dest="aliases",
                             help="alternate channel address or display name; repeatable")
    channel_add.add_argument("--set", action="append", default=[], dest="metadata_values",
                             help="metadata as KEY=VALUE; repeatable and .cmd-safe")
    channel_del = commands.add_parser(
        "channel-del", help="delete a configured retained channel",
        description="Delete one configured channel while protecting active subscribers.",
        epilog="Example: mailbox-client channel-del team_updates",
    )
    channel_del.add_argument("channel_id", help="configured channel address to delete")
    channel_del.add_argument("--force", action="store_true",
                             help="remove the channel even when it has subscribers")

    commands.add_parser(
        "relays", help="list cursor-driven channel relays",
        description="List cursor-driven relays from retained channels to external destinations.",
        epilog="Example: mailbox-client relays",
    )
    relay_add = commands.add_parser(
        "relay-add", help="register a cursor-driven channel relay",
        description="Add a relay with its own durable source-channel cursor.",
        epilog="Example: mailbox-client relay-add team_updates mm/chat.example/channel-id",
    )
    relay_add.add_argument("source_channel", help="retained channel consumed by the relay")
    relay_add.add_argument("destination", help="external TYPE/INSTANCE/IDENTIFIER address")
    relay_add.add_argument("--id", dest="relay_id", default="", help="stable relay identity")
    relay_add.add_argument("--start", default="now", help="initial cursor position")
    relay_add.add_argument("--dry-run", action="store_true", help="validate without saving")
    relay_del = commands.add_parser(
        "relay-del", help="delete a relay and retain its cursor",
        description="Delete a relay configuration without erasing its cursor history.",
        epilog="Example: mailbox-client relay-del updates-to-mm",
    )
    relay_del.add_argument("relay_id", help="relay identity to delete")
    relay_del.add_argument("--dry-run", action="store_true", help="preview without deleting")
    follow_parser = commands.add_parser(
        "follow", help="continuously stream incoming messages",
        description="Continuously read new messages, advancing the selected cursor as they arrive.",
        epilog="Example: mailbox-client follow --to worker-1 --interval 1 --nobuffer",
    )
    follow_parser.add_argument("recipient", nargs="?", help="mailbox identity to follow; alternatively use --to")
    follow_parser.add_argument("--interval", type=float, default=1.0,
                               help="seconds between checks (default: 1)")
    follow_parser.add_argument("--require-port", action="append", default=[], type=int,
                               help="exit when this local TCP port stops listening; repeatable")
    _add_read_options(follow_parser, waiting=False)
    unread_parser = commands.add_parser(
        "unread-count", help="count unread messages without consuming them",
        description="Count unread messages without advancing the selected cursor.",
        epilog="Example: mailbox-client unread-count --to worker-1 --cursor monitor",
    )
    unread_parser.add_argument("recipient", nargs="?",
                               help="mailbox identity whose unread messages are counted; alternatively use --to")
    unread_parser.add_argument("--cursor", help="independent cursor name")
    ack_parser = commands.add_parser(
        "ack", help="advance a cursor through a specific message",
        description="Explicitly acknowledge through a message ID on a durable cursor.",
        epilog="Example: mailbox-client ack --from worker-1 MESSAGE_ID --cursor audit",
    )
    ack_parser.add_argument("recipient", nargs="?",
                            help="mailbox identity owning the cursor; alternatively use --to")
    ack_parser.add_argument("message_id", help="message ID through which the cursor advances")
    ack_parser.add_argument("--cursor", help="independent cursor name")
    subscribe_parser = commands.add_parser(
        "subscribe", help="subscribe an identity to a qualified conversation address",
        description="Durably subscribe one mailbox identity to a TYPE/INSTANCE/IDENTIFIER address.",
        epilog="Example: mailbox-client subscribe server_events --to symbolic-workbench-codex",
    )
    subscribe_parser.add_argument("channel", help="TYPE/INSTANCE/IDENTIFIER conversation address")
    unsubscribe_parser = commands.add_parser(
        "unsubscribe", help="remove an identity from a conversation address",
        description="Remove one mailbox identity's durable conversation subscription.",
        epilog="Example: mailbox-client unsubscribe server_events --to symbolic-workbench-codex",
    )
    unsubscribe_parser.add_argument("channel", help="TYPE/INSTANCE/IDENTIFIER conversation address")
    subscriptions_parser = commands.add_parser(
        "subscriptions", help="list an identity's conversation subscriptions",
        description="List one identity's subscriptions, or every subscribed mailbox with --all.",
        epilog=("Example: mailbox-client subscriptions --to symbolic-workbench-codex; "
                "mailbox-client --url http://127.0.0.1:46667 subscriptions --all"),
    )
    subscriptions_parser.add_argument(
        "--all", action="store_true", dest="all_subscriptions",
        help="list every mailbox identity having at least one subscription",
    )
    commands.add_parser(
        "status", help="show mailbox configuration",
        description="Show the selected mailbox or REST service status without consuming messages.",
        epilog="Example: mailbox-client --url http://127.0.0.1:46667 status",
    )
    commands.add_parser(
        "check", help="validate mailbox access without consuming messages",
        description="Validate mailbox access and report a nonzero exit status on failure.",
        epilog="Example: mailbox-client --url http://127.0.0.1:46667 check",
    )
    commands.add_parser("token", help="register or inspect server authentication tokens",
                        add_help=False)
    commands.add_parser("contacts", help="import or inspect durable platform contacts",
                        add_help=False)
    commands.add_parser("registry", help="manage UUID and identifier-to-text mappings",
                        add_help=False)
    commands.add_parser("discover", help="list visible resources from configured platforms",
                        add_help=False)
    for chat_command in CHAT_COMMANDS:
        commands.add_parser(
            chat_command, help=CHAT_COMMAND_HELP[chat_command],
            add_help=False,
        )
    return parser


def _option_arguments(name: str, value: Any) -> list[str]:
    option = f"--{name.replace('_', '-')}"
    if isinstance(value, bool):
        return [option] if value else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend([option, str(item)])
        return result
    if value is None:
        return []
    return [option, str(value)]


def _command_document_argv(document: Any) -> list[str]:
    if isinstance(document, list):
        if not all(isinstance(item, str) for item in document):
            raise ValueError("command JSON array must contain only strings")
        return list(document)
    if not isinstance(document, dict):
        raise ValueError("command JSON must be an object, string array, or object containing args")
    if "args" in document:
        if set(document) != {"args"}:
            raise ValueError("a command document containing args cannot contain other fields")
        return _command_document_argv(document["args"])
    command = str(document.get("command") or "").strip()
    if command not in COMMAND_POSITIONALS:
        raise ValueError(f"unknown or missing command: {command or '<empty>'}")
    known_positionals = COMMAND_POSITIONALS[command]
    known_fields = {"command", "to", *GLOBAL_RUN_FIELDS, *known_positionals}
    command_fields = set(document) - known_fields
    command_options = {
        "sender", "type", "channel_id", "channel_type", "source_id", "thread_id", "root_id",
        "attach", "cursor", "ack", "interval", "checks", "require_port", "wait", "limit",
        "contains", "message_type", "no_advance", "until", "start", "presence",
        "purge", "dry_run",
    }
    unknown = command_fields - command_options
    if unknown:
        raise ValueError(f"unknown command document fields: {', '.join(sorted(unknown))}")
    arguments: list[str] = []
    for name in GLOBAL_RUN_FIELDS:
        if name in document:
            arguments.extend(_option_arguments(name, document[name]))
    arguments.append(command)
    for name in known_positionals:
        if name == "text":
            continue
        if command == "send" and name == "recipient" and "to" in document:
            if "recipient" in document:
                raise ValueError("send command document accepts either to or recipient, not both")
            arguments.extend(["--to", str(document["to"])])
            continue
        if name not in document:
            raise ValueError(f"{command} command document requires {name}")
        arguments.append(str(document[name]))
    for name in sorted(command_fields):
        arguments.extend(_option_arguments(name, document[name]))
    if "text" in known_positionals and document.get("text") is not None:
        arguments.extend(["--", str(document["text"])])
    return arguments


def _expand_run_document(argv: list[str]) -> list[str]:
    boundary = argv.index("--") if "--" in argv else len(argv)
    active = argv[:boundary]
    matches = [index for index, value in enumerate(active) if value == "--run" or value.startswith("--run=")]
    if not matches:
        return argv
    if len(matches) != 1:
        raise ValueError("--run may be specified only once")
    index = matches[0]
    if active[index] == "--run":
        if index + 1 >= len(active):
            raise ValueError("--run requires a JSON file")
        path_text = active[index + 1]
        consumed = {index, index + 1}
    else:
        path_text = active[index].partition("=")[2]
        consumed = {index}
    if any(position not in consumed for position in range(len(active))) or boundary != len(argv):
        raise ValueError("--run defines the entire command and cannot be combined with other arguments")
    path = Path(path_text).expanduser()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load --run command document: {error}") from error
    return _command_document_argv(document)


def _batch_path(argv: list[str]) -> Path | None:
    """Extract a position-independent --batch option that defines the whole invocation."""
    boundary = argv.index("--") if "--" in argv else len(argv)
    active = argv[:boundary]
    matches = [index for index, value in enumerate(active)
               if value == "--batch" or value.startswith("--batch=")]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("--batch may be specified only once")
    index = matches[0]
    if active[index] == "--batch":
        if index + 1 >= len(active):
            raise ValueError("--batch requires a command file")
        path_text, consumed = active[index + 1], {index, index + 1}
    else:
        path_text, consumed = active[index].partition("=")[2], {index}
    if any(position not in consumed for position in range(len(active))) or boundary != len(argv):
        raise ValueError("--batch defines the entire invocation; put shared options on each file line")
    return Path(path_text).expanduser()


def _run_batch(path: Path) -> int:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(f"cannot read --batch: {error}") from error
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            arguments = shlex.split(line, posix=os.name != "nt")
        except ValueError as error:
            print(f"{path}:{line_number}: cannot parse command: {error}", file=sys.stderr)
            return 2
        if arguments and arguments[0].lower() in {"mailbox-client", "mailbox-client.cmd"}:
            arguments = arguments[1:]
        if not arguments:
            continue
        if any(item == "--batch" or item.startswith("--batch=") for item in arguments):
            print(f"{path}:{line_number}: nested --batch is not allowed", file=sys.stderr)
            return 2
        try:
            result = main(arguments)
        except SystemExit as error:
            result = int(error.code or 0)
        if result:
            print(f"{path}:{line_number}: command failed with exit status {result}", file=sys.stderr)
            return result
    return 0


def _normalize_chat_dispatch(argv: list[str]) -> list[str]:
    """Move a top-level chat command ahead of its position-independent platform options."""
    if not argv or argv[0] in CHAT_COMMANDS:
        return argv
    paired = {"--url", "--token", "--on", "--input", "--input-format", "--format"}
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in CHAT_COMMANDS:
            return [item, *argv[:index], *argv[index + 1:]]
        if item.partition("=")[0] in paired:
            index += 1 if "=" in item else 2
            continue
        if item.startswith("-"):
            index += 1
            continue
        return argv
    return argv


def _normalize_anywhere_flags(argv: list[str]) -> list[str]:
    """Move position-independent global options ahead of the subcommand."""
    boundary = argv.index("--") if "--" in argv else len(argv)
    options, literal_arguments = argv[:boundary], argv[boundary:]
    curl_requested = "--curl" in options
    nobuffer_requested = "--nobuffer" in options
    normalized: list[str] = []
    input_option: list[str] = []
    identity_options: dict[str, list[str]] = {
        "--to": [], "--from": [], "--as": [], "--subscribed": [],
    }
    index = 0
    while index < len(options):
        argument = options[index]
        if argument in {"--curl", "--nobuffer"}:
            index += 1
            continue
        if argument == "--input":
            if index + 1 >= len(options):
                normalized.append(argument)
                index += 1
                continue
            input_option = [argument, options[index + 1]]
            index += 2
            continue
        if argument in identity_options:
            if index + 1 >= len(options):
                normalized.append(argument)
                index += 1
                continue
            identity_options[argument] = [argument, options[index + 1]]
            index += 2
            continue
        if (argument.startswith("--to=") or argument.startswith("--from=") or
                argument.startswith("--as=") or
                argument.startswith("--subscribed=")):
            option = argument.partition("=")[0]
            identity_options[option] = [argument]
            index += 1
            continue
        if argument.startswith("--input="):
            input_option = [argument]
            index += 1
            continue
        normalized.append(argument)
        index += 1
    leading = (["--curl"] if curl_requested else []) + (["--nobuffer"] if nobuffer_requested else [])
    return (leading + input_option + identity_options["--as"] + identity_options["--from"] +
            identity_options["--to"] + identity_options["--subscribed"] + normalized + literal_arguments)


def _enable_unbuffered_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(line_buffering=True, write_through=True)


def _json_object_with_assignments(document: str, assignments: list[str], option: str) -> dict[str, Any]:
    try:
        result = json.loads(document)
    except json.JSONDecodeError as error:
        build_parser().error(f"{option} must be a JSON object: {error}")
    if not isinstance(result, dict):
        build_parser().error(f"{option} must be a JSON object")
    for assignment in assignments:
        key, separator, raw_value = assignment.partition("=")
        if not separator or not key.strip():
            build_parser().error(f"--set requires KEY=VALUE, received: {assignment}")
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        result[key.strip()] = value
    return result


def main(argv: list[str] | None = None) -> int:
    global REST_TIMEOUT, REST_RETRIES, REST_RETRY_DELAY, REST_TOKEN
    supplied = list(sys.argv[1:] if argv is None else argv)
    try:
        batch_path = _batch_path(supplied)
        if batch_path is not None:
            return _run_batch(batch_path)
    except ValueError as error:
        build_parser().error(str(error))
    supplied = _normalize_chat_dispatch(supplied)
    if supplied and supplied[0] in {
        "token", "contacts", "registry", "discover", *CHAT_COMMANDS,
    }:
        family, nested = supplied[0], supplied[1:]
        if family == "token":
            from .token_admin import main as administration_main
        elif family == "contacts":
            from .contact_admin import main as administration_main
        elif family == "registry":
            from .registry_admin import main as administration_main
        elif family == "discover":
            from .discovery_admin import main as administration_main
        elif family in CHAT_COMMANDS:
            from .chat_admin import main as administration_main
            nested = [family, *nested]
        return administration_main(nested)
    try:
        supplied = _expand_run_document(supplied)
    except ValueError as error:
        build_parser().error(str(error))
    args = build_parser().parse_args(_normalize_anywhere_flags(supplied))
    from .endpoint_address import parse_endpoint
    source_endpoint = parse_endpoint(args.global_sender) if args.global_sender else None
    destination_value = args.global_recipient or (
        args.recipient if args.command == "send" else None
    )
    destination_endpoint = parse_endpoint(destination_value) if destination_value else None
    if source_endpoint and not args.local_identity:
        build_parser().error("remote --from ENDPOINT requires --as IDENTITY")
    if args.nobuffer:
        _enable_unbuffered_output()
    if args.command != "send" and args.input_file:
        build_parser().error("--input is only valid with send")
    if args.command == "send":
        if source_endpoint:
            build_parser().error("send --from must be a local identity; use --as IDENTITY")
        if destination_endpoint:
            args.channel_type = destination_endpoint.adapter
            args.channel_id = destination_endpoint.identifier
            if args.global_recipient:
                args.global_recipient = "outbound_delivery"
            else:
                args.recipient = "outbound_delivery"
        if args.global_recipient and args.recipient:
            if args.text is not None:
                build_parser().error("send accepts either positional recipient or --to, not both")
            args.text, args.recipient = args.recipient, None
        args.recipient = args.global_recipient or args.recipient
        if not args.recipient:
            build_parser().error("send requires positional recipient or --to RECIPIENT")
        if args.input_file and args.text is not None:
            build_parser().error("send accepts either inline text or --input, not both")
        if args.input_file:
            try:
                args.text = args.input_file.expanduser().read_text(encoding="utf-8")
            except OSError as error:
                build_parser().error(f"cannot read --input: {error}")
        if args.text is None:
            build_parser().error("send requires inline text or --input PATH")
    elif args.command in {"receive", "peek", "poll", "follow", "unread-count", "ack"}:
        if args.command != "ack" and args.global_sender and not source_endpoint:
            build_parser().error("--from is only valid with send")
        if args.command == "ack" and args.global_sender and args.global_recipient:
            build_parser().error("ack accepts --from or the compatibility --to alias, not both")
        selected_identity = (args.local_identity or args.global_sender) if args.command == "ack" else (
            args.local_identity if source_endpoint or (
                args.command == "poll" and getattr(args, "poll_subscriptions", False)
            ) else args.global_recipient
        )
        selected_identity = selected_identity or args.global_recipient
        if selected_identity and args.recipient:
            build_parser().error(f"{args.command} accepts either positional recipient or --to, not both")
        args.recipient = selected_identity or args.recipient
        if args.command == "poll" and getattr(args, "poll_subscriptions", False):
            args.cursor = args.cursor or args.recipient
            args.recipient = None
            if not args.cursor:
                build_parser().error("poll --subscriptions requires --as AGENT or --cursor NAME")
        elif not args.recipient:
            option = "--as IDENTITY" if args.command == "ack" else "--to RECIPIENT"
            build_parser().error(f"{args.command} requires positional recipient or {option}")
    elif args.command in {"poll-many", "history", "cursor-init"}:
        if args.global_recipient or args.global_sender or args.local_identity:
            build_parser().error(f"{args.command} takes channel addresses as positional arguments")
    elif args.command in {"subscribe", "unsubscribe", "subscriptions"}:
        all_subscriptions = bool(
            args.command == "subscriptions" and getattr(args, "all_subscriptions", False)
        )
        if args.global_sender or (not args.global_recipient and not all_subscriptions):
            build_parser().error(f"{args.command} requires --to IDENTITY")
        if all_subscriptions and args.global_recipient:
            build_parser().error("subscriptions accepts --all or --to IDENTITY, not both")
    elif args.global_recipient or args.global_sender:
        build_parser().error("--from and --to require a message or mailbox command")
    if args.timeout <= 0 or args.retry < 0 or args.retry_delay < 0:
        build_parser().error("--timeout must be positive; retry values must be non-negative")
    REST_TIMEOUT, REST_RETRIES, REST_RETRY_DELAY = args.timeout, args.retry, args.retry_delay
    REST_TOKEN = args.token or os.environ.get(MAILBOX_TOKEN_ENV)
    configured_dir = os.environ.get(MAILBOX_ENV)
    if args.mailbox:
        try:
            mailbox_root, rest_url = named_mailbox(args.mailbox, args.config)
        except ValueError as error:
            build_parser().error(str(error))
    else:
        mailbox_root = args.dir.expanduser().resolve() if args.dir else (
            Path(configured_dir).expanduser().resolve() if configured_dir else None
        )
        rest_url = args.url or (None if mailbox_root else os.environ.get(MAILBOX_URL_ENV))
    use_rest = bool(rest_url)
    subscription_identity = args.local_identity or args.global_recipient
    declared_subscriptions = ([source_endpoint.canonical] if source_endpoint else [])
    if args.subscribed:
        if not subscription_identity:
            build_parser().error("--subscribed requires --to or --as IDENTITY")
        from .subscriptions import set_subscription
        declared_subscriptions.extend(
            item.strip() for item in args.subscribed.split(",") if item.strip()
        )
    requested_channels = list(dict.fromkeys(declared_subscriptions))
    if args.subscribed and not requested_channels:
            build_parser().error("--subscribed requires at least one channel")
    for channel in requested_channels:
        if use_rest:
            _rest_request("POST", "/v1/subscriptions", {
                "channel": channel, "identity": subscription_identity, "subscribed": True,
            }, base_url=rest_url)
        else:
            if channel.lower().startswith(("mm/", "mattermost/")):
                from .identifier_directory import IdentifierDirectory
                from .adapters.mattermost_adapter import resolve_address
                channel = resolve_address(
                    channel, IdentifierDirectory(mailbox_root or mailbox_dir()),
                    base_url=os.environ.get("MM_URL", ""),
                )
            from .subscriptions import set_subscription
            set_subscription(channel, subscription_identity, enabled=True)
    if args.curl:
        if not use_rest:
            build_parser().error("--curl requires a REST transport selected by --url, --mailbox, or AGENT_MAILBOX_URL")
        print(curl_command(args, str(rest_url), token=bool(REST_TOKEN)))
        return 0
    if args.verbose:
        target = rest_url if use_rest else str(mailbox_root or mailbox_dir())
        print(f"agent_mailbox transport={'REST' if use_rest else 'JSONL'} target={target}", file=sys.stderr)
    if args.command in {"agents", "connectors", "channels", "relays"}:
        registry = (_rest_request("GET", "/v1/registry", base_url=rest_url)
                    if use_rest else __import__(
                        "mailbox_channels.connector_registry", fromlist=["public_registry"]
                    ).public_registry())
        if args.command == "agents":
            enriched_agents = []
            for agent in registry.get("agents", []):
                agent_id = str(agent.get("agent_id") or "")
                cursor_state = (_rest_request(
                    "GET", "/v1/cursors?" + urllib.parse.urlencode({"cursor": agent_id}),
                    base_url=rest_url,
                ) if use_rest else {
                    "cursor": agent_id,
                    "recipients": cursor_subscriptions(agent_id, root=mailbox_root),
                    "positions": cursor_positions(agent_id, root=mailbox_root),
                })
                subscriptions = [
                    {
                        "channel": item.get("recipient"),
                        "cursor": item.get("cursor"),
                        "offset": item.get("offset", 0),
                    }
                    for item in cursor_state.get("positions", [])
                ]
                enriched_agents.append({
                    **agent,
                    "subscriptions": subscriptions,
                })
            result = {"agents": enriched_agents}
        else:
            result = {args.command: registry.get(args.command, [])}
        _emit(json.dumps(result, ensure_ascii=False, indent=2), output=args.output, quiet=args.quiet)
    elif args.command == "cursors":
        result = (_rest_request(
            "GET", "/v1/cursors?" + urllib.parse.urlencode({"cursor": args.cursor}),
            base_url=rest_url,
        ) if use_rest else {
            "cursor": args.cursor,
            "recipients": cursor_subscriptions(args.cursor, root=mailbox_root),
            "positions": cursor_positions(args.cursor, root=mailbox_root),
        })
        _emit(json.dumps(result, ensure_ascii=False, indent=2), output=args.output, quiet=args.quiet)
    elif args.command == "agent-add":
        if use_rest:
            result = register_agent_rest(
                args.agent_id, presence_id=args.presence_id or "",
                dry_run=args.dry_run, base_url=rest_url,
            )
        else:
            from .connector_registry import register_agent
            result = register_agent(
                args.agent_id, presence_id=args.presence_id or "",
                dry_run=args.dry_run, mailbox_root=mailbox_root,
            )
        _emit(json.dumps(result, ensure_ascii=False, indent=2), output=args.output, quiet=args.quiet)
    elif args.command == "agent-del":
        if args.dry_run and not args.purge:
            build_parser().error("agent-del --dry-run requires --purge")
        if use_rest:
            result = unregister_agent_rest(
                args.agent_id, presence_id=args.presence_id or "", purge=args.purge,
                dry_run=args.dry_run, base_url=rest_url,
            )
        else:
            from .connector_registry import unregister_agent
            result = unregister_agent(
                args.agent_id, presence_id=args.presence_id or "", purge=args.purge,
                dry_run=args.dry_run, mailbox_root=mailbox_root,
            )
        _emit(json.dumps(result, ensure_ascii=False, indent=2), output=args.output, quiet=args.quiet)
    elif args.command in {
        "connector-add", "connector-del", "channel-add", "channel-del", "relay-add", "relay-del",
    }:
        if use_rest:
            build_parser().error(f"{args.command} currently requires direct local configuration access")
        if args.command == "connector-add":
            config = _json_object_with_assignments(args.config, args.config_values, "--config")
            from .connector_registry import register_connector
            result = register_connector(
                args.connector_id, args.kind, direction=args.direction, instance=args.instance,
                channel_ids=args.channel_ids, config=config, dry_run=args.dry_run,
            )
        elif args.command == "connector-del":
            from .connector_registry import unregister_connector
            result = unregister_connector(args.connector_id, dry_run=args.dry_run)
        elif args.command == "channel-add":
            metadata = _json_object_with_assignments(
                args.metadata, args.metadata_values, "--metadata",
            )
            from .subscriptions import ensure_channel
            result = ensure_channel(args.channel_id, metadata=metadata, aliases=args.aliases)
        elif args.command == "channel-del":
            from .subscriptions import delete_channel
            result = delete_channel(args.channel_id, force=args.force)
        elif args.command == "relay-add":
            from .retained_relay import add_relay
            result = add_relay(
                args.source_channel, args.destination, relay_id=args.relay_id,
                start=args.start, mailbox_root=mailbox_root, dry_run=args.dry_run,
            )
        else:
            from .retained_relay import delete_relay
            result = delete_relay(args.relay_id, dry_run=args.dry_run)
        _emit(json.dumps(result, ensure_ascii=False, indent=2), output=args.output, quiet=args.quiet)
    elif args.command in {"subscribe", "unsubscribe", "subscriptions"}:
        from .subscriptions import set_subscription, subscriptions
        if args.command == "subscriptions":
            if args.all_subscriptions:
                if use_rest:
                    configured = _rest_request("GET", "/v1/registry", base_url=rest_url).get(
                        "channels", []
                    )
                else:
                    from .subscriptions import channels
                    configured = channels()
                by_identity: dict[str, list[str]] = {}
                for subscription in configured:
                    for identity in subscription.get("subscribers") or []:
                        by_identity.setdefault(str(identity), []).append(str(subscription["id"]))
                result = {"mailboxes": [
                    {"identity": identity, "channels": channel_ids}
                    for identity, channel_ids in sorted(by_identity.items())
                ]}
            else:
                result = (_rest_request(
                    "GET", "/v1/subscriptions?" + urllib.parse.urlencode({"identity": args.global_recipient}),
                    base_url=rest_url,
                ) if use_rest else {"identity": args.global_recipient,
                                    "channels": subscriptions(args.global_recipient)})
        else:
            enabled = args.command == "subscribe"
            channel = args.channel
            if not use_rest and channel.lower().startswith(("mm/", "mattermost/")):
                from .identifier_directory import IdentifierDirectory
                from .adapters.mattermost_adapter import resolve_address
                channel = resolve_address(
                    channel, IdentifierDirectory(mailbox_root or mailbox_dir()),
                    base_url=os.environ.get("MM_URL", ""),
                )
            result = (_rest_request(
                "POST", "/v1/subscriptions",
                {"channel": channel, "identity": args.global_recipient, "subscribed": enabled},
                base_url=rest_url,
            ) if use_rest else set_subscription(channel, args.global_recipient, enabled=enabled))
        _emit(json.dumps(result, ensure_ascii=False, indent=2), output=args.output, quiet=args.quiet)
    elif args.command == "send":
        record = (
                (send_rest if use_rest else send)(
                    args.recipient,
                    args.text,
                    sender=args.sender or args.local_identity or args.global_sender or DEFAULT_SENDER,
                    message_type=args.message_type,
                    attachments=args.attach,
                    channel_id=args.channel_id,
                    channel_type=args.channel_type,
                    source_id=args.source_id,
                    thread_id=args.thread_id,
                    root_id=args.root_id,
                    extra_fields={"endpoint_address": destination_endpoint.canonical,
                                  "endpoint": destination_endpoint.describe()}
                    if destination_endpoint else None,
                    **({"base_url": rest_url} if use_rest else {"root": mailbox_root}),
                )
        )
        _emit(_render_records([record], args.format), output=args.output, quiet=args.quiet)
    elif args.command in {"receive", "peek"}:
        if args.command == "receive" and args.ack:
            acknowledged = (acknowledge_rest(args.recipient, args.ack, base_url=rest_url, cursor=args.cursor)
                            if use_rest else acknowledge(args.recipient, args.ack, root=mailbox_root,
                                                         cursor=args.cursor))
            _emit(json.dumps({"acknowledged": acknowledged}), output=args.output, quiet=args.quiet)
            return 0 if acknowledged else 1
        advance = args.command == "receive" and not args.no_advance
        deadline = time.monotonic() + max(0.0, getattr(args, "wait", 0.0))
        while True:
            records = (receive_rest(args.recipient, base_url=rest_url, advance=advance, cursor=args.cursor)
                       if use_rest else receive(args.recipient, root=mailbox_root, advance=advance,
                                                cursor=args.cursor))
            if records or time.monotonic() >= deadline:
                break
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        records = _select_records(records, since=args.since, until=args.until,
                                  limit=args.limit, where=args.where)
        _emit(_render_records(records, args.format), output=args.output, quiet=args.quiet)
    elif args.command == "poll":
        if args.poll_subscriptions:
            recipients = (list(_rest_request(
                "GET", "/v1/cursors?" + urllib.parse.urlencode({"cursor": args.cursor}),
                base_url=rest_url,
            ).get("recipients", [])) if use_rest else cursor_subscriptions(args.cursor, root=mailbox_root))
            if not recipients:
                build_parser().error(f"cursor {args.cursor!r} has no polling subscriptions")
        else:
            recipients = [args.recipient]
        if not use_rest and len(recipients) == 1:
            records, missing_ports = poll(recipients[0], interval_seconds=args.interval,
                                          max_checks=args.checks, required_ports=tuple(args.require_port),
                                          root=mailbox_root, advance=not args.no_advance,
                                          cursor=args.cursor)
        else:
            records, missing_ports = [], []
            for check in range(args.checks):
                if check:
                    time.sleep(args.interval)
                records = []
                for recipient in recipients:
                    records.extend(
                        receive_rest(recipient, base_url=rest_url,
                                     advance=not args.no_advance, cursor=args.cursor)
                        if use_rest else receive(recipient, root=mailbox_root,
                                                advance=not args.no_advance, cursor=args.cursor)
                    )
                records.sort(key=lambda record: (
                    str(record.get("timestamp", "")), str(record.get("id", "")),
                ))
                if records:
                    break
                missing_ports = [port for port in args.require_port if not _port_is_listening(port)]
                if missing_ports:
                    break
        records = _select_records(records, since=args.since, until=args.until,
                                  limit=args.limit, where=args.where)
        _emit(_render_records(records, args.format), output=args.output, quiet=args.quiet)
        if missing_ports:
            print(json.dumps({"error": "monitored_process_failure", "missing_ports": missing_ports}), file=sys.stderr)
            return 2
    elif args.command == "poll-many":
        if args.interval < 0 or args.checks < 1:
            build_parser().error("poll-many requires a non-negative --interval and positive --checks")
        records, missing_ports = [], []
        for check in range(args.checks):
            if check:
                time.sleep(args.interval)
            batch: list[dict[str, Any]] = []
            for recipient in args.recipients:
                batch.extend(
                    receive_rest(recipient, base_url=rest_url,
                                 advance=not args.no_advance, cursor=args.cursor)
                    if use_rest else
                    receive(recipient, root=mailbox_root,
                            advance=not args.no_advance, cursor=args.cursor)
                )
            batch.sort(key=lambda record: (str(record.get("timestamp", "")), str(record.get("id", ""))))
            records = _select_records(batch, since=args.since, until=args.until,
                                      limit=args.limit, where=args.where)
            if records:
                break
            missing_ports = [port for port in args.require_port if not _port_is_listening(port)]
            if missing_ports:
                break
        _emit(_render_records(records, args.format), output=args.output, quiet=args.quiet)
        if missing_ports:
            print(json.dumps({"error": "monitored_process_failure", "missing_ports": missing_ports}),
                  file=sys.stderr)
            return 2
    elif args.command == "history":
        records = []
        history_cursor = f"history-{uuid.uuid4().hex}"
        for recipient in args.recipients:
            records.extend(
                peek_rest(recipient, base_url=rest_url, cursor=history_cursor)
                if use_rest else peek(recipient, root=mailbox_root, cursor=history_cursor)
            )
        records.sort(key=lambda record: (str(record.get("timestamp", "")), str(record.get("id", ""))))
        records = _select_records(records, since=args.since, until=args.until,
                                  limit=args.limit, where=args.where)
        _emit(_render_records(records, args.format), output=args.output, quiet=args.quiet)
    elif args.command == "cursor-init":
        initialized = [
            initialize_cursor_rest(recipient, cursor=args.cursor, start=args.start, base_url=rest_url)
            if use_rest else initialize_cursor(
                recipient, cursor=args.cursor, start=args.start, root=mailbox_root,
            )
            for recipient in args.recipients
        ]
        _emit(json.dumps({"cursors": initialized}, ensure_ascii=False, indent=2),
              output=args.output, quiet=args.quiet)
    elif args.command == "follow":
        if args.interval < 0:
            raise ValueError("interval must be non-negative")
        seen_without_advance: set[str] = set()
        try:
            while True:
                records = (receive_rest(args.recipient, base_url=rest_url,
                                        advance=not args.no_advance, cursor=args.cursor) if use_rest
                           else receive(args.recipient, root=mailbox_root,
                                        advance=not args.no_advance, cursor=args.cursor))
                records = _select_records(records, since=args.since, until=args.until,
                                          limit=args.limit, where=args.where)
                if args.no_advance:
                    records = [record for record in records if str(record.get("id")) not in seen_without_advance]
                    seen_without_advance.update(str(record.get("id")) for record in records)
                _emit(_render_records(records, args.format), output=args.output, quiet=args.quiet, append=True)
                missing_ports = [port for port in args.require_port if not _port_is_listening(port)]
                if missing_ports:
                    print(json.dumps({"error": "monitored_process_failure", "missing_ports": missing_ports}),
                          file=sys.stderr)
                    return 2
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
    elif args.command == "unread-count":
        records = (peek_rest(args.recipient, base_url=rest_url, cursor=args.cursor) if use_rest
                   else peek(args.recipient, root=mailbox_root, cursor=args.cursor))
        _emit(str(len(records)), output=args.output, quiet=args.quiet)
    elif args.command == "ack":
        acknowledged = (acknowledge_rest(args.recipient, args.message_id, base_url=rest_url,
                                         cursor=args.cursor) if use_rest
                        else acknowledge(args.recipient, args.message_id, root=mailbox_root,
                                         cursor=args.cursor))
        _emit(json.dumps({"acknowledged": acknowledged}), output=args.output, quiet=args.quiet)
        return 0 if acknowledged else 1
    else:
        result = status_rest(base_url=rest_url) if use_rest else status(root=mailbox_root)
        if args.command == "check":
            result = {"ok": True, "transport": "rest" if use_rest else "jsonl", **result}
        _emit(json.dumps(result, ensure_ascii=False, indent=2), output=args.output, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
