from pathlib import Path

from mailbox_chat.adapters.discord_adapter import DiscordAdapter
from mailbox_chat.identifier_directory import IdentifierDirectory


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
        if url.endswith("/users/@me/guilds"):
            return Response([])
        if "/guilds/" in url and url.endswith("/channels"):
            return Response([])
        if url.endswith("/users/@me"):
            return Response({"id": "bot-1"})
        self.message_reads += 1
        if self.message_reads == 1:
            return Response([])
        return Response([{"id": "123", "content": "hello", "author": {"id": "u1", "username": "douglas"}}])

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return Response({"id": "sent-1"})


class RefreshOnlySession:
    def __init__(self):
        self.posts = []
        self.refresh_reads = 0

    def get(self, url, **kwargs):
        params = kwargs.get("params") or {}
        if url.endswith("/users/@me/guilds"):
            return Response([])
        if "/guilds/" in url and url.endswith("/channels"):
            return Response([])
        if url.endswith("/users/@me"):
            return Response({"id": "bot-1"})
        if params.get("limit") == 1:
            return Response([{"id": "200"}])
        if params.get("after") is not None:
            return Response([])
        self.refresh_reads += 1
        return Response([{
            "id": "199",
            "content": "",
            "guild_id": "g-1",
            "author": {"id": "u-refresh", "username": "refresh-user"},
        }])

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return Response({"id": "sent-1"})


class ChannelDiscoverySession:
    def __init__(self):
        self.posts = []
        self.requested_channels = []

    def get(self, url, **kwargs):
        params = kwargs.get("params") or {}
        if url.endswith("/users/@me"):
            return Response({"id": "bot-1"})
        if url.endswith("/users/@me/guilds"):
            return Response([{"id": "g1", "name": "Guild"}])
        if "/guilds/" in url and url.endswith("/channels"):
            return Response([
                {"id": "c-discovered", "type": 0, "name": "general"},
                {"id": "voice-1", "type": 2, "name": "voice"},
            ])
        if "/channels/" in url and url.endswith("/messages"):
            channel_id = url.split("/channels/", 1)[1].split("/messages", 1)[0]
            self.requested_channels.append(channel_id)
            if params.get("limit") == 1:
                return Response([])
            return Response([])
        return Response([])

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
    connector = {
        "id": "discord-one", "direction": "bidirectional", "mailbox_ids": ["c1"],
        "bridge_agent": "discord-agent", "mailbox_recipients": ["worker"], "auto_discover_channels": False,
    }
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret")
    monkeypatch.setattr("mailbox_chat.adapters.discord_adapter.connectors_for", lambda adapter: [connector])
    session = Session()
    adapter = DiscordAdapter(session=session)
    mailbox = Mailbox(tmp_path)
    assert adapter.configure()
    adapter.cycle(mailbox)
    adapter.cycle(mailbox)
    assert [item[0] for item in mailbox.sent] == ["discord-agent", "worker"]
    assert mailbox.sent[0][2]["mailbox_type"] == "discord"

    adapter.send_message({"connector_id": "discord-one", "mailbox_id": "c1", "text": "reply"})
    assert session.posts[0][1]["json"] == {"content": "reply"}


def test_discord_cycle_refreshes_and_stores_user_names_without_new_messages(monkeypatch, tmp_path: Path) -> None:
    connector = {
        "id": "discord-one",
        "direction": "bidirectional",
        "mailbox_ids": ["c1"],
        "bridge_agent": "discord-agent",
        "mailbox_recipients": ["worker"],
        "auto_discover_channels": False,
    }
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret")
    monkeypatch.setattr("mailbox_chat.adapters.discord_adapter.connectors_for", lambda adapter: [connector])
    session = RefreshOnlySession()
    adapter = DiscordAdapter(session=session)
    mailbox = Mailbox(tmp_path)

    assert adapter.configure()
    adapter.cycle(mailbox)

    directory = IdentifierDirectory(tmp_path)
    users = directory.find(system="discord/discord-one", kind="user", limit=50)
    assert users[0]["identifier"] == "u-refresh"
    assert users[0]["text"] == "refresh-user"
    assert users[0]["metadata"]["mailbox_id"] == "c1"
    assert session.refresh_reads == 1


def test_discord_cycle_auto_discovers_guild_text_channels(monkeypatch, tmp_path: Path) -> None:
    connector = {
        "id": "discord-one",
        "direction": "bidirectional",
        "mailbox_ids": ["c-configured"],
        "bridge_agent": "discord-agent",
        "mailbox_recipients": ["worker"],
        "auto_discover_channels": True,
    }
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret")
    monkeypatch.setattr("mailbox_chat.adapters.discord_adapter.connectors_for", lambda adapter: [connector])
    session = ChannelDiscoverySession()
    adapter = DiscordAdapter(session=session)
    mailbox = Mailbox(tmp_path)

    assert adapter.configure()
    adapter.cycle(mailbox)

    assert "c-configured" in adapter.status["mailboxes"]
    assert "c-discovered" in adapter.status["mailboxes"]
    assert "voice-1" not in adapter.status["mailboxes"]
