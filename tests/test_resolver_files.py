import json
from pathlib import Path

from mailbox_chat import agent_mailbox
from mailbox_chat.adapters.discord_adapter import DiscordAdapter
from mailbox_chat.adapters.irc_adapter import IrcAdapter
from mailbox_chat.identifier_directory import IdentifierDirectory


class _Mailbox:
    def __init__(self, root: Path) -> None:
        self._root = root
        self.sent: list[dict] = []

    def mailbox_dir(self) -> Path:
        return self._root

    def send(self, recipient: str, text: str, **kwargs) -> None:
        self.sent.append({"recipient": recipient, "text": text, **kwargs})


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _DiscordSession:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages

    def get(self, *_args, **_kwargs):
        return _Response(self._messages)


def _resolver_entries(root: Path, name: str) -> list[dict]:
    path = root / "resolver" / name
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("entries") or [])


def test_identifier_directory_writes_mm_entries_to_resolver_file(tmp_path: Path) -> None:
    directory = IdentifierDirectory(tmp_path)
    directory.remember(
        "jmoduftrb3yjdnozzwst5ore1c",
        "logicmoo",
        system="mm/chat.singularitynet.io",
        kind="mailbox",
        metadata={"mailbox_name": "logicmoo"},
    )

    entries = _resolver_entries(tmp_path, "resolver_mattermost-chat.singularitynet.io.json")
    assert any(
        item["identifier"] == "jmoduftrb3yjdnozzwst5ore1c"
        and item["text"] == "logicmoo"
        and item["kind"] == "mailbox"
        for item in entries
    )


def test_irc_inbound_records_user_and_mailbox_in_resolver(tmp_path: Path) -> None:
    connector = {
        "id": "irc-libera",
        "adapter": "irc",
        "server": "irc.libera.chat",
        "nickname": "relaybot",
        "mailbox_ids": ["##logicmoo"],
    }
    adapter = IrcAdapter()
    adapter.configure(connector)
    mailbox = _Mailbox(tmp_path)

    adapter._handle_line(":prologmud!user@host PRIVMSG ##logicmoo :hello", mailbox)

    entries = _resolver_entries(tmp_path, "resolver_irc-irc.libera.chat.json")
    identifiers = {(item["identifier"], item["kind"]) for item in entries}
    assert ("##logicmoo", "mailbox") in identifiers
    assert ("prologmud", "user") in identifiers


def test_discord_inbound_records_user_and_mailbox_in_resolver(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN_TEST", "token")
    connector = {
        "id": "discord-main",
        "adapter": "discord",
        "token_env": "DISCORD_TOKEN_TEST",
        "mailbox_ids": ["12345"],
        "direction": "bidirectional",
    }
    adapter = DiscordAdapter(session=_DiscordSession([{
        "id": "200",
        "content": "hi",
        "author": {"id": "user-1", "username": "atom.ant"},
        "guild_id": "guild-9",
    }]))
    adapter.connectors = [connector]
    adapter.bot_ids = {"discord-main": "bot-1"}
    adapter.latest_ids = {("discord-main", "12345"): "0"}
    adapter.status.update({"enabled": True, "connected": True})
    mailbox = _Mailbox(tmp_path)

    adapter._poll_mailbox(mailbox, connector, "12345")

    entries = _resolver_entries(tmp_path, "resolver_discord-discord-main.json")
    identifiers = {(item["identifier"], item["kind"]) for item in entries}
    assert ("user-1", "user") in identifiers
    assert ("12345", "mailbox") in identifiers


def test_send_records_generated_message_and_attachment_identifiers(tmp_path: Path) -> None:
    attachment = tmp_path / "sample.png"
    attachment.write_bytes(b"image-bytes")

    agent_mailbox.send(
        "worker-one",
        "hello",
        root=tmp_path,
        attachments=[attachment],
    )

    entries = _resolver_entries(tmp_path, "resolver_mailbox-local.json")
    kinds = {item["kind"] for item in entries}
    assert "message" in kinds
    assert "message_audit" in kinds
    assert "image" in kinds
