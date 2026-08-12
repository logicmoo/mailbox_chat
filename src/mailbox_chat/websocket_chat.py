"""Minimal RFC 6455 WebSocket view over the durable mailbox."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import time
from typing import Any


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_PAYLOAD_BYTES = 1_048_576


def accept_value(key: str) -> str:
    digest = hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(payload: bytes, *, opcode: int = 1) -> bytes:
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length <= 0xFFFF:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    return bytes(header) + payload


def _read_exact(connection: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = connection.recv(length - len(chunks))
        if not chunk:
            raise ConnectionError("WebSocket client disconnected")
        chunks.extend(chunk)
    return bytes(chunks)


def read_frame(connection: socket.socket) -> tuple[int, bytes]:
    first, second = _read_exact(connection, 2)
    if not first & 0x80:
        raise ValueError("Fragmented WebSocket frames are not supported")
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(connection, 8))[0]
    if length > MAX_PAYLOAD_BYTES:
        raise ValueError("WebSocket payload exceeds 1 MiB")
    if not masked:
        raise ValueError("Client WebSocket frames must be masked")
    mask = _read_exact(connection, 4)
    payload = _read_exact(connection, length)
    return opcode, bytes(value ^ mask[index % 4] for index, value in enumerate(payload))


def send_json(connection: socket.socket, payload: dict[str, Any]) -> None:
    connection.sendall(encode_frame(json.dumps(payload).encode("utf-8")))


def handle_chat(connection: socket.socket, recipient: str, mailbox: Any) -> None:
    """Run one mailbox-backed WebSocket chat session for a stable identity."""
    connection.settimeout(0.25)
    send_json(connection, {
        "type": "hello",
        "recipient": recipient,
        "protocol": "mailbox-chat.v1",
        "mailbox_backed": True,
    })
    while True:
        for message in mailbox.receive(recipient):
            send_json(connection, {"type": "message", "message": message})
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
            raise ValueError("Only JSON text frames are supported")
        request = json.loads(payload.decode("utf-8"))
        action = str(request.get("action") or "send")
        if action == "ping":
            send_json(connection, {"type": "pong", "timestamp": time.time()})
            continue
        if action != "send":
            send_json(connection, {"type": "error", "error": f"Unknown action: {action}"})
            continue
        destination = str(request.get("to") or "").strip()
        if not destination:
            send_json(connection, {"type": "error", "error": "to is required"})
            continue
        message = mailbox.send(
            destination,
            str(request.get("text") or ""),
            sender=recipient,
            message_type=str(request.get("message_type") or "message"),
            channel_id=request.get("channel_id"),
            channel_type=request.get("channel_type"),
            thread_id=request.get("thread_id"),
            root_id=request.get("root_id"),
            extra_fields={key: value for key, value in request.items() if key not in {
                "action", "to", "text", "message_type", "channel_id", "channel_type", "thread_id", "root_id",
            }},
        )
        send_json(connection, {"type": "sent", "message": message})
