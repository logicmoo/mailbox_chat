"""Run Mailbox Channel Relay Bridging Proxy as a standalone daemon."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv


PACKAGE_ROOT = Path(__file__).resolve().parent
RESOURCE_ROOT = PACKAGE_ROOT / "resources"
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
DEFAULT_HOST = "127.0.0.1"
HOST_ENV = "MAILBOX_RELAY_HOST"
PORT_ENV = "MAILBOX_RELAY_PORT"
TOKEN_ENV = "MAILBOX_RELAY_TOKEN"
from .channel_relay import (
    ADAPTER_CAPABILITIES, ChannelRelay, PLANNED_CHANNEL_TYPES, RELAY_PORT, RELAY_RECIPIENT,
    SUPPORTED_CHANNEL_TYPES,
)
from . import agent_mailbox
from .listener_registry import CONFIG_DIR_ENV, config_dir, public_registry
from .websocket_chat import accept_value, handle_chat
from .attachment_gateway import ATTACHMENT_PREFIX, PUBLIC_URL_ENV
from .attachment_storage import (
    DEFAULT_MAX_FILE_BYTES, DEFAULT_MAX_STORAGE_BYTES, MAX_FILE_ENV, MAX_STORAGE_ENV,
)
from .sqlite_limits import DEFAULT_MAX_SQLITE_BYTES, MAX_SQLITE_ENV
from .identifier_directory import IdentifierDirectory
from .meta_webhooks import verify_challenge, verify_signature


def runtime_paths(port: int, mailbox_root: Path | None = None) -> tuple[Path, Path, Path]:
    runtime_dir = (mailbox_root or agent_mailbox.mailbox_dir()) / "runtime" / f"channel-relay-{port}"
    return runtime_dir, runtime_dir / "relay.pid", runtime_dir / "status.json"


def _write_status(relay: ChannelRelay) -> None:
    status_file = Path(str(relay.status["statusFile"]))
    temporary = status_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(relay.status, indent=2) + "\n", encoding="utf-8")
    temporary.replace(status_file)


def _safe_write_status(relay: ChannelRelay) -> None:
    try:
        _write_status(relay)
    except OSError as error:
        relay.status["statusWriteError"] = str(error)


def run_relay_supervisor(relay: ChannelRelay, *, sleep=time.sleep) -> None:
    """Keep adapters alive until explicitly stopped, despite recoverable failures."""
    retry_delay = 1.0
    configured = False
    relay.status["running"] = True
    while not relay.stop_requested:
        try:
            if not configured:
                relay.configure()
                configured = True
            if relay.status.get("enabled"):
                relay.cycle()
            retry_delay = 1.0
            relay.status["supervisorError"] = None
            _safe_write_status(relay)
            sleep(1.0)
        except Exception as error:
            relay.status.update({
                "running": True,
                "connected": False,
                "lastError": str(error),
                "supervisorError": type(error).__name__,
                "retryInSeconds": retry_delay,
            })
            try:
                relay.reset_after_failure()
            except Exception as reset_error:
                relay.status["resetError"] = str(reset_error)
            configured = False
            _safe_write_status(relay)
            sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailbox-relay-server",
        description=__doc__,
        epilog="Bind address controls where the server listens; public address is the URL clients can reach.",
    )
    parser.add_argument(
        "--host", default=os.environ.get(HOST_ENV, DEFAULT_HOST),
        help=f"server bind address (environment: {HOST_ENV}; default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get(PORT_ENV, RELAY_PORT)),
        help=f"server TCP port (environment: {PORT_ENV}; default: {RELAY_PORT})",
    )
    parser.add_argument("--mailbox-dir", type=Path, help="mailbox data directory")
    parser.add_argument("--config-dir", type=Path, help="directory containing .env, relays.json, and mailboxes.json")
    parser.add_argument(
        "--public-address", "--public-url", dest="public_url", default=os.environ.get(PUBLIC_URL_ENV),
        help=f"externally reachable base URL advertised to clients and used for attachment links "
             f"(environment: {PUBLIC_URL_ENV})",
    )
    parser.add_argument("--token", help=f"require this REST Bearer token (or {TOKEN_ENV})")
    parser.add_argument(
        "--max-attachment-mb", type=int,
        default=int(os.environ.get(MAX_FILE_ENV, DEFAULT_MAX_FILE_BYTES)) // (1024 * 1024),
        help=f"maximum size of one attachment in MiB (environment: {MAX_FILE_ENV}; default: 1024)",
    )
    parser.add_argument(
        "--max-attachment-storage-mb", type=int,
        default=int(os.environ.get(MAX_STORAGE_ENV, DEFAULT_MAX_STORAGE_BYTES)) // (1024 * 1024),
        help=f"maximum total attachment storage in MiB (environment: {MAX_STORAGE_ENV}; default: 25600)",
    )
    parser.add_argument(
        "--max-jsonl-mb", type=int,
        default=int(os.environ.get(agent_mailbox.MAX_JSONL_ENV, agent_mailbox.DEFAULT_MAX_JSONL_BYTES))
        // (1024 * 1024),
        help=f"maximum messages.jsonl size in MiB (environment: {agent_mailbox.MAX_JSONL_ENV}; default: 5120)",
    )
    parser.add_argument(
        "--max-sqlite-mb", type=int,
        default=int(os.environ.get(MAX_SQLITE_ENV, DEFAULT_MAX_SQLITE_BYTES)) // (1024 * 1024),
        help=f"maximum size of each relay SQLite database in MiB "
             f"(environment: {MAX_SQLITE_ENV}; default: 1024)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if not 1 <= arguments.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if any(value < 1 for value in (
        arguments.max_attachment_mb, arguments.max_attachment_storage_mb,
        arguments.max_jsonl_mb, arguments.max_sqlite_mb,
    )):
        raise SystemExit("storage size limits must be at least 1 MiB")
    os.environ[MAX_FILE_ENV] = str(arguments.max_attachment_mb * 1024 * 1024)
    os.environ[MAX_STORAGE_ENV] = str(arguments.max_attachment_storage_mb * 1024 * 1024)
    os.environ[agent_mailbox.MAX_JSONL_ENV] = str(arguments.max_jsonl_mb * 1024 * 1024)
    os.environ[MAX_SQLITE_ENV] = str(arguments.max_sqlite_mb * 1024 * 1024)
    if arguments.mailbox_dir:
        os.environ[agent_mailbox.MAILBOX_ENV] = str(arguments.mailbox_dir.expanduser().resolve())
    if arguments.config_dir:
        os.environ[CONFIG_DIR_ENV] = str(arguments.config_dir.expanduser().resolve())
    configuration_root = config_dir()
    load_dotenv(configuration_root / ".env", override=False)
    if arguments.token:
        os.environ[TOKEN_ENV] = arguments.token
    mailbox_root = agent_mailbox.mailbox_dir()
    public_url = arguments.public_url
    if not public_url:
        advertised_host = "127.0.0.1" if arguments.host in {"127.0.0.1", "localhost"} else socket.getfqdn()
        public_url = f"http://{advertised_host}:{arguments.port}"
    os.environ[PUBLIC_URL_ENV] = public_url.rstrip("/")
    runtime_dir, pid_file, status_file = runtime_paths(arguments.port, mailbox_root)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    identifiers = IdentifierDirectory(mailbox_root)
    relay = ChannelRelay()
    relay.status.update({
        "host": arguments.host,
        "port": arguments.port,
        "statusFile": str(status_file),
        "mailboxDirectory": str(mailbox_root),
        "configDirectory": str(configuration_root),
        "publicUrl": os.environ[PUBLIC_URL_ENV],
        "maxAttachmentBytes": arguments.max_attachment_mb * 1024 * 1024,
        "maxAttachmentStorageBytes": arguments.max_attachment_storage_mb * 1024 * 1024,
        "maxJsonlBytes": arguments.max_jsonl_mb * 1024 * 1024,
        "maxSqliteBytes": arguments.max_sqlite_mb * 1024 * 1024,
    })

    class HealthHandler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            required = os.environ.get(TOKEN_ENV, "")
            if not required:
                return True
            supplied = self.headers.get("Authorization", "")
            if supplied == f"Bearer {required}":
                return True
            self._json(401, {"error": "REST Bearer token is required"})
            return False

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, content_type: str) -> None:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/v1/webhooks/whatsapp", "/v1/webhooks/facebook-messenger"}:
                token_env = ("WHATSAPP_VERIFY_TOKEN" if parsed.path.endswith("whatsapp")
                             else "FACEBOOK_VERIFY_TOKEN")
                try:
                    challenge = verify_challenge(parse_qs(parsed.query), os.environ.get(token_env, ""))
                except ValueError as error:
                    self._json(403, {"error": str(error)})
                    return
                body = challenge.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path.startswith(ATTACHMENT_PREFIX):
                relative_text = unquote(parsed.path[len(ATTACHMENT_PREFIX):])
                attachment_root = (mailbox_root / "attachments").resolve()
                requested = (attachment_root / Path(relative_text)).resolve()
                try:
                    requested.relative_to(attachment_root)
                except ValueError:
                    self._json(403, {"error": "attachment path is outside the mailbox"})
                    return
                if not requested.is_file():
                    self._json(404, {"error": "attachment not found"})
                    return
                content_type = mimetypes.guess_type(requested.name)[0] or "application/octet-stream"
                self._file(requested, content_type)
                return
            if parsed.path == "/agent_mailbox.py":
                self._file(PACKAGE_ROOT / "agent_mailbox.py", "text/x-python; charset=utf-8")
                return
            if parsed.path == "/AUTOMATION_PROMPT.md":
                self._file(RESOURCE_ROOT / "AUTOMATION_PROMPT.md", "text/markdown; charset=utf-8")
                return
            if parsed.path == "/INSTALL_WITH_CODEX.md":
                self._file(RESOURCE_ROOT / "INSTALL_WITH_CODEX.md", "text/markdown; charset=utf-8")
                return
            if parsed.path in {"/page-demo", "/page-demo/", "/chat", "/chat/"}:
                self._file(RESOURCE_ROOT / "special_websocket_client.html", "text/html; charset=utf-8")
                return
            if not self._authorized():
                return
            if parsed.path == "/v1/chat/ws":
                recipient = parse_qs(parsed.query).get("recipient", [""])[0].strip()
                key = self.headers.get("Sec-WebSocket-Key", "").strip()
                if not recipient or not key or self.headers.get("Upgrade", "").lower() != "websocket":
                    self._json(400, {"error": "WebSocket upgrade and recipient are required"})
                    return
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept_value(key))
                self.end_headers()
                try:
                    handle_chat(self.connection, recipient, agent_mailbox)
                except (ConnectionError, OSError, ValueError, json.JSONDecodeError):
                    pass
                self.close_connection = True
                return
            if parsed.path in {"/", "/health", "/v1/status"}:
                self._json(200, {
                    "service": "mailbox-channel-relay-proxy",
                    "project": "Mailbox Channel Relay Bridging Proxy",
                    "mailbox": agent_mailbox.status(),
                    "outboundRecipient": RELAY_RECIPIENT,
                    "supportedChannelTypes": SUPPORTED_CHANNEL_TYPES,
                    "plannedChannelTypes": PLANNED_CHANNEL_TYPES,
                    **relay.status,
                })
                return
            if parsed.path == "/v1/adapters":
                self._json(200, {
                    "supported": SUPPORTED_CHANNEL_TYPES,
                    "planned": PLANNED_CHANNEL_TYPES,
                    "capabilities": ADAPTER_CAPABILITIES,
                })
                return
            if parsed.path == "/v1/listeners":
                try:
                    self._json(200, public_registry())
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    self._json(500, {"error": str(error)})
                return
            if parsed.path == "/v1/identifiers":
                query = parse_qs(parsed.query)
                try:
                    entries = identifiers.find(
                        system=query.get("system", [""])[0],
                        identifier=query.get("identifier", [""])[0],
                        text=query.get("text", [""])[0],
                        kind=query.get("kind", [""])[0],
                        limit=int(query.get("limit", ["100"])[0]),
                    )
                    self._json(200, {"identifiers": entries})
                except ValueError as error:
                    self._json(400, {"error": str(error)})
                return
            if parsed.path == "/v1/identifier-resolution-requests":
                query = parse_qs(parsed.query)
                try:
                    self._json(200, {"requests": identifiers.resolution_requests(
                        system=query.get("system", [""])[0],
                        identifier=query.get("identifier", [""])[0],
                    )})
                except ValueError as error:
                    self._json(400, {"error": str(error)})
                return
            if parsed.path == "/v1/messages":
                query = parse_qs(parsed.query)
                recipient = query.get("recipient", [""])[0].strip()
                if not recipient:
                    self._json(400, {"error": "recipient is required"})
                    return
                advance = query.get("advance", ["true"])[0].lower() not in {"0", "false", "no"}
                cursor = query.get("cursor", [""])[0].strip() or None
                messages = agent_mailbox.receive(recipient, advance=advance, cursor=cursor)
                self._json(200, {"messages": [identifiers.enrich(message) for message in messages]})
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            request_path = urlparse(self.path).path
            if request_path in {"/v1/webhooks/whatsapp", "/v1/webhooks/facebook-messenger"}:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                secret_env = ("WHATSAPP_APP_SECRET" if request_path.endswith("whatsapp")
                              else "FACEBOOK_APP_SECRET")
                if not verify_signature(body, self.headers.get("X-Hub-Signature-256", ""),
                                        os.environ.get(secret_env, "")):
                    self._json(401, {"error": "invalid Meta webhook signature"})
                    return
                try:
                    payload = json.loads(body.decode("utf-8"))
                    adapter = relay.whatsapp if request_path.endswith("whatsapp") else relay.facebook_messenger
                    adapter.handle_webhook(payload, agent_mailbox)
                except (ValueError, OSError, json.JSONDecodeError) as error:
                    self._json(400, {"error": str(error)})
                    return
                self._json(200, {"accepted": True})
                return
            if not self._authorized():
                return
            if request_path not in {"/v1/messages", "/v1/ack", "/v1/identifiers",
                                     "/v1/identifier-resolution-requests"}:
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if request_path == "/v1/identifiers":
                    entries = payload.get("entries") if isinstance(payload, dict) else None
                    if entries is None:
                        entries = [payload]
                    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
                        raise ValueError("entries must be a list of identifier records")
                    self._json(201, {"identifiers": identifiers.remember_many(entries)})
                    return
                if request_path == "/v1/identifier-resolution-requests":
                    result = identifiers.request_resolution(
                        str(payload.get("system") or ""), str(payload.get("identifier") or ""),
                        resolver=str(payload.get("resolver") or ""), force=bool(payload.get("force")),
                    )
                    self._json(201, {"request": result})
                    return
                if request_path == "/v1/ack":
                    recipient = str(payload.get("recipient") or "").strip()
                    message_id = str(payload.get("message_id") or "").strip()
                    if not recipient or not message_id:
                        raise ValueError("recipient and message_id are required")
                    self._json(200, {"acknowledged": agent_mailbox.acknowledge(
                        recipient, message_id, cursor=str(payload.get("cursor") or "") or None,
                    )})
                    return
                recipient = str(payload.get("to") or "").strip()
                if not recipient:
                    raise ValueError("to is required")
                message = agent_mailbox.send(
                    recipient,
                    str(payload.get("text") or ""),
                    sender=str(payload.get("from") or agent_mailbox.DEFAULT_SENDER),
                    message_type=str(payload.get("type") or "message"),
                    attachments=[Path(item) for item in payload.get("attachments") or []],
                    channel_id=payload.get("channel_id"),
                    channel_type=payload.get("channel_type"),
                    source_id=payload.get("source_id"),
                    thread_id=payload.get("thread_id"),
                    root_id=payload.get("root_id"),
                    extra_fields={key: value for key, value in payload.items() if key not in {
                        "to", "text", "from", "type", "attachments", "channel_id", "channel_type",
                        "source_id", "thread_id", "root_id",
                    }},
                )
            except (ValueError, OSError, json.JSONDecodeError) as error:
                self._json(400, {"error": str(error)})
                return
            self._json(201, {"message": identifiers.enrich(message)})

        def log_message(self, _format: str, *_args) -> None:
            return

    try:
        health_server = ThreadingHTTPServer((arguments.host, arguments.port), HealthHandler)
    except OSError as error:
        print(f"Cannot bind channel relay to {arguments.host}:{arguments.port}: {error}", file=sys.stderr)
        return 3
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    health_thread = threading.Thread(target=health_server.serve_forever, name="mattermost-relay-health", daemon=True)
    health_thread.start()

    def stop(_signum=None, _frame=None) -> None:
        relay.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        run_relay_supervisor(relay)
        return 0
    finally:
        try:
            health_server.shutdown()
            health_server.server_close()
        except Exception as error:
            relay.status["shutdownError"] = str(error)
        relay.status.update({"running": False, "connected": False})
        _safe_write_status(relay)
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
