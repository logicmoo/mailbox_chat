import json
import socket
import struct
from pathlib import Path

from mailbox_chat import mailbox_store
from mailbox_chat.subscriptions import ensure_mailbox
from mailbox_chat.ws_collab_api import (
    CONVERSATION_MAILBOX,
    handle_ws_collab_delete,
    handle_ws_collab_get,
    handle_ws_collab_post,
    run_ws_collab_websocket,
)


def _ok(result):
    assert result is not None
    status, payload = result
    assert status == 200
    return payload


def _seed_message(root: Path, *, mailbox: str, text: str, sender: str = "seed") -> str:
    ensure_mailbox(mailbox, path=root)
    stored = mailbox_store.append_message(root, mailbox, {
        "id": f"{mailbox}-{sender}-{text}",
        "stream": mailbox,
        "type": "CONVERSATION_MESSAGE",
        "ts": "2026-01-01T00:00:00Z",
        "source_id": sender,
        "source_kind": "agent",
        "data": {"text": text, "to": mailbox},
    })
    return str(stored["entry_number"])


def masked_frame(payload: bytes, opcode: int = 1) -> bytes:
    mask = b"mask"
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.extend([0x80 | length])
    elif length <= 0xFFFF:
        header.extend([0x80 | 126])
        header.extend(struct.pack("!H", length))
    else:
        header.extend([0x80 | 127])
        header.extend(struct.pack("!Q", length))
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return bytes(header) + mask + masked


class FakeSocket:
    def __init__(self, incoming: bytes) -> None:
        self.incoming = bytearray(incoming)
        self.outgoing = bytearray()

    def settimeout(self, _value: float) -> None:
        return None

    def recv(self, size: int) -> bytes:
        if not self.incoming:
            raise socket.timeout()
        chunk = bytes(self.incoming[:size])
        del self.incoming[:size]
        return chunk

    def sendall(self, payload: bytes) -> None:
        self.outgoing.extend(payload)


def _decode_server_frames(payload: bytes) -> list[tuple[int, bytes]]:
    result: list[tuple[int, bytes]] = []
    index = 0
    while index < len(payload):
        first = payload[index]
        second = payload[index + 1]
        index += 2
        opcode = first & 0x0F
        length = second & 0x7F
        masked = bool(second & 0x80)
        if length == 126:
            length = struct.unpack("!H", payload[index:index + 2])[0]
            index += 2
        elif length == 127:
            length = struct.unpack("!Q", payload[index:index + 8])[0]
            index += 8
        assert not masked
        body = payload[index:index + length]
        index += length
        result.append((opcode, body))
    return result


def test_ws_collab_get_capabilities_mailboxes_and_messages(tmp_path: Path) -> None:
    root = tmp_path
    _seed_message(root, mailbox=CONVERSATION_MAILBOX, text="alpha hello", sender="agent-a")
    _seed_message(root, mailbox=CONVERSATION_MAILBOX, text="beta world", sender="agent-b")

    capabilities = _ok(handle_ws_collab_get("/ws_collab/v1/capabilities", "", root=root))
    assert capabilities["product"] == "WS_COLLAB"
    assert capabilities["versioned_base"] == "/ws_collab/v1"

    listing = _ok(handle_ws_collab_get(
        "/ws_collab/v1/mailbox/mailboxes", "agent=tester&include_activity=true", root=root))
    assert listing["place"] == "ws_collab"
    names = {entry["id"] for entry in listing["mailboxes"]}
    assert CONVERSATION_MAILBOX in names
    conversation = next(entry for entry in listing["mailboxes"] if entry["id"] == CONVERSATION_MAILBOX)
    assert conversation["messages"] >= 2
    assert conversation["agent"] == "tester"
    assert "entries_total" in conversation

    filtered = _ok(handle_ws_collab_get(
        "/ws_collab/v1/mailbox/messages",
        "mailbox=conversation&from=agent-a&text=alpha&filter=true&limit=10",
        root=root,
    ))
    assert len(filtered["messages"]) == 1
    assert filtered["messages"][0]["from"] == "agent-a"

    tail = _ok(handle_ws_collab_get("/ws_collab/v1/streams/conversation/tail", "count=1", root=root))
    assert tail["stream"] == "conversation"
    assert len(tail["events"]) == 1


def test_ws_collab_post_round_trip_for_mailboxes_messages_and_cursor(tmp_path: Path) -> None:
    root = tmp_path

    created = _ok(handle_ws_collab_post("/ws_collab/v1/mailbox/create", {
        "id": "feature_room",
        "purpose": "feature testing",
        "writable": True,
        "hidden": False,
    }, root=root))
    assert created["mailbox"]["id"] == "feature_room"
    assert created["mailbox"]["writable"] is True

    sent_one = _ok(handle_ws_collab_post("/ws_collab/v1/mailbox/send", {
        "to": "feature_room",
        "send_to": "feature_room",
        "sender": "tester",
        "text": "hello room",
        "idempotency_key": "fixed-idempotency-key",
    }, root=root))
    sent_two = _ok(handle_ws_collab_post("/ws_collab/v1/mailbox/send", {
        "to": "feature_room",
        "send_to": "feature_room",
        "sender": "tester",
        "text": "hello room",
        "idempotency_key": "fixed-idempotency-key",
    }, root=root))
    assert sent_one["message"]["id"] == sent_two["message"]["id"]

    _ok(handle_ws_collab_post("/ws_collab/v1/mailbox/mailbox-config", {
        "mailbox": "feature_room",
        "config": {"retention": "7d"},
    }, root=root))
    config = _ok(handle_ws_collab_get(
        "/ws_collab/v1/mailbox/mailbox-config", "mailbox=feature_room", root=root))
    assert config["config"]["retention"] == "7d"

    cursor = _ok(handle_ws_collab_post("/ws_collab/v1/mailbox/cursor", {
        "mailbox": "feature_room",
        "agent": "agent-1",
        "start": "beginning",
    }, root=root))
    assert cursor["agent"] == "agent-1"
    assert cursor["mailbox"] == "feature_room"

    _ok(handle_ws_collab_delete(
        "/ws_collab/v1/mailbox/cursor", "mailbox=feature_room&agent=agent-1", root=root))

    removed = _ok(handle_ws_collab_delete("/ws_collab/v1/mailbox/mailboxes", "id=feature_room", root=root))
    assert removed["deleted"] is True


def test_ws_collab_post_agents_subscription_record_and_entity(tmp_path: Path) -> None:
    root = tmp_path
    _seed_message(root, mailbox="conversation", text="edit me", sender="author")

    upserted = _ok(handle_ws_collab_post("/ws_collab/v1/mailbox/agents", {
        "id": "worker-1",
        "properties": {"display_name": "Worker One", "role": "worker"},
    }, root=root))
    assert upserted["id"] == "worker-1"
    assert upserted["display_name"] == "Worker One"

    linked = _ok(handle_ws_collab_post("/ws_collab/v1/mailbox/subscription", {
        "mailbox": "conversation",
        "agent": "worker-1",
        "state": True,
    }, root=root))
    assert linked["state"] is True

    _ok(handle_ws_collab_post("/ws_collab/v1/mailbox/entity", {
        "kind": "agent",
        "id": "worker-1",
        "entry": {"status": "available"},
    }, root=root))
    agents = _ok(handle_ws_collab_get("/ws_collab/v1/mailbox/agents", "", root=root))
    worker = next(item for item in agents["agents"] if item.get("id") == "worker-1")
    assert worker["status"] == "available"

    edited = _ok(handle_ws_collab_post("/ws_collab/v1/mailbox/record", {
        "id": "source-message-id",
        "record": {
            "mailboxId": "conversation",
            "type": "CONVERSATION_MESSAGE",
            "text": "edited text",
            "from": "worker-1",
            "data": {"text": "edited text"},
        },
        "mode": "at-end",
    }, root=root))
    assert edited["mailbox"] == "conversation"
    assert edited["record"]["text"] == "edited text"
    assert edited["record"]["raw"]["data"]["edited_from"] == "source-message-id"


def test_ws_collab_websocket_subscribe_and_publish_flow(tmp_path: Path) -> None:
    root = tmp_path
    _seed_message(root, mailbox="conversation", text="existing", sender="seed")
    subscribe = masked_frame(json.dumps({
        "type": "subscribe",
        "streams": ["conversation"],
        "cursors": {},
        "filters": {},
    }).encode("utf-8"))
    publish = masked_frame(json.dumps({
        "type": "publish",
        "stream": "conversation",
        "event_type": "message",
        "data": {"text": "hello from ws"},
        "idempotency_key": "ws-idem",
        "ack_id": "ack-1",
    }).encode("utf-8"))
    close = masked_frame(b"", opcode=8)
    fake = FakeSocket(subscribe + publish + close)

    run_ws_collab_websocket(fake, root=root, client_ip="127.0.0.1")

    frames = _decode_server_frames(bytes(fake.outgoing))
    messages = [json.loads(payload.decode("utf-8")) for opcode, payload in frames if opcode == 1]
    kinds = {message.get("type") for message in messages}
    assert "auth_ok" in kinds
    assert "subscribed" in kinds
    assert "caught_up" in kinds
    assert "ack" in kinds
    ack = next(message for message in messages if message.get("type") == "ack")
    assert ack["ack_id"] == "ack-1"
    assert ack["duplicate"] is False
    assert any(message.get("type") == "event" for message in messages)


def test_ws_collab_websocket_requires_auth_for_remote_clients(tmp_path: Path) -> None:
    root = tmp_path
    subscribe = masked_frame(json.dumps({
        "type": "subscribe",
        "streams": ["conversation"],
        "cursors": {},
        "filters": {},
    }).encode("utf-8"))
    auth = masked_frame(json.dumps({"type": "auth", "token": "secret"}).encode("utf-8"))
    close = masked_frame(b"", opcode=8)
    fake = FakeSocket(subscribe + auth + close)

    run_ws_collab_websocket(fake, root=root, client_ip="10.11.12.13", required_token="secret")

    frames = _decode_server_frames(bytes(fake.outgoing))
    messages = [json.loads(payload.decode("utf-8")) for opcode, payload in frames if opcode == 1]
    assert not any(message.get("type") == "auth_ok" for message in messages[:1])
    assert any(message.get("type") == "error" and message.get("error", {}).get("code") == "auth"
               for message in messages)
    assert any(message.get("type") == "auth_ok" for message in messages)
