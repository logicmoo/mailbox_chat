import hashlib
import hmac
import json
from pathlib import Path

from mailbox_channels.facebook_messenger_adapter import FacebookMessengerAdapter
from mailbox_channels.meta_webhooks import verify_challenge, verify_signature
from mailbox_channels.whatsapp_adapter import WhatsAppAdapter


class Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class Session:
    def __init__(self):
        self.posts = []

    def get(self, url, **kwargs):
        return Response({"id": "user-1", "name": "Douglas"})

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("/media"):
            return Response({"id": "media-1"})
        return Response({"messages": [{"id": "sent-1"}]})


class Mailbox:
    def __init__(self, root: Path):
        self.root = root
        self.sent = []

    def mailbox_dir(self):
        return self.root

    def send(self, recipient, text, **kwargs):
        self.sent.append((recipient, text, kwargs))


def test_meta_webhook_verification_and_signature() -> None:
    body = json.dumps({"object": "page"}).encode()
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_challenge({"hub.mode": ["subscribe"], "hub.verify_token": ["token"],
                             "hub.challenge": ["123"]}, "token") == "123"
    assert verify_signature(body, signature, "secret")
    assert not verify_signature(body + b"x", signature, "secret")


def test_facebook_messenger_inbound_and_outbound(monkeypatch, tmp_path: Path) -> None:
    listener = {"id": "facebook-one", "direction": "bidirectional", "page_id": "page-1",
                "channel_ids": ["user-1"], "bridge_agent": "facebook-agent",
                "mailbox_recipients": ["worker"]}
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "secret")
    monkeypatch.setenv("FACEBOOK_VERIFY_TOKEN", "verify")
    monkeypatch.setenv("FACEBOOK_APP_SECRET", "app-secret")
    monkeypatch.setattr("mailbox_channels.facebook_messenger_adapter.listeners_for", lambda adapter: [listener])
    session = Session()
    adapter = FacebookMessengerAdapter(session=session)
    mailbox = Mailbox(tmp_path)
    assert adapter.configure()
    adapter.handle_webhook({"entry": [{"id": "page-1", "messaging": [{
        "sender": {"id": "user-1"}, "recipient": {"id": "page-1"},
        "message": {"mid": "m-1", "text": "hello"},
    }]}]}, mailbox)
    assert [item[0] for item in mailbox.sent] == ["facebook-agent", "worker"]
    assert mailbox.sent[0][2]["channel_type"] == "facebook_messenger"
    adapter.send_message({"listener_id": "facebook-one", "channel_id": "user-1", "text": "reply"})
    assert session.posts[-1][1]["json"]["recipient"] == {"id": "user-1"}


def test_whatsapp_business_inbound_outbound_and_media(monkeypatch, tmp_path: Path) -> None:
    listener = {"id": "whatsapp-one", "direction": "bidirectional", "phone_number_id": "phone-1",
                "channel_ids": ["15551234567"], "bridge_agent": "whatsapp-agent",
                "mailbox_recipients": ["worker"]}
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "secret")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret")
    monkeypatch.setattr("mailbox_channels.whatsapp_adapter.listeners_for", lambda adapter: [listener])
    session = Session()
    adapter = WhatsAppAdapter(session=session)
    mailbox = Mailbox(tmp_path)
    assert adapter.configure()
    adapter.handle_webhook({"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "phone-1"},
        "contacts": [{"wa_id": "15551234567", "profile": {"name": "Douglas"}}],
        "messages": [{"from": "15551234567", "id": "wamid-1", "type": "text",
                      "text": {"body": "hello"}}],
    }}]}]}, mailbox)
    assert [item[0] for item in mailbox.sent] == ["whatsapp-agent", "worker"]
    assert mailbox.sent[0][2]["channel_type"] == "whatsapp"
    attachment = tmp_path / "report.txt"
    attachment.write_text("result", encoding="utf-8")
    adapter.send_message({"listener_id": "whatsapp-one", "channel_id": "15551234567",
                          "text": "reply", "attachments": [{"path": str(attachment)}]})
    assert session.posts[0][1]["json"]["type"] == "text"
    assert session.posts[1][0].endswith("/media")
    assert session.posts[2][1]["json"]["document"]["id"] == "media-1"


def test_whatsapp_business_group_preserves_group_and_participant(monkeypatch, tmp_path: Path) -> None:
    listener = {"id": "whatsapp-one", "direction": "bidirectional", "phone_number_id": "phone-1",
                "channel_ids": ["group-1"], "groups_enabled": True,
                "bridge_agent": "whatsapp-agent", "mailbox_recipients": []}
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "secret")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret")
    monkeypatch.setattr("mailbox_channels.whatsapp_adapter.listeners_for",
                        lambda adapter: [listener])
    session, mailbox = Session(), Mailbox(tmp_path)
    adapter = WhatsAppAdapter(session=session)
    assert adapter.configure()
    adapter.handle_webhook({"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "phone-1"},
        "contacts": [{"wa_id": "15551234567", "profile": {"name": "Douglas"}}],
        "messages": [{"from": "15551234567", "id": "wamid-group", "group_id": "group-1",
                      "type": "text", "text": {"body": "hello group"}}],
    }}]}]}, mailbox)
    assert mailbox.sent[0][2]["channel_id"] == "group-1"
    assert mailbox.sent[0][2]["extra_fields"]["whatsapp_participant_id"] == "15551234567"
    adapter.send_message({"listener_id": "whatsapp-one", "channel_id": "group-1",
                          "whatsapp_group": True, "text": "reply"})
    assert session.posts[-1][1]["json"]["recipient_type"] == "group"


def test_inbound_meta_adapters_require_webhook_credentials(monkeypatch) -> None:
    whatsapp = {"id": "wa", "direction": "inbound", "phone_number_id": "phone"}
    facebook = {"id": "fb", "direction": "inbound", "page_id": "page"}
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "token")
    for name in ("WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET",
                 "FACEBOOK_VERIFY_TOKEN", "FACEBOOK_APP_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("mailbox_channels.whatsapp_adapter.listeners_for",
                        lambda adapter: [whatsapp])
    monkeypatch.setattr("mailbox_channels.facebook_messenger_adapter.listeners_for",
                        lambda adapter: [facebook])
    whatsapp_adapter = WhatsAppAdapter()
    facebook_adapter = FacebookMessengerAdapter()
    assert not whatsapp_adapter.configure()
    assert "WHATSAPP_VERIFY_TOKEN" in whatsapp_adapter.status["lastError"]
    assert "WHATSAPP_APP_SECRET" in whatsapp_adapter.status["lastError"]
    assert not facebook_adapter.configure()
    assert "FACEBOOK_VERIFY_TOKEN" in facebook_adapter.status["lastError"]
    assert "FACEBOOK_APP_SECRET" in facebook_adapter.status["lastError"]
