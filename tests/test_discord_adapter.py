from pathlib import Path

from mailbox_channel_relay_bridging_proxy.discord_adapter import DiscordAdapter


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
        self.message_reads = 0

    def get(self, url, **kwargs):
        if url.endswith("/users/@me"):
            return Response({"id": "bot-1"})
        self.message_reads += 1
        if self.message_reads == 1:
            return Response([])
        return Response([{"id": "123", "content": "hello", "author": {"id": "u1", "username": "douglas"}}])

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return Response({"id": "sent-1"})


class Mailbox:
    def __init__(self, root: Path):
        self.root = root
        self.sent = []

    def mailbox_dir(self):
        return self.root

    def send(self, recipient, text, **kwargs):
        self.sent.append((recipient, text, kwargs))


def test_discord_inbound_and_outbound_use_mailbox(monkeypatch, tmp_path: Path) -> None:
    listener = {
        "id": "discord-one", "direction": "bidirectional", "channel_ids": ["c1"],
        "bridge_agent": "discord-agent", "mailbox_recipients": ["worker"],
    }
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret")
    monkeypatch.setattr("mailbox_channel_relay_bridging_proxy.discord_adapter.listeners_for", lambda adapter: [listener])
    session = Session()
    adapter = DiscordAdapter(session=session)
    mailbox = Mailbox(tmp_path)
    assert adapter.configure()
    adapter.cycle(mailbox)
    adapter.cycle(mailbox)
    assert [item[0] for item in mailbox.sent] == ["discord-agent", "worker"]
    assert mailbox.sent[0][2]["channel_type"] == "discord"

    adapter.send_message({"listener_id": "discord-one", "channel_id": "c1", "text": "reply"})
    assert session.posts[0][1]["json"] == {"content": "reply"}
