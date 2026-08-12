import base64
import hashlib
import hmac
import json
from pathlib import Path

from mailbox_channel_relay_bridging_proxy.line_adapter import LineAdapter


class Response:
    content = b"image"
    headers = {"Content-Type": "image/jpeg"}

    def __init__(self, payload=None):
        self.payload = payload or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class Session:
    def __init__(self):
        self.posts = []

    def get(self, url, **kwargs):
        if "/member/" in url or "/profile/" in url:
            return Response({"displayName": "Douglas"})
        return Response()

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return Response({"sentMessages": [{"id": "sent-1"}]})


class Mailbox:
    def __init__(self, root: Path):
        self.root = root
        self.sent = []

    def mailbox_dir(self):
        return self.root

    def send(self, recipient, text, **kwargs):
        self.sent.append((recipient, text, kwargs))


def test_line_signed_group_webhook_and_push(monkeypatch, tmp_path: Path) -> None:
    listener = {"id": "line-one", "direction": "bidirectional", "channel_ids": ["group-1"],
                "bridge_agent": "line-agent", "mailbox_recipients": ["worker"]}
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "access")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "secret")
    monkeypatch.setattr("mailbox_channel_relay_bridging_proxy.line_adapter.listeners_for",
                        lambda adapter: [listener])
    session = Session()
    adapter = LineAdapter(session=session)
    mailbox = Mailbox(tmp_path)
    assert adapter.configure()
    payload = {"destination": "bot", "events": [{
        "type": "message", "webhookEventId": "event-1", "replyToken": "reply-1",
        "source": {"type": "group", "groupId": "group-1", "userId": "user-1"},
        "message": {"type": "text", "id": "message-1", "text": "hello"},
    }]}
    body = json.dumps(payload).encode()
    signature = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
    assert adapter.authenticate_webhook(body, signature) == listener
    assert adapter.authenticate_webhook(body + b"x", signature) is None
    adapter.handle_webhook(payload, mailbox, listener)
    assert [item[0] for item in mailbox.sent] == ["line-agent", "worker"]
    assert mailbox.sent[0][2]["channel_id"] == "group-1"
    assert mailbox.sent[0][2]["channel_type"] == "line"
    adapter.send_message({"listener_id": "line-one", "channel_id": "group-1", "text": "reply"})
    assert session.posts[-1][0].endswith("/message/push")
    assert session.posts[-1][1]["json"]["to"] == "group-1"
    adapter.send_message({"listener_id": "line-one", "channel_id": "group-1", "text": "fast reply",
                          "line_reply_token": "reply-1"})
    assert session.posts[-1][0].endswith("/message/reply")
    assert session.posts[-1][1]["json"]["replyToken"] == "reply-1"


def test_line_downloads_inbound_content_through_quota_storage(monkeypatch, tmp_path: Path) -> None:
    listener = {"id": "line-one", "direction": "inbound", "channel_ids": ["room-1"],
                "mailbox_recipients": ["worker"]}
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "access")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "secret")
    monkeypatch.setattr("mailbox_channel_relay_bridging_proxy.line_adapter.listeners_for",
                        lambda adapter: [listener])
    adapter = LineAdapter(session=Session())
    mailbox = Mailbox(tmp_path)
    assert adapter.configure()
    adapter.handle_webhook({"events": [{"type": "message", "webhookEventId": "event-2",
        "source": {"type": "room", "roomId": "room-1", "userId": "user-1"},
        "message": {"type": "image", "id": "image-1"}}]}, mailbox, listener)
    attachments = mailbox.sent[0][2]["extra_fields"]["attachments"]
    assert Path(attachments[0]["path"]).read_bytes() == b"image"
