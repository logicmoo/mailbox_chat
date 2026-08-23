from pathlib import Path

from mailbox_channels import agent_mailbox
from mailbox_channels.adapters import irc_adapter
from mailbox_channels.adapters.irc_adapter import IrcAdapter


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.chunks: list[bytes] = []

    def setblocking(self, _enabled: bool) -> None:
        return

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload.decode("utf-8"))

    def recv(self, _size: int) -> bytes:
        if not self.chunks:
            raise BlockingIOError
        return self.chunks.pop(0)

    def close(self) -> None:
        return


def connector() -> dict:
    return {
        "id": "irc-test", "adapter": "irc", "enabled": True,
        "direction": "bidirectional", "server": "irc.example", "port": 6667,
        "tls": False, "nickname": "relay", "channel_ids": ["#agents"],
        "bridge_agent": "irc-bridge-agent", "mailbox_recipients": ["observer"],
    }


def test_irc_registration_ping_join_and_inbound_mailbox(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(agent_mailbox.MAILBOX_ENV, str(tmp_path))
    monkeypatch.setattr(irc_adapter, "connectors_for", lambda *_args, **_kwargs: [connector()])
    connection = FakeSocket()
    adapter = IrcAdapter(socket_factory=lambda _address, _timeout: connection)
    assert adapter.configure()
    adapter.connect()
    connection.chunks.append(
        b":server 001 relay :welcome\r\nPING :token\r\n:nick!user@host PRIVMSG #agents :hello IRC\r\n"
    )
    adapter.cycle(agent_mailbox)
    sent = "".join(connection.sent)
    assert "NICK relay\r\n" in sent
    assert "JOIN #agents\r\n" in sent
    assert "PONG :token\r\n" in sent
    assert agent_mailbox.receive("irc-bridge-agent", root=tmp_path)[0]["text"] == "hello IRC"
    assert agent_mailbox.receive("observer", root=tmp_path)[0]["channel_type"] == "irc"


def test_irc_outbound_privmsg(monkeypatch) -> None:
    monkeypatch.setattr(irc_adapter, "connectors_for", lambda *_args, **_kwargs: [connector()])
    connection = FakeSocket()
    adapter = IrcAdapter(socket_factory=lambda _address, _timeout: connection)
    adapter.configure()
    adapter.send_message({"channel_id": "#agents", "text": "one\ntwo"})
    sent = "".join(connection.sent)
    assert "PRIVMSG #agents :one\r\n" in sent
    assert "PRIVMSG #agents :two\r\n" in sent


def test_irc_publishes_attachment_urls(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(agent_mailbox.MAILBOX_ENV, str(tmp_path))
    monkeypatch.setenv("MAILBOX_RELAY_PUBLIC_URL", "https://relay.example")
    monkeypatch.setattr(irc_adapter, "connectors_for", lambda *_args, **_kwargs: [connector()])
    source = tmp_path / "image name.png"
    source.write_bytes(b"png")
    message = agent_mailbox.send("unused", "image", attachments=[source], root=tmp_path)
    connection = FakeSocket()
    adapter = IrcAdapter(socket_factory=lambda _address, _timeout: connection)
    adapter.configure()
    adapter.send_message({"channel_id": "#agents", "text": "look", "attachments": message["attachments"]})
    sent = "".join(connection.sent)
    assert "Attachment: https://relay.example/v1/attachments/" in sent
    assert "image%20name.png" in sent


def test_irc_list_channels_collects_names_counts_and_topics(monkeypatch) -> None:
    monkeypatch.setattr(irc_adapter, "connectors_for", lambda *_args, **_kwargs: [connector()])
    connection = FakeSocket()
    connection.chunks.append(
        b":server 001 relay :welcome\r\n"
        b":server 322 relay #agents 42 :Agent coordination\r\n"
        b":server 322 relay #testing 3 :Test room\r\n"
        b":server 323 relay :End of LIST\r\n"
    )
    adapter = IrcAdapter(socket_factory=lambda _address, _timeout: connection)
    assert adapter.configure()
    assert adapter.list_channels(timeout=1) == [
        {"identifier": "#agents", "text": "Agent coordination", "kind": "channel",
         "metadata": {"visible_users": 42}},
        {"identifier": "#testing", "text": "Test room", "kind": "channel",
         "metadata": {"visible_users": 3}},
    ]
    assert "LIST\r\n" in "".join(connection.sent)


def test_irc_names_lists_channel_users_and_status(monkeypatch) -> None:
    monkeypatch.setattr(irc_adapter, "connectors_for", lambda *_args, **_kwargs: [connector()])
    connection = FakeSocket()
    connection.chunks.append(
        b":server 001 relay :welcome\r\n"
        b":server 353 relay = #testing :@alice +bob carol\r\n"
        b":server 366 relay #testing :End of NAMES\r\n"
    )
    adapter = IrcAdapter(socket_factory=lambda _address, _timeout: connection)
    assert adapter.configure()
    users = adapter.list_channel_users("testing", timeout=1)
    assert [item["identifier"] for item in users] == ["alice", "bob", "carol"]
    assert users[0]["metadata"] == {"channel": "#testing", "status_prefix": "@"}
    assert "NAMES #testing\r\n" in "".join(connection.sent)


def test_irc_status_address_receives_server_notices(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(agent_mailbox.MAILBOX_ENV, str(tmp_path))
    monkeypatch.setattr(irc_adapter, "connectors_for", lambda *_args, **_kwargs: [connector()])
    monkeypatch.setattr(
        irc_adapter, "subscription_recipients",
        lambda adapter, _connector, identifier: ["observer"]
        if (adapter, identifier) == ("irc", "status") else [],
    )
    adapter = IrcAdapter(socket_factory=lambda _address, _timeout: FakeSocket())
    adapter.configure()
    adapter._handle_line(":irc.example NOTICE relay :Scheduled maintenance", agent_mailbox)
    message = agent_mailbox.receive("observer", root=tmp_path)[0]
    assert message["type"] == "irc_status"
    assert message["channel_id"] == "status"
    assert message["text"] == "Scheduled maintenance"
