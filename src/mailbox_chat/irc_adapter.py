"""IRC transport adapter using the Python standard library."""

from __future__ import annotations

import os
import socket
import ssl
import time
import uuid
from typing import Any, Callable

from .listener_registry import listeners_for
from .delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from .endpoint_address import subscription_recipients
from .attachment_gateway import attachment_url
from .channel_routes import dispatch_routes


SocketFactory = Callable[[tuple[str, int], float], socket.socket]


class IrcAdapter:
    def __init__(self, *, socket_factory: SocketFactory = socket.create_connection) -> None:
        self.socket_factory = socket_factory
        self.connection: socket.socket | None = None
        self.listener: dict[str, Any] | None = None
        self.buffer = ""
        self.joined = False
        self.status: dict[str, Any] = {
            "enabled": False,
            "connected": False,
            "lastError": None,
            "channels": [],
        }

    def configure(self) -> bool:
        configured = listeners_for("irc")
        self.listener = configured[0] if configured else None
        self.status["enabled"] = self.listener is not None
        self.status["channels"] = list(self.listener.get("channel_ids", [])) if self.listener else []
        return bool(self.listener)

    def connect(self) -> None:
        if not self.listener:
            raise RuntimeError("No enabled IRC listener is configured")
        server = str(self.listener.get("server") or "").strip()
        if server.startswith("$"):
            server = os.environ.get(server[1:], "").strip()
        if not server:
            raise ValueError("IRC listener requires server")
        use_tls = bool(self.listener.get("tls", True))
        port = int(self.listener.get("port") or (6697 if use_tls else 6667))
        connection = self.socket_factory((server, port), 15)
        if use_tls:
            connection = ssl.create_default_context().wrap_socket(connection, server_hostname=server)
        connection.setblocking(False)
        self.connection = connection
        password = os.environ.get("IRC_PASSWORD", "").strip()
        if password:
            self._write(f"PASS {password}")
        nickname = str(self.listener.get("nickname") or "mailbox-relay")
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
        if self.joined or not self.listener:
            return
        for channel in self.listener.get("channel_ids", []):
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
            return
        prefix, remainder = line[1:].split(" PRIVMSG ", 1)
        target, separator, text = remainder.partition(" :")
        if not separator:
            return
        nickname = prefix.split("!", 1)[0]
        own_nick = str((self.listener or {}).get("nickname") or "mailbox-relay")
        if nickname.casefold() == own_nick.casefold():
            return
        listener = self.listener or {}
        recipients = list(dict.fromkeys([
            *([str(listener["bridge_agent"])] if listener.get("bridge_agent") else []),
            *listener.get("mailbox_recipients", []),
            *subscription_recipients("irc", listener, target),
        ]))
        source_id = tags.get("msgid") or f"irc-{uuid.uuid4()}"
        extra_fields = with_origin(
            {"author": nickname, "irc_prefix": prefix},
            adapter="irc",
            listener_id=str(listener.get("id") or ""),
            source_id=source_id,
            channel_id=target,
            presence_id=str(listener.get("presence_id") or ""),
        )
        DeliveryLedger(mailbox.mailbox_dir()).claim(
            extra_fields,
            endpoint_id(
                "irc", listener_id=str(listener.get("id") or ""), channel_id=target,
                presence_id=str(listener.get("presence_id") or ""),
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
        dispatch_routes(mailbox, listener_id=str(listener.get("id") or ""), channel_id=target,
                        message={**extra_fields, "text": text, "source_id": source_id})

    def send_message(self, message: dict[str, Any]) -> None:
        if not self.status["enabled"]:
            raise RuntimeError("IRC adapter is not enabled")
        if not self.connection:
            self.connect()
        target = str(message.get("channel_id") or "").strip()
        if not target:
            raise ValueError("IRC outbound message requires channel_id")
        text = str(message.get("text") or "")
        attachment_lines = [f"Attachment: {attachment_url(record)}" for record in message.get("attachments") or []]
        for line in [*(text.splitlines() or [""]), *attachment_lines]:
            self._write(f"PRIVMSG {target} :{line}")
