"""Run Mailbox Channel Relay Bridging Proxy as a standalone daemon."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv


PACKAGE_ROOT = Path(__file__).resolve().parent
RESOURCE_ROOT = PACKAGE_ROOT / "resources"
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
DEFAULT_HOST = "127.0.0.1"
HOST_ENV = "MAILBOX_RELAY_HOST"
PORT_ENV = "MAILBOX_RELAY_PORT"
TOKEN_ENV = "MAILBOX_RELAY_TOKEN"
# Exit code the server returns to ask its supervisor loop to quit instead of
# relaunching. Any other exit is treated by mailbox-server-loop as "restart".
SHUTDOWN_EXIT_CODE = 42
from .mailbox_relay import (
    ADAPTER_CAPABILITIES, ChannelRelay, PLANNED_MAILBOX_TYPES, RELAY_PORT, RELAY_RECIPIENT,
    SUPPORTED_MAILBOX_TYPES,
)
from . import agent_mailbox
from .connector_registry import CONFIG_DIR_ENV, config_dir, public_registry
from .websocket_chat import accept_value, handle_chat
from .attachment_gateway import ATTACHMENT_PREFIX, PUBLIC_URL_ENV
from .attachment_storage import (
    DEFAULT_MAX_FILE_BYTES, DEFAULT_MAX_STORAGE_BYTES, MAX_FILE_ENV, MAX_STORAGE_ENV,
)
from .sqlite_limits import DEFAULT_MAX_SQLITE_BYTES, MAX_SQLITE_ENV
from .identifier_directory import IdentifierDirectory
from .bootstrap_config import BOOTSTRAP_CONFIG_DIR_ENV, bootstrap_mailbox_state
from .meta_webhooks import verify_challenge, verify_signature
from .subscriptions import set_subscription, subscriptions
from .cmd_client_page import (
    command_status, render_cmd_client_page, start_mailbox_client, stop_mailbox_client,
)
from .ws_collab_api import (
    WS_COLLAB_MOUNT,
    handle_ws_collab_delete,
    handle_ws_collab_get,
    handle_ws_collab_post,
    is_ws_collab_path,
    is_ws_collab_ws_path,
    run_ws_collab_websocket,
)


def runtime_paths(port: int, mailbox_root: Path | None = None) -> tuple[Path, Path, Path]:
    runtime_dir = (mailbox_root or agent_mailbox.mailbox_dir()) / "runtime" / f"mailbox-relay-{port}"
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


def _mailbox_listing_payload(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = registry if isinstance(registry, dict) else public_registry()
    mailboxes = list(snapshot.get("mailboxes") or [])
    return {
        "version": int(snapshot.get("version") or 1),
        "mailboxes": mailboxes,
        "count": len(mailboxes),
    }


def _service_listing_payload(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = registry if isinstance(registry, dict) else public_registry()
    connectors = list(snapshot.get("connectors") or [])
    services = [
        {
            "id": str(connector.get("id") or ""),
            "adapter": str(connector.get("adapter") or ""),
            "instance": str(connector.get("instance") or ""),
            "enabled": bool(connector.get("enabled")),
            "can_listen": bool(connector.get("can_listen")),
            "can_send": bool(connector.get("can_send")),
            "mailbox_ids": list(connector.get("mailbox_ids") or []),
        }
        for connector in connectors
    ]
    return {
        "version": int(snapshot.get("version") or 1),
        "services": services,
        "count": len(services),
    }


def run_relay_supervisor(relay: ChannelRelay, *, sleep=time.sleep, verbose: int = 0) -> None:
    """Keep adapters alive until explicitly stopped, despite recoverable failures."""
    retry_delay = 1.0
    configured = False
    relay.status["running"] = True
    while not relay.stop_requested:
        try:
            if not configured:
                relay.configure()
                configured = True
            if relay.status.get("enabled") and not relay.status.get("maintenancePaused"):
                relay.status["cycleActive"] = True
                try:
                    relay.cycle()
                finally:
                    relay.status["cycleActive"] = False
            retry_delay = 1.0
            relay.status["supervisorError"] = None
            _safe_write_status(relay)
            sleep(1.0)
        except Exception as error:
            if verbose >= 1:
                print(
                    f"Relay supervisor {type(error).__name__}: {error}; retrying in {retry_delay:g}s",
                    file=sys.stderr,
                    flush=True,
                )
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
        prog="mailbox-server",
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
    parser.add_argument("--config-dir", type=Path, help="directory containing .env and mailboxes.json")
    parser.add_argument(
        "--bootstrap-config-dir", type=Path,
        help="directory containing presence/system bootstrap JSON (environment: "
             f"{BOOTSTRAP_CONFIG_DIR_ENV}; default: <mailbox-dir>/config)",
    )
    parser.add_argument(
        "--public-address", "--public-url", dest="public_url", default=os.environ.get(PUBLIC_URL_ENV),
        help=f"externally reachable base URL advertised to clients and used for attachment links "
             f"(environment: {PUBLIC_URL_ENV})",
    )
    parser.add_argument("--token", help=f"require this REST Bearer token (or {TOKEN_ENV})")
    parser.add_argument(
        "-v", "--verbose", nargs="?", const=1, default=0, type=int, choices=range(3),
        metavar="LEVEL",
        help="logging detail: 0=errors only, 1=lifecycle and retries, 2=include HTTP requests; "
             "using --verbose without LEVEL selects 1",
    )
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
        help=f"maximum JSON message store size in MiB (environment: {agent_mailbox.MAX_JSONL_ENV}; default: 5120)",
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
    bootstrap_report = bootstrap_mailbox_state(
        mailbox_root,
        config_dir=arguments.bootstrap_config_dir,
    )
    public_url = arguments.public_url
    if not public_url:
        advertised_host = "127.0.0.1" if arguments.host in {"127.0.0.1", "localhost"} else socket.getfqdn()
        public_url = f"http://{advertised_host}:{arguments.port}"
    os.environ[PUBLIC_URL_ENV] = public_url.rstrip("/")
    runtime_dir, pid_file, status_file = runtime_paths(arguments.port, mailbox_root)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    identifiers = IdentifierDirectory(mailbox_root)
    relay = ChannelRelay(verbose=arguments.verbose)
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
        "verboseLevel": arguments.verbose,
        "maintenancePaused": False,
        "cycleActive": False,
        "bootstrap": bootstrap_report,
    })

    restart_state = {"exit_code": 0}

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

        def _html(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _request_exit(self, exit_code: int, payload: dict) -> None:
            # Ack first, then ask the supervisor to return so the process exits.
            # Under mailbox-server-loop, exit code 0 relaunches a fresh process
            # (reimporting every module, so edited code takes effect); the
            # shutdown code tells the loop to quit instead of relaunching.
            restart_state["exit_code"] = exit_code
            relay.status["restartRequested"] = (exit_code == 0)
            _safe_write_status(relay)
            self._json(200, {**payload, "pid": os.getpid()})
            try:
                self.wfile.flush()
            except OSError:
                pass
            relay.stop()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if is_ws_collab_ws_path(parsed.path):
                key = self.headers.get("Sec-WebSocket-Key", "").strip()
                if not key or self.headers.get("Upgrade", "").lower() != "websocket":
                    self._json(400, {"error": "WebSocket upgrade is required"})
                    return
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept_value(key))
                self.end_headers()
                try:
                    run_ws_collab_websocket(
                        self.connection,
                        root=mailbox_root,
                        client_ip=(self.client_address[0] if self.client_address else None),
                        required_token=os.environ.get(TOKEN_ENV, ""),
                    )
                except (ConnectionError, OSError, ValueError, json.JSONDecodeError):
                    pass
                self.close_connection = True
                return
            if parsed.path == WS_COLLAB_MOUNT or parsed.path.startswith(WS_COLLAB_MOUNT + "/"):
                status, payload = handle_ws_collab_get(parsed.path, parsed.query, root=mailbox_root) or (
                    404, {"error": "not found"}
                )
                self._json(status, payload)
                return
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
            if parsed.path in {"/api/restart", "/api/shutdown"}:
                if not self._authorized():
                    return
                if parsed.path == "/api/restart":
                    self._request_exit(0, {"restarting": True,
                        "note": "process exits now; loop relaunches with the updated code"})
                else:
                    self._request_exit(SHUTDOWN_EXIT_CODE, {"stopping": True,
                        "note": "process exits now; loop quits"})
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
            if parsed.path == "/mailbox_store.py":
                self._file(PACKAGE_ROOT / "mailbox_store.py", "text/x-python; charset=utf-8")
                return
            if parsed.path in {"/CODEX_AUTOMATION.md", "/AUTOMATION_PROMPT.md"}:
                self._file(RESOURCE_ROOT / "CODEX_AUTOMATION.md", "text/markdown; charset=utf-8")
                return
            if parsed.path in {"/CODEX_INSTALL.md", "/INSTALL_WITH_CODEX.md"}:
                # Install is consolidated into CODEX_AUTOMATION.md; keep these
                # paths as backward-compatible aliases.
                self._file(RESOURCE_ROOT / "CODEX_AUTOMATION.md", "text/markdown; charset=utf-8")
                return
            if parsed.path.endswith(".md") and parsed.path.count("/") == 1:
                name = parsed.path[1:]
                candidate = (RESOURCE_ROOT / name).resolve()
                if candidate.parent == RESOURCE_ROOT.resolve() and candidate.is_file():
                    self._file(candidate, "text/markdown; charset=utf-8")
                else:
                    self._json(404, {"error": "not found"})
                return
            if parsed.path in {"/page-demo", "/page-demo/", "/chat", "/chat/"}:
                self._file(RESOURCE_ROOT / "special_websocket_client.html", "text/html; charset=utf-8")
                return
            if not self._authorized():
                return
            if (relay.status.get("maintenancePaused")
                    and parsed.path not in {"/", "/health", "/v1/status"}):
                self._json(423, {"error": "mailbox server is paused for maintenance"})
                return
            if parsed.path in {"/cmd-client", "/cmd-client/"}:
                arguments = parse_qs(parsed.query, keep_blank_values=True).get("args", [""])[0]
                session = None
                error = ""
                if arguments.strip():
                    try:
                        session = start_mailbox_client(
                            arguments,
                            relay_url=str(relay.status["publicUrl"]),
                            token=os.environ.get(TOKEN_ENV, ""),
                        )
                    except (OSError, RuntimeError, ValueError) as caught:
                        error = str(caught)
                self._html(200, render_cmd_client_page(arguments, session, error))
                return
            if parsed.path == "/cmd-client/output":
                session_id = parse_qs(parsed.query).get("session", [""])[0]
                status = command_status(session_id)
                self._json(200, status) if status else self._json(404, {"error": "session not found"})
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
                    "service": "mailbox-mailbox-relay-proxy",
                    "project": "Mailbox Channel Relay Bridging Proxy",
                    "mailbox": agent_mailbox.status(),
                    "outboundRecipient": RELAY_RECIPIENT,
                    "cmdClient": "/cmd-client/",
                    "supportedChannelTypes": SUPPORTED_MAILBOX_TYPES,
                    "plannedChannelTypes": PLANNED_MAILBOX_TYPES,
                    **relay.status,
                })
                return
            if parsed.path == "/v1/adapters":
                self._json(200, {
                    "supported": SUPPORTED_MAILBOX_TYPES,
                    "planned": PLANNED_MAILBOX_TYPES,
                    "capabilities": ADAPTER_CAPABILITIES,
                })
                return
            if parsed.path == "/v1/discovery/chats":
                query = parse_qs(parsed.query)
                platform = query.get("platform", [""])[0].strip().lower()
                instance = query.get("instance", [""])[0].strip()
                try:
                    timeout = float(query.get("timeout", ["30"])[0])
                    if platform == "irc":
                        from .discovery_admin import discover_irc_chats
                        chats = discover_irc_chats(instance=instance, timeout=timeout)
                    elif platform == "discord":
                        from .discovery_admin import discover_discord_chats
                        chats = discover_discord_chats(instance=instance, timeout=timeout)
                    else:
                        raise ValueError("chat discovery currently supports platform=irc or platform=discord")
                    self._json(200, {"platform": platform, "chats": chats})
                except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                    self._json(400, {"error": str(error)})
                return
            if parsed.path == "/v1/discovery/users":
                query = parse_qs(parsed.query)
                platform = query.get("platform", [""])[0].strip().lower()
                mailbox = query.get("mailbox", [""])[0].strip()
                instance = query.get("instance", [""])[0].strip()
                try:
                    timeout = float(query.get("timeout", ["15"])[0])
                    if not mailbox:
                        raise ValueError("mailbox is required")
                    if platform == "irc":
                        from .discovery_admin import discover_irc_users
                        users = discover_irc_users(mailbox, instance=instance, timeout=timeout)
                    elif platform == "discord":
                        from .discovery_admin import discover_discord_users
                        users = discover_discord_users(mailbox, instance=instance, timeout=timeout)
                    else:
                        raise ValueError("user discovery currently supports platform=irc or platform=discord")
                    self._json(200, {"platform": platform, "mailbox": mailbox, "users": users})
                except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                    self._json(400, {"error": str(error)})
                return
            if parsed.path == "/v1/registry":
                try:
                    self._json(200, public_registry())
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    self._json(500, {"error": str(error)})
                return
            if parsed.path == "/v1/mailboxes":
                try:
                    self._json(200, _mailbox_listing_payload())
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    self._json(500, {"error": str(error)})
                return
            if parsed.path == "/v1/services":
                try:
                    self._json(200, _service_listing_payload())
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    self._json(500, {"error": str(error)})
                return
            if parsed.path == "/v1/subscriptions":
                identity = parse_qs(parsed.query).get("identity", [""])[0].strip()
                if not identity:
                    self._json(400, {"error": "identity is required"})
                    return
                self._json(200, {"identity": identity, "mailboxes": subscriptions(identity)})
                return
            if parsed.path == "/v1/cursors":
                cursor = parse_qs(parsed.query).get("cursor", [""])[0].strip()
                if not cursor:
                    self._json(400, {"error": "cursor is required"})
                    return
                self._json(200, {
                    "cursor": cursor,
                    "recipients": agent_mailbox.cursor_subscriptions(cursor),
                    "positions": agent_mailbox.cursor_positions(cursor),
                })
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
            if request_path == WS_COLLAB_MOUNT or request_path.startswith(WS_COLLAB_MOUNT + "/"):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    if not isinstance(payload, dict):
                        raise ValueError("JSON body must be an object")
                    status, body = handle_ws_collab_post(request_path, payload, root=mailbox_root) or (
                        404, {"error": "not found"}
                    )
                except (ValueError, OSError, json.JSONDecodeError) as error:
                    self._json(400, {"error": str(error)})
                    return
                self._json(status, body)
                return
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
            if request_path == "/v1/webhooks/viber":
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                connector = relay.viber.authenticate_webhook(
                    body, self.headers.get("X-Viber-Content-Signature", ""),
                )
                if connector is None:
                    self._json(401, {"error": "invalid Viber webhook signature"})
                    return
                try:
                    relay.viber.handle_webhook(json.loads(body.decode("utf-8")), agent_mailbox, connector)
                except (ValueError, OSError, json.JSONDecodeError) as error:
                    self._json(400, {"error": str(error)})
                    return
                self._json(200, {"accepted": True})
                return
            if request_path == "/v1/webhooks/line":
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                connector = relay.line.authenticate_webhook(body, self.headers.get("X-Line-Signature", ""))
                if connector is None:
                    self._json(401, {"error": "invalid LINE webhook signature"})
                    return
                try:
                    relay.line.handle_webhook(json.loads(body.decode("utf-8")), agent_mailbox, connector)
                except (ValueError, OSError, json.JSONDecodeError) as error:
                    self._json(400, {"error": str(error)})
                    return
                self._json(200, {"accepted": True})
                return
            if request_path == "/v1/webhooks/discourse":
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                connector = relay.discourse.authenticate_webhook(
                    body, self.headers.get("X-Discourse-Event-Signature", ""),
                )
                if connector is None:
                    self._json(401, {"error": "invalid Discourse webhook signature"})
                    return
                try:
                    relay.discourse.handle_webhook(
                        json.loads(body.decode("utf-8")), agent_mailbox, connector,
                        event_id=self.headers.get("X-Discourse-Event-Id", ""),
                        event_name=self.headers.get("X-Discourse-Event", ""),
                    )
                except (ValueError, OSError, json.JSONDecodeError) as error:
                    self._json(400, {"error": str(error)})
                    return
                self._json(200, {"accepted": True})
                return
            if request_path == "/v1/webhooks/whatsapp-personal":
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                connector = relay.whatsapp_personal.authenticate_webhook(
                    body, self.headers.get("X-WhatsApp-Personal-Signature", ""),
                )
                if connector is None:
                    self._json(401, {"error": "invalid WhatsApp Personal webhook signature"})
                    return
                try:
                    relay.whatsapp_personal.handle_webhook(
                        json.loads(body.decode("utf-8")), agent_mailbox, connector,
                    )
                except (ValueError, OSError, json.JSONDecodeError) as error:
                    self._json(400, {"error": str(error)})
                    return
                self._json(200, {"accepted": True})
                return
            if not self._authorized():
                return
            if request_path == "/v1/maintenance/pause":
                relay.status["maintenancePaused"] = True
                deadline = time.monotonic() + 40
                while relay.status.get("cycleActive") and time.monotonic() < deadline:
                    time.sleep(0.05)
                if relay.status.get("cycleActive"):
                    self._json(503, {"error": "relay cycle did not quiesce before timeout"})
                    return
                _safe_write_status(relay)
                self._json(200, {"paused": True, "cycleActive": False})
                return
            if request_path == "/v1/maintenance/resume":
                relay.status["maintenancePaused"] = False
                _safe_write_status(relay)
                self._json(200, {"paused": False})
                return
            if request_path in {"/api/restart", "/api/shutdown"}:
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
                if request_path == "/api/restart":
                    self._request_exit(0, {"restarting": True,
                        "note": "process exits now; loop relaunches with the updated code"})
                else:
                    self._request_exit(SHUTDOWN_EXIT_CODE, {"stopping": True,
                        "note": "process exits now; loop quits"})
                return
            if request_path in {
                "/v1/maintenance/trim", "/v1/maintenance/trim-before",
                "/v1/maintenance/trim-to-size",
            }:
                if not relay.status.get("maintenancePaused") or relay.status.get("cycleActive"):
                    self._json(409, {"error": "pause and quiesce the server before trimming"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    from datetime import datetime, timedelta, timezone
                    cutoff = None
                    max_bytes = None
                    if request_path != "/v1/maintenance/trim-to-size" and payload.get("before"):
                        cutoff = datetime.fromisoformat(
                            str(payload["before"]).replace("Z", "+00:00")
                        )
                    elif request_path != "/v1/maintenance/trim-to-size" and (
                        request_path == "/v1/maintenance/trim-before"
                        or payload.get("older_than_seconds") is not None
                    ):
                        cutoff = datetime.now(timezone.utc) - timedelta(
                            seconds=float(payload.get("older_than_seconds", 172800))
                        )
                    elif request_path == "/v1/maintenance/trim" and payload.get("max_bytes") is None:
                        cutoff = datetime.now(timezone.utc) - timedelta(seconds=172800)
                    if request_path == "/v1/maintenance/trim-to-size" or payload.get("max_bytes") is not None:
                        max_bytes = int(payload.get("max_bytes"))
                    mailboxes = payload.get("mailboxes") or []
                    if not isinstance(mailboxes, list):
                        raise ValueError("mailboxes must be an array; omit it to trim all mailboxes")
                    result = agent_mailbox.trim_messages(
                        cutoff=cutoff, max_bytes=max_bytes,
                        mailboxes={str(item) for item in mailboxes},
                    )
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    self._json(400, {"error": str(error)})
                    return
                self._json(200, result)
                return
            if relay.status.get("maintenancePaused"):
                self._json(423, {"error": "mailbox server is paused for maintenance"})
                return
            if request_path == "/cmd-client/stop":
                session_id = parse_qs(urlparse(self.path).query).get("session", [""])[0]
                self._json(200, {"stopped": stop_mailbox_client(session_id)})
                return
            if request_path not in {"/v1/messages", "/v1/ack", "/v1/cursors", "/v1/agents", "/v1/identifiers",
                                     "/v1/identifier-resolution-requests",
                                     "/v1/subscriptions", "/v1/irc/command",
                                     "/v1/mm/command", "/v1/connectors", "/v1/mailboxes",
                                     "/v1/relays", "/v1/mailboxes", "/v1/exec"}:
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if request_path == "/v1/mm/command":
                    from .adapters.mattermost_adapter import execute, remember_named_ids
                    if not relay.status.get("enabled"):
                        raise ValueError("Mattermost adapter is not enabled")
                    if not relay.status.get("connected"):
                        relay._connect()
                    registry_count_before = identifiers.entry_count()
                    result = execute(str(payload.get("command") or ""),
                                     dict(payload.get("arguments") or {}),
                                     session=relay.session,
                                     base_url=relay.base_url,
                                     directory=identifiers)
                    subscription_result = None
                    arguments = dict(payload.get("arguments") or {})
                    if (str(payload.get("command") or "") == "join"
                            and bool(arguments.get("subscribe_all"))):
                        from .adapters.mattermost_adapter import resolve_address
                        from .connector_registry import load_agents
                        from .connector_registry import add_connector_mailbox
                        from .endpoint_address import mailbox_resource_id, endpoint_instance
                        from .subscriptions import ensure_mailbox, set_subscription

                        address = resolve_address(
                            str(arguments.get("mailbox") or ""), identifiers,
                            base_url=relay.base_url,
                        )
                        mailbox_id = address.rsplit("/", 1)[-1]
                        add_connector_mailbox("mattermost", "0", mailbox_id)
                        identity = relay._mattermost_mailbox_identity(mailbox_id)
                        instance = endpoint_instance("mattermost", relay.connector)
                        resource_id = mailbox_resource_id(
                            "mattermost", instance, mailbox_id,
                        )
                        ensure_mailbox(resource_id, metadata={
                            "connector": relay.connector.get("id", ""),
                            "endpoint": instance,
                            "external_address": f"mm/{instance}/{mailbox_id}",
                            **identity,
                        })
                        agent_ids = [agent["agent_id"] for agent in load_agents()]
                        for agent_id in agent_ids:
                            set_subscription(resource_id, agent_id, enabled=True)
                            agent_mailbox.ensure_cursor(
                                resource_id, cursor=agent_id, start="now",
                            )
                        subscription_result = {
                            "mailbox": resource_id,
                            "subscribed_agents": agent_ids,
                            "count": len(agent_ids),
                        }
                    remember_named_ids(identifiers, relay.base_url, result)
                    registry_count_after = identifiers.entry_count()
                    self._json(200, {"result": result, "registry": {
                        "new_entries": registry_count_after - registry_count_before,
                        "total_entries": registry_count_after,
                    }, **({"subscriptions": subscription_result}
                          if subscription_result is not None else {})})
                    return
                if request_path == "/v1/irc/command":
                    line = str(payload.get("line") or "").replace("\r", " ").replace("\n", " ").strip()
                    if not line:
                        raise ValueError("IRC command line is required")
                    adapter = relay.irc_adapter_for(
                        connector_id=str(payload.get("connector_id") or ""),
                        instance=str(payload.get("instance") or ""),
                    )
                    if adapter is None or not adapter.status.get("enabled"):
                        raise ValueError("IRC adapter is not enabled")
                    if not adapter.connection:
                        adapter.connect()
                    adapter._write(line)
                    self._json(202, {"accepted": True, "line": line.split(" ", 1)[0].upper()})
                    return
                if request_path == "/v1/subscriptions":
                    mailbox = str(payload.get("mailbox") or "")
                    if mailbox.lower().startswith(("mm/", "mattermost/")):
                        from .adapters.mattermost_adapter import resolve_address
                        mailbox = resolve_address(mailbox, identifiers,
                                                  base_url=relay.base_url)
                    result = set_subscription(
                        mailbox, str(payload.get("identity") or ""),
                        enabled=bool(payload.get("subscribed", True)),
                    )
                    self._json(200, result)
                    return
                if request_path == "/v1/identifiers":
                    entries = payload.get("entries") if isinstance(payload, dict) else None
                    if entries is None:
                        entries = [payload]
                    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
                        raise ValueError("entries must be a list of identifier records")
                    saved = []
                    for entry in entries:
                        values = {key: value for key, value in entry.items()
                                  if key != "friendly_alias"}
                        saved.append(
                            identifiers.remember_alias(**values)
                            if entry.get("friendly_alias") else identifiers.remember(**values)
                        )
                    self._json(201, {"identifiers": saved})
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
                if request_path == "/v1/cursors":
                    recipient = str(payload.get("recipient") or "").strip()
                    cursor = str(payload.get("cursor") or "").strip()
                    if not recipient or not cursor:
                        raise ValueError("recipient and cursor are required")
                    self._json(201, agent_mailbox.initialize_cursor(
                        recipient, cursor=cursor, start=str(payload.get("start") or "now"),
                    ))
                    return
                if request_path == "/v1/agents":
                    from .connector_registry import register_agent, unregister_agent
                    if str(payload.get("action") or "").lower() == "delete":
                        result = unregister_agent(
                            str(payload.get("agent_id") or ""),
                            presence_id=str(payload.get("presence_id") or ""),
                            purge=bool(payload.get("purge", False)),
                            dry_run=bool(payload.get("dry_run", False)),
                        )
                        self._json(200, result)
                    else:
                        result = register_agent(
                            str(payload.get("agent_id") or ""),
                            presence_id=str(payload.get("presence_id") or ""),
                            dry_run=bool(payload.get("dry_run", False)),
                        )
                        created = bool(
                            result.get("agent_created") or result.get("presence_created")
                        )
                        self._json(201 if created else 200, result)
                    return
                if request_path == "/v1/connectors":
                    from .connector_registry import register_connector, unregister_connector
                    if str(payload.get("action") or "").lower() == "delete":
                        self._json(200, unregister_connector(
                            str(payload.get("connector_id") or ""),
                            dry_run=bool(payload.get("dry_run", False)),
                        ))
                    else:
                        result = register_connector(
                            str(payload.get("connector_id") or ""),
                            str(payload.get("kind") or ""),
                            direction=str(payload.get("direction") or "bidirectional"),
                            instance=str(payload.get("instance") or ""),
                            mailbox_ids=list(payload.get("mailbox_ids") or []),
                            config=dict(payload.get("config") or {}),
                            dry_run=bool(payload.get("dry_run", False)),
                        )
                        self._json(200 if payload.get("dry_run") else 201, result)
                    return
                if request_path == "/v1/mailboxes":
                    from .subscriptions import delete_mailbox, ensure_mailbox, set_subscription
                    from .connector_registry import add_connector_mailbox, load_agents
                    from .endpoint_address import parse_endpoint
                    mailbox_id = str(payload.get("mailbox_id") or "")
                    if str(payload.get("action") or "").lower() == "delete":
                        self._json(200, delete_mailbox(
                            mailbox_id, force=bool(payload.get("force", False)),
                        ))
                        return
                    metadata = dict(payload.get("metadata") or {})
                    aliases = [str(item) for item in (payload.get("aliases") or [])]
                    endpoint = parse_endpoint(mailbox_id)
                    monitored = (add_connector_mailbox(
                        endpoint.adapter, endpoint.instance, endpoint.identifier,
                    ) if endpoint else None)
                    mailbox = ensure_mailbox(mailbox_id, metadata=metadata, aliases=aliases)
                    result = mailbox
                    if bool(payload.get("subscribe_all", False)):
                        agent_ids = [agent["agent_id"] for agent in load_agents()]
                        for agent_id in agent_ids:
                            set_subscription(mailbox["id"], agent_id, enabled=True)
                            agent_mailbox.ensure_cursor(mailbox["id"], cursor=agent_id, start="now")
                        result = {
                            "mailbox": mailbox,
                            **({"connector": monitored} if monitored else {}),
                            "subscribed_agents": agent_ids, "count": len(agent_ids),
                        }
                    self._json(201, result)
                    return
                if request_path == "/v1/relays":
                    from .retained_relay import add_relay, delete_relay
                    if str(payload.get("action") or "").lower() == "delete":
                        self._json(200, delete_relay(
                            str(payload.get("relay_id") or ""),
                            dry_run=bool(payload.get("dry_run", False)),
                        ))
                    else:
                        result = add_relay(
                            str(payload.get("source_mailbox") or ""),
                            str(payload.get("destination") or ""),
                            relay_id=str(payload.get("relay_id") or ""),
                            start=str(payload.get("start") or "now"),
                            dry_run=bool(payload.get("dry_run", False)),
                        )
                        self._json(200 if payload.get("dry_run") else 201, result)
                    return
                if request_path == "/v1/mailboxes":
                    from .mailbox_registry import register_mailbox, unregister_mailbox
                    if str(payload.get("action") or "").lower() == "delete":
                        self._json(200, unregister_mailbox(
                            str(payload.get("mailbox_id") or ""),
                            dry_run=bool(payload.get("dry_run", False)),
                        ))
                    else:
                        result = register_mailbox(
                            str(payload.get("mailbox_id") or ""),
                            str(payload.get("location") or ""),
                            dry_run=bool(payload.get("dry_run", False)),
                        )
                        self._json(200 if payload.get("dry_run") else 201, result)
                    return
                if request_path == "/v1/exec":
                    program = str(payload.get("program") or "mailbox-client")
                    argv = [str(item) for item in (payload.get("argv") or [])]
                    priority = str(payload.get("priority") or "now").lower()
                    exec_env = {str(key): str(value)
                                for key, value in (payload.get("env") or {}).items()}
                    exec_cwd = str(payload.get("cwd") or "")
                    if priority in {"enque", "follow-enque"}:
                        record = agent_mailbox.send(
                            agent_mailbox.SERVER_WORKER_QUEUE,
                            shlex.join(argv), message_type="work_item",
                            extra_fields={"program": program, "argv": argv, "priority": "enque",
                                          **({"env": exec_env} if exec_env else {}),
                                          **({"cwd": exec_cwd} if exec_cwd else {})},
                        )
                        self._json(201, {"enqueued": True,
                                         "queue": agent_mailbox.SERVER_WORKER_QUEUE,
                                         "message": record})
                        return
                    if program != "mailbox-client":
                        self._json(400, {"error": f"unsupported exec program: {program}"})
                        return
                    from .cmd_client_page import run_mailbox_client_argv
                    try:
                        result = run_mailbox_client_argv(
                            argv, relay_url=str(relay.status["publicUrl"]),
                            token=os.environ.get(TOKEN_ENV, ""), env=exec_env,
                            cwd=exec_cwd or None,
                            timeout=float(payload.get("timeout") or 120),
                        )
                    except subprocess.TimeoutExpired as error:
                        self._json(504, {"error": f"exec timed out: {error}"})
                        return
                    except (OSError, ValueError) as error:
                        self._json(400, {"error": f"exec failed: {error}"})
                        return
                    self._json(200, result)
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
                    mailbox_id=payload.get("mailbox_id"),
                    mailbox_type=payload.get("mailbox_type"),
                    source_id=payload.get("source_id"),
                    thread_id=payload.get("thread_id"),
                    root_id=payload.get("root_id"),
                    extra_fields={key: value for key, value in payload.items() if key not in {
                        "to", "text", "from", "type", "attachments", "mailbox_id", "mailbox_type",
                        "source_id", "thread_id", "root_id",
                    }},
                )
            except (ValueError, OSError, json.JSONDecodeError) as error:
                self._json(400, {"error": str(error)})
                return
            self._json(201, {"message": identifiers.enrich(message)})

        def do_DELETE(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == WS_COLLAB_MOUNT or parsed.path.startswith(WS_COLLAB_MOUNT + "/"):
                status, payload = handle_ws_collab_delete(parsed.path, parsed.query, root=mailbox_root) or (
                    404, {"error": "not found"}
                )
                self._json(status, payload)
                return
            if not self._authorized():
                return
            self._json(404, {"error": "not found"})

        def log_message(self, _format: str, *_args) -> None:
            if arguments.verbose >= 2:
                super().log_message(_format, *_args)

    try:
        health_server = ThreadingHTTPServer((arguments.host, arguments.port), HealthHandler)
    except OSError as error:
        print(f"Cannot bind mailbox relay to {arguments.host}:{arguments.port}: {error}", file=sys.stderr)
        return 3
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    health_thread = threading.Thread(target=health_server.serve_forever, name="mattermost-relay-health", daemon=True)
    health_thread.start()
    if arguments.verbose >= 1:
        print(
            f"Mailbox relay listening on {arguments.host}:{arguments.port} "
            f"(public URL: {os.environ[PUBLIC_URL_ENV]})",
            file=sys.stderr,
            flush=True,
        )

    def stop(_signum=None, _frame=None) -> None:
        relay.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        run_relay_supervisor(relay, verbose=arguments.verbose)
        return restart_state["exit_code"]
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
