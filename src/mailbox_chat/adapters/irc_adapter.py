"""IRC transport adapter using the Python standard library."""

from __future__ import annotations

import os
import argparse
import json
import socket
import ssl
import time
import uuid
import urllib.request
from typing import Any, Callable

from ..connector_registry import connectors_for
from ..delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from ..endpoint_address import subscription_recipients
from ..attachment_gateway import attachment_url


SocketFactory = Callable[[tuple[str, int], float], socket.socket]


class IrcAdapter:
    def __init__(self, *, socket_factory: SocketFactory = socket.create_connection) -> None:
        self.socket_factory = socket_factory
        self.connection: socket.socket | None = None
        self.connector: dict[str, Any] | None = None
        self.buffer = ""
        self.joined = False
        self.status: dict[str, Any] = {
            "enabled": False,
            "connected": False,
            "lastError": None,
            "channels": [],
        }

    def configure(self) -> bool:
        configured = connectors_for("irc")
        self.connector = configured[0] if configured else None
        self.status["enabled"] = self.connector is not None
        self.status["channels"] = list(self.connector.get("channel_ids", [])) if self.connector else []
        return bool(self.connector)

    def connect(self) -> None:
        if not self.connector:
            raise RuntimeError("No enabled IRC connector is configured")
        server = str(self.connector.get("server") or "").strip()
        if server.startswith("$"):
            server = os.environ.get(server[1:], "").strip()
        if not server:
            raise ValueError("IRC connector requires server")
        use_tls = bool(self.connector.get("tls", True))
        port = int(self.connector.get("port") or (6697 if use_tls else 6667))
        connection = self.socket_factory((server, port), 15)
        if use_tls:
            connection = ssl.create_default_context().wrap_socket(connection, server_hostname=server)
        connection.setblocking(False)
        self.connection = connection
        password = os.environ.get("IRC_PASSWORD", "").strip()
        if password:
            self._write(f"PASS {password}")
        nickname = str(self.connector.get("nickname") or "mailbox-relay")
        self._write(f"NICK {nickname}")
        self._write(f"USER {nickname} 0 * :Mailbox Channel Relay Bridging Proxy")
        self.status.update({"connected": True, "lastError": None})

    def close(self) -> None:
        if self.connection:
            try:
                self.connection.close()
            finally:
                self.connection = None
        self.joined = False
        self.status["connected"] = False

    def _write(self, line: str) -> None:
        if not self.connection:
            raise ConnectionError("IRC adapter is not connected")
        self.connection.sendall((line.replace("\r", " ").replace("\n", " ") + "\r\n").encode("utf-8"))

    def _join_channels(self) -> None:
        if self.joined or not self.connector:
            return
        for channel in self.connector.get("channel_ids", []):
            self._write(f"JOIN {channel}")
        self.joined = True

    def cycle(self, mailbox: Any) -> None:
        if not self.status["enabled"]:
            return
        if not self.connection:
            self.connect()
        while True:
            try:
                chunk = self.connection.recv(65536) if self.connection else b""
            except (BlockingIOError, ssl.SSLWantReadError):
                break
            if not chunk:
                self.close()
                raise ConnectionError("IRC server closed the connection")
            self.buffer += chunk.decode("utf-8", errors="replace")
        while "\r\n" in self.buffer:
            line, self.buffer = self.buffer.split("\r\n", 1)
            self._handle_line(line, mailbox)

    def _handle_line(self, line: str, mailbox: Any) -> None:
        tags: dict[str, str] = {}
        if line.startswith("@"):
            raw_tags, line = line[1:].split(" ", 1)
            tags = dict(item.partition("=")[::2] for item in raw_tags.split(";"))
        if line.startswith("PING "):
            self._write("PONG " + line[5:])
            return
        if " 001 " in line:
            self._join_channels()
            return
        if " PRIVMSG " not in line or not line.startswith(":"):
            if " NOTICE " in line or line.startswith("ERROR ") or (
                line.startswith(":") and len(line.split(" ", 3)) >= 3
                and line.split(" ", 3)[1].isdigit()
            ):
                connector = self.connector or {}
                text = line.partition(" :")[2] or line
                for recipient in subscription_recipients("irc", connector, "status"):
                    mailbox.send(
                        recipient, text, sender="irc:server", message_type="irc_status",
                        channel_id="status", channel_type="irc",
                        extra_fields={"irc_line": line},
                    )
            return
        prefix, remainder = line[1:].split(" PRIVMSG ", 1)
        target, separator, text = remainder.partition(" :")
        if not separator:
            return
        nickname = prefix.split("!", 1)[0]
        own_nick = str((self.connector or {}).get("nickname") or "mailbox-relay")
        if nickname.casefold() == own_nick.casefold():
            return
        connector = self.connector or {}
        recipients = list(dict.fromkeys([
            *([str(connector["bridge_agent"])] if connector.get("bridge_agent") else []),
            *connector.get("mailbox_recipients", []),
            *subscription_recipients("irc", connector, target),
        ]))
        source_id = tags.get("msgid") or f"irc-{uuid.uuid4()}"
        extra_fields = with_origin(
            {"author": nickname, "irc_prefix": prefix},
            adapter="irc",
            connector_id=str(connector.get("id") or ""),
            source_id=source_id,
            channel_id=target,
            presence_id=str(connector.get("presence_id") or ""),
        )
        DeliveryLedger(mailbox.mailbox_dir()).claim(
            extra_fields,
            endpoint_id(
                "irc", connector_id=str(connector.get("id") or ""), channel_id=target,
                presence_id=str(connector.get("presence_id") or ""),
            ),
        )
        for recipient in recipients:
            mailbox.send(
                recipient,
                text,
                sender=f"irc:{nickname}",
                message_type="irc_message",
                channel_id=target,
                channel_type="irc",
                source_id=source_id,
                extra_fields=extra_fields,
            )

    def send_message(self, message: dict[str, Any]) -> None:
        if not self.status["enabled"]:
            raise RuntimeError("IRC adapter is not enabled")
        if not self.connection:
            self.connect()
        target = str(message.get("channel_id") or "").strip()
        if not target:
            raise ValueError("IRC outbound message requires channel_id")
        if target == "status":
            raise ValueError("irc/INSTANCE/status is read-only")
        text = str(message.get("text") or "")
        attachment_lines = [f"Attachment: {attachment_url(record)}" for record in message.get("attachments") or []]
        for line in [*(text.splitlines() or [""]), *attachment_lines]:
            self._write(f"PRIVMSG {target} :{line}")

    def list_channels(self, *, timeout: float = 30.0) -> list[dict[str, Any]]:
        """Use IRC LIST (322/323) to discover visible public channels."""
        if not self.status["enabled"]:
            raise RuntimeError("IRC adapter is not enabled")
        if not self.connection:
            self.connect()
        deadline = time.monotonic() + timeout
        requested = False
        channels: list[dict[str, Any]] = []
        buffer = ""
        while time.monotonic() < deadline:
            try:
                chunk = self.connection.recv(65536) if self.connection else b""
            except (BlockingIOError, ssl.SSLWantReadError):
                time.sleep(0.01)
                continue
            if not chunk:
                raise ConnectionError("IRC server closed the connection during LIST")
            buffer += chunk.decode("utf-8", errors="replace")
            while "\r\n" in buffer:
                line, buffer = buffer.split("\r\n", 1)
                if line.startswith("PING "):
                    self._write("PONG " + line[5:])
                if " 001 " in line and not requested:
                    self._write("LIST")
                    requested = True
                if " 322 " in line:
                    fields = line.split(" ", 5)
                    if len(fields) >= 6:
                        channels.append({
                            "identifier": fields[3],
                            "text": fields[5].removeprefix(":"),
                            "kind": "channel",
                            "metadata": {"visible_users": int(fields[4])},
                        })
                if " 323 " in line:
                    return channels
        raise TimeoutError("IRC LIST did not complete before the timeout")

    def list_channel_users(self, channel: str, *, timeout: float = 15.0) -> list[dict[str, Any]]:
        """Use IRC NAMES (353/366) to discover nicknames visible in one channel."""
        if not self.status["enabled"]:
            raise RuntimeError("IRC adapter is not enabled")
        if not channel.startswith(("#", "&", "+", "!")):
            channel = f"#{channel}"
        if not self.connection:
            self.connect()
        deadline = time.monotonic() + timeout
        requested = False
        users: dict[str, dict[str, Any]] = {}
        buffer = ""
        while time.monotonic() < deadline:
            try:
                chunk = self.connection.recv(65536) if self.connection else b""
            except (BlockingIOError, ssl.SSLWantReadError):
                time.sleep(0.01)
                continue
            if not chunk:
                raise ConnectionError("IRC server closed the connection during NAMES")
            buffer += chunk.decode("utf-8", errors="replace")
            while "\r\n" in buffer:
                line, buffer = buffer.split("\r\n", 1)
                if line.startswith("PING "):
                    self._write("PONG " + line[5:])
                if " 001 " in line and not requested:
                    self._write(f"NAMES {channel}")
                    requested = True
                if " 353 " in line:
                    names = line.partition(" :")[2].split()
                    for decorated in names:
                        nickname = decorated.lstrip("~&@%+")
                        if nickname:
                            users[nickname.casefold()] = {
                                "identifier": nickname, "text": nickname, "kind": "user",
                                "metadata": {"channel": channel,
                                             "status_prefix": decorated[:-len(nickname)]},
                            }
                if " 366 " in line:
                    return list(users.values())
        raise TimeoutError("IRC NAMES did not complete before the timeout")

def protocol_line(args: argparse.Namespace) -> str:
    command = args.command
    if command == "ping":
        return f"PING :{args.token}"
    if command == "join":
        return f"JOIN {args.channel}" + (f" {args.key}" if args.key else "")
    if command == "part":
        return f"PART {args.channel}" + (f" :{args.message}" if args.message else "")
    if command == "topic":
        return f"TOPIC {args.channel}" + (f" :{args.text}" if args.text is not None else "")
    if command == "nick":
        return f"NICK {args.nickname}"
    if command == "whois":
        return f"WHOIS {args.nickname}"
    if command == "mode":
        return " ".join(["MODE", args.target, *args.modes])
    if command == "invite":
        return f"INVITE {args.nickname} {args.channel}"
    if command == "kick":
        return f"KICK {args.channel} {args.nickname}" + (f" :{args.reason}" if args.reason else "")
    if command == "message":
        return f"PRIVMSG {args.target} :{args.text}"
    if command == "notice":
        return f"NOTICE {args.target} :{args.text}"
    if command == "raw":
        return " ".join(args.arguments)
    raise ValueError(f"{command} is handled by IRC discovery")


def post_relay_command(url: str, token: str, line: str) -> dict:
    body = json.dumps({"line": line}).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(
        f"{url.rstrip('/')}/v1/irc/command", data=body, headers=headers, method="POST",
    ), timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))
