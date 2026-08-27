from mailbox_chat import discovery_admin
from mailbox_chat import agent_mailbox
from mailbox_chat.identifier_directory import IdentifierDirectory


def test_discovery_help_lists_irc_chat_capability(capsys) -> None:
    assert discovery_admin.main(["platforms"]) == 0
    output = capsys.readouterr().out
    assert '"id": "irc"' in output
    assert '"chats"' in output


def test_discovery_chats_outputs_provider_results(monkeypatch, capsys) -> None:
    monkeypatch.setattr(discovery_admin, "discover_irc_chats", lambda **_kwargs: [
        {"address": "irc/irc.example/%23testing", "text": "Test room"},
    ])
    assert discovery_admin.main(["chats", "--platform", "irc"]) == 0
    assert "irc/irc.example/%23testing" in capsys.readouterr().out


def test_discovery_users_accepts_mailbox_address(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(discovery_admin, "discover_irc_users", lambda mailbox, **_kwargs: calls.append(mailbox) or [
        {"address": "irc/irc.example/alice", "text": "alice"},
    ])
    assert discovery_admin.main([
        "users", "--platform", "irc", "--mailbox", "irc/0/testing",
    ]) == 0
    assert calls == ["irc/0/testing"]
    assert "alice" in capsys.readouterr().out


class _DiscoveryResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _RoutedDiscoveryResponses:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, url, **_kwargs):
        self.calls.append(url)
        if url.endswith("/users/@me/guilds"):
            return _DiscoveryResponse([{"id": "g1", "name": "Guild"}])
        if "/guilds/" in url and url.endswith("/channels"):
            return _DiscoveryResponse([
                {"id": "c1", "type": 0, "name": "general"},
                {"id": "voice-1", "type": 2, "name": "voice"},
            ])
        return _DiscoveryResponse([])


def test_discord_user_discovery_remembers_users_from_live_messages(monkeypatch) -> None:
    connector = {
        "id": "discord-prologmud",
        "adapter": "discord",
        "instance": "discord.com",
        "enabled": True,
        "mailbox_ids": ["c1"],
        "token_env": "DISCORD_BOT_TOKEN",
    }
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setattr(discovery_admin, "connectors_for", lambda _adapter: [connector])
    monkeypatch.setattr(
        discovery_admin.requests, "get",
        lambda *_args, **_kwargs: _DiscoveryResponse([
            {"author": {"id": "u1", "username": "Alice"}},
            {"author": {"id": "u2", "username": "Bob"}},
        ]),
    )

    users = discovery_admin.discover_discord_users("c1")
    names = {entry["text"] for entry in users}
    assert names == {"Alice", "Bob"}

    directory = IdentifierDirectory(agent_mailbox.mailbox_dir())
    remembered = directory.find(system="discord/discord.com", kind="user", limit=100)
    by_id = {entry["identifier"]: entry for entry in remembered}
    assert by_id["u1"]["metadata"]["mailbox_id"] == "c1"
    assert by_id["u2"]["metadata"]["mailbox_id"] == "c1"


def test_discord_user_discovery_returns_cached_users_when_recent_messages_empty(monkeypatch) -> None:
    connector = {
        "id": "discord-prologmud",
        "adapter": "discord",
        "instance": "discord.com",
        "enabled": True,
        "mailbox_ids": ["c1"],
        "token_env": "DISCORD_BOT_TOKEN",
    }
    directory = IdentifierDirectory(agent_mailbox.mailbox_dir())
    directory.remember(
        "u1",
        "Alice",
        system="discord/discord.com",
        kind="user",
        metadata={"mailbox_id": "c1", "mailbox": "c1", "author": {"id": "u1", "username": "Alice"}},
    )

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setattr(discovery_admin, "connectors_for", lambda _adapter: [connector])
    monkeypatch.setattr(
        discovery_admin.requests, "get",
        lambda *_args, **_kwargs: _DiscoveryResponse([]),
    )

    users = discovery_admin.discover_discord_users("c1")
    assert [entry["text"] for entry in users] == ["Alice"]


def test_discord_chat_discovery_includes_api_channels(monkeypatch) -> None:
    connector = {
        "id": "discord-prologmud",
        "adapter": "discord",
        "instance": "discord.com",
        "enabled": True,
        "mailbox_ids": ["c-configured"],
        "token_env": "DISCORD_BOT_TOKEN",
        "auto_discover_channels": True,
    }
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setattr(discovery_admin, "connectors_for", lambda _adapter: [connector])
    routed = _RoutedDiscoveryResponses()
    monkeypatch.setattr(discovery_admin.requests, "get", routed)

    chats = discovery_admin.discover_discord_chats()
    identifiers = {entry["identifier"] for entry in chats}

    assert "c-configured" in identifiers
    assert "c1" in identifiers
    assert "voice-1" not in identifiers
