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
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SENDER = "local-agent"
PEERS = ("omegaclaw-core", "omegaclaw-min", "channel-relay")
MAILBOX_ENV = "AGENT_MAILBOX_DIR"
MAILBOX_URL_ENV = "AGENT_MAILBOX_URL"
MAILBOX_TOKEN_ENV = "AGENT_MAILBOX_TOKEN"
DEFAULT_MAILBOX_URL = "http://127.0.0.1:46667"
VERSION = "0.2.0"
REST_TIMEOUT = 15.0
REST_RETRIES = 0
REST_RETRY_DELAY = 1.0
REST_TOKEN: str | None = None
UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


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
    for source in paths:
        resolved = source.expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise ValueError(f"attachment is not a file: {source}")
        name = _safe_attachment_name(resolved, used_names)
        destination = destination_dir / name
        shutil.copyfile(resolved, destination)
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
    payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(target / "messages.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
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
    return {
        "directory": str(target),
        "messages_file": str(path),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "default_sender": DEFAULT_SENDER,
        "peers": list(PEERS),
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
              base_url: str | None = None) -> dict[str, Any]:
    payload = {"to": recipient, "text": text, "from": sender, "type": message_type,
               "attachments": [str(path.expanduser().resolve()) for path in (attachments or [])],
               "channel_id": channel_id, "channel_type": channel_type, "source_id": source_id,
               "thread_id": thread_id, "root_id": root_id}
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
                    limit: int | None = None, where: list[str] | None = None) -> list[dict[str, Any]]:
    filters = _where_filters(where or [])
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
        if all(str(record.get(key, "")) == expected for key, expected in filters.items()):
            selected.append(record)
            if limit is not None and len(selected) >= limit:
                break
    return selected


def _render_records(records: list[dict[str, Any]], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(records, ensure_ascii=False, indent=2)
    if output_format == "text":
        return "\n".join(f"[{item.get('timestamp', '')}] {item.get('from', '')}: {item.get('text', '')}"
                          for item in records)
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
    parser.add_argument("--limit", type=int, help="maximum records to return")
    parser.add_argument("--where", action="append", default=[], metavar="FIELD=VALUE")
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
    parser = argparse.ArgumentParser(description=__doc__)
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--dir", type=Path, help="use this JSONL mailbox directory")
    transport.add_argument("--url", help=f"use REST instead of JSONL (default service: {DEFAULT_MAILBOX_URL})")
    transport.add_argument("--mailbox", help="use a named mailbox from the mailbox config")
    parser.add_argument("--config", type=Path, help="mailbox configuration file")
    parser.add_argument("--from", dest="global_sender", help="default sender identity")
    parser.add_argument("--format", choices=("jsonl", "json", "text"), default="jsonl")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--token", help=f"REST Bearer token (or {MAILBOX_TOKEN_ENV})")
    parser.add_argument("--curl", action="store_true",
                        help="print the equivalent REST curl command without executing it")
    parser.add_argument("--retry", type=int, default=0)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)
    send_parser = commands.add_parser("send", help="append a message")
    send_parser.add_argument("recipient")
    send_parser.add_argument("text")
    send_parser.add_argument("--sender")
    send_parser.add_argument("--type", dest="message_type", default="message")
    send_parser.add_argument("--channel-id")
    send_parser.add_argument("--channel-type")
    send_parser.add_argument("--source-id")
    send_parser.add_argument("--thread-id")
    send_parser.add_argument("--root-id")
    send_parser.add_argument(
        "--attach",
        action="append",
        default=[],
        type=Path,
        help="copy a file into this message (repeatable)",
    )
    receive_parser = commands.add_parser("receive", help="consume unread messages")
    receive_parser.add_argument("recipient")
    _add_read_options(receive_parser)
    receive_parser.add_argument("--ack", metavar="MESSAGE_ID", help="acknowledge through this message ID")
    peek_parser = commands.add_parser("peek", help="show unread messages without advancing the cursor")
    peek_parser.add_argument("recipient")
    _add_read_options(peek_parser, waiting=False)
    poll_parser = commands.add_parser("poll", help="poll for unread messages")
    poll_parser.add_argument("recipient")
    poll_parser.add_argument("--interval", type=float, default=30.0)
    poll_parser.add_argument("--checks", type=int, default=10)
    poll_parser.add_argument("--require-port", action="append", default=[], type=int)
    _add_read_options(poll_parser, waiting=False)
    follow_parser = commands.add_parser("follow", help="continuously stream incoming messages")
    follow_parser.add_argument("recipient")
    follow_parser.add_argument("--interval", type=float, default=1.0)
    follow_parser.add_argument("--require-port", action="append", default=[], type=int)
    _add_read_options(follow_parser, waiting=False)
    unread_parser = commands.add_parser("unread-count", help="count unread messages without consuming them")
    unread_parser.add_argument("recipient")
    unread_parser.add_argument("--cursor")
    ack_parser = commands.add_parser("ack", help="advance a cursor through a specific message")
    ack_parser.add_argument("recipient")
    ack_parser.add_argument("message_id")
    ack_parser.add_argument("--cursor")
    commands.add_parser("status", help="show mailbox configuration")
    commands.add_parser("check", help="validate mailbox access without consuming messages")
    return parser


def _normalize_anywhere_flags(argv: list[str]) -> list[str]:
    """Move position-independent global flags ahead of the subcommand."""
    boundary = argv.index("--") if "--" in argv else len(argv)
    options, literal_arguments = argv[:boundary], argv[boundary:]
    curl_requested = "--curl" in options
    normalized = [argument for argument in options if argument != "--curl"]
    return (["--curl"] if curl_requested else []) + normalized + literal_arguments


def main(argv: list[str] | None = None) -> int:
    global REST_TIMEOUT, REST_RETRIES, REST_RETRY_DELAY, REST_TOKEN
    supplied = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(_normalize_anywhere_flags(supplied))
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
    if args.curl:
        if not use_rest:
            build_parser().error("--curl requires a REST transport selected by --url, --mailbox, or AGENT_MAILBOX_URL")
        print(curl_command(args, str(rest_url), token=bool(REST_TOKEN)))
        return 0
    if args.verbose:
        target = rest_url if use_rest else str(mailbox_root or mailbox_dir())
        print(f"agent_mailbox transport={'REST' if use_rest else 'JSONL'} target={target}", file=sys.stderr)
    if args.command == "send":
        record = (
                (send_rest if use_rest else send)(
                    args.recipient,
                    args.text,
                    sender=args.sender or args.global_sender or DEFAULT_SENDER,
                    message_type=args.message_type,
                    attachments=args.attach,
                    channel_id=args.channel_id,
                    channel_type=args.channel_type,
                    source_id=args.source_id,
                    thread_id=args.thread_id,
                    root_id=args.root_id,
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
        records = _select_records(records, since=args.since, limit=args.limit, where=args.where)
        _emit(_render_records(records, args.format), output=args.output, quiet=args.quiet)
    elif args.command == "poll":
        if use_rest:
            records, missing_ports = [], []
            for check in range(args.checks):
                if check:
                    time.sleep(args.interval)
                records = receive_rest(args.recipient, base_url=rest_url,
                                       advance=not args.no_advance, cursor=args.cursor)
                if records:
                    break
                missing_ports = [port for port in args.require_port if not _port_is_listening(port)]
                if missing_ports:
                    break
        else:
            records, missing_ports = poll(args.recipient, interval_seconds=args.interval,
                                          max_checks=args.checks, required_ports=tuple(args.require_port),
                                          root=mailbox_root, advance=not args.no_advance,
                                          cursor=args.cursor)
        records = _select_records(records, since=args.since, limit=args.limit, where=args.where)
        _emit(_render_records(records, args.format), output=args.output, quiet=args.quiet)
        if missing_ports:
            print(json.dumps({"error": "monitored_process_failure", "missing_ports": missing_ports}), file=sys.stderr)
            return 2
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
                records = _select_records(records, since=args.since, limit=args.limit, where=args.where)
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
