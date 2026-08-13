import hashlib
import hmac
import json
from pathlib import Path

from mailbox_channels.adapters.whatsapp_personal_adapter import WhatsAppPersonalAdapter


class Response:
    def __init__(self, payload=None): self.payload = payload or {"ready": True}
    def json(self): return self.payload
    def raise_for_status(self): pass


class Session:
    def __init__(self): self.posts = []
    def get(self, url, **kwargs): return Response()
    def post(self, url, **kwargs): self.posts.append((url, kwargs)); return Response({"sent": ["one"]})


class Mailbox:
    def __init__(self, root: Path): self.root, self.sent = root, []
    def mailbox_dir(self): return self.root
    def send(self, recipient, text, **kwargs): self.sent.append((recipient, text, kwargs))


def test_personal_whatsapp_signed_group_and_send(monkeypatch, tmp_path: Path) -> None:
    listener = {"id": "personal", "direction": "bidirectional", "channel_ids": ["group@g.us"],
                "include_groups": True, "bridge_agent": "wa-agent", "mailbox_recipients": ["worker"]}
    monkeypatch.setenv("WHATSAPP_PERSONAL_COMPANION_TOKEN", "token")
    monkeypatch.setenv("WHATSAPP_PERSONAL_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr("mailbox_channels.adapters.whatsapp_personal_adapter.listeners_for",
                        lambda adapter: [listener])
    session, mailbox = Session(), Mailbox(tmp_path); adapter = WhatsAppPersonalAdapter(session=session)
    assert adapter.configure()
    payload = {"message_id": "msg-1", "chat_id": "group@g.us", "chat_name": "Family",
               "is_group": True, "author_id": "1555@c.us", "author_name": "Douglas", "text": "hello"}
    body = json.dumps(payload).encode(); signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert adapter.authenticate_webhook(body, signature) == listener
    adapter.handle_webhook(payload, mailbox, listener)
    assert [item[0] for item in mailbox.sent] == ["wa-agent", "worker"]
    assert mailbox.sent[0][2]["channel_id"] == "group@g.us"
    adapter.send_message({"listener_id": "personal", "channel_id": "group@g.us", "text": "reply"})
    assert session.posts[-1][0].endswith("/send")
    assert session.posts[-1][1]["headers"]["Authorization"] == "Bearer token"
