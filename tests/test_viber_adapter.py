import hashlib
import hmac
import json
from pathlib import Path

from mailbox_channels.adapters.viber_adapter import ViberAdapter


class Response:
    def json(self):
        return {"status": 0, "status_message": "ok", "message_token": 123}

    def raise_for_status(self):
        return None


class Session:
    def __init__(self):
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return Response()


class Mailbox:
    def __init__(self, root: Path):
        self.root = root
        self.sent = []

    def mailbox_dir(self):
        return self.root

    def send(self, recipient, text, **kwargs):
        self.sent.append((recipient, text, kwargs))


def test_viber_signed_webhook_inbound_and_outbound(monkeypatch, tmp_path: Path) -> None:
    listener = {
        "id": "viber-one", "direction": "bidirectional", "channel_ids": ["user-1"],
        "bridge_agent": "viber-agent", "mailbox_recipients": ["worker"], "bot_name": "Relay",
    }
    monkeypatch.setenv("VIBER_AUTH_TOKEN", "secret")
    monkeypatch.setattr("mailbox_channels.adapters.viber_adapter.listeners_for",
                        lambda adapter: [listener])
    session = Session()
    adapter = ViberAdapter(session=session)
    mailbox = Mailbox(tmp_path)
    assert adapter.configure()
    payload = {
        "event": "message", "message_token": 101,
        "sender": {"id": "user-1", "name": "Douglas", "language": "en"},
        "message": {"type": "text", "text": "hello"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert adapter.authenticate_webhook(body, signature) == listener
    assert adapter.authenticate_webhook(body + b"x", signature) is None
    adapter.handle_webhook(payload, mailbox, listener)
    assert [item[0] for item in mailbox.sent] == ["viber-agent", "worker"]
    assert mailbox.sent[0][2]["channel_type"] == "viber"
    adapter.send_message({"listener_id": "viber-one", "channel_id": "user-1", "text": "reply"})
    assert session.posts[-1][0].endswith("/send_message")
    assert session.posts[-1][1]["headers"]["X-Viber-Auth-Token"] == "secret"
    assert session.posts[-1][1]["json"]["receiver"] == "user-1"


def test_viber_outbound_file_uses_public_attachment_url(monkeypatch, tmp_path: Path) -> None:
    listener = {"id": "viber-one", "direction": "outbound", "bot_name": "Relay"}
    monkeypatch.setenv("VIBER_AUTH_TOKEN", "secret")
    monkeypatch.setattr("mailbox_channels.adapters.viber_adapter.listeners_for",
                        lambda adapter: [listener])
    monkeypatch.setattr("mailbox_channels.adapters.viber_adapter.attachment_url",
                        lambda record: "https://relay.example/v1/attachments/report.txt")
    path = tmp_path / "report.txt"
    path.write_text("result", encoding="utf-8")
    session = Session()
    adapter = ViberAdapter(session=session)
    assert adapter.configure()
    adapter.send_message({"listener_id": "viber-one", "channel_id": "user-1", "attachments": [{
        "path": str(path), "name": "report.txt",
    }]})
    assert session.posts[-1][1]["json"]["type"] == "file"
    assert session.posts[-1][1]["json"]["media"].startswith("https://relay.example/")
