import hashlib
import hmac
import json
from pathlib import Path

from mailbox_channel_relay_bridging_proxy.discourse_adapter import DiscourseAdapter


class Response:
    def raise_for_status(self): pass
    def json(self): return {"id": 99, "topic_id": 42}


class Session:
    def __init__(self): self.posts = []
    def post(self, url, **kwargs): self.posts.append((url, kwargs)); return Response()


class Mailbox:
    def __init__(self, root: Path): self.root, self.sent = root, []
    def mailbox_dir(self): return self.root
    def send(self, recipient, text, **kwargs): self.sent.append((recipient, text, kwargs))


def test_discourse_signed_post_webhook_and_reply(monkeypatch, tmp_path: Path) -> None:
    listener = {"id": "forum", "direction": "bidirectional", "base_url": "https://forum.example",
                "channel_ids": ["42"], "bridge_agent": "forum-agent", "mailbox_recipients": ["worker"]}
    monkeypatch.setenv("DISCOURSE_API_KEY", "key"); monkeypatch.setenv("DISCOURSE_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr("mailbox_channel_relay_bridging_proxy.discourse_adapter.listeners_for",
                        lambda adapter: [listener])
    session, mailbox = Session(), Mailbox(tmp_path); adapter = DiscourseAdapter(session=session)
    assert adapter.configure()
    payload = {"post": {"id": 7, "topic_id": 42, "post_number": 2,
                        "username": "douglas", "raw": "hello", "topic_title": "Agents"}}
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert adapter.authenticate_webhook(body, signature) == listener
    adapter.handle_webhook(payload, mailbox, listener, event_id="event-1", event_name="post_created")
    assert [item[0] for item in mailbox.sent] == ["forum-agent", "worker"]
    adapter.send_message({"listener_id": "forum", "channel_id": "42", "text": "reply", "thread_id": "2"})
    assert session.posts[-1][1]["json"]["reply_to_post_number"] == 2


def test_discourse_can_create_topic(monkeypatch) -> None:
    listener = {"id": "forum", "direction": "outbound", "base_url": "https://forum.example",
                "default_category_id": 3}
    monkeypatch.setenv("DISCOURSE_API_KEY", "key"); monkeypatch.setenv("DISCOURSE_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr("mailbox_channel_relay_bridging_proxy.discourse_adapter.listeners_for",
                        lambda adapter: [listener])
    session = Session(); adapter = DiscourseAdapter(session=session); assert adapter.configure()
    adapter.send_message({"listener_id": "forum", "text": "opening", "topic_title": "New topic"})
    assert session.posts[-1][1]["json"]["category"] == 3
