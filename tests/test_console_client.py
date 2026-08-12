import json
from pathlib import Path

from mailbox_channel_relay_bridging_proxy import agent_mailbox, console_client
from mailbox_channel_relay_bridging_proxy.console_client import (
    CLIENT_NAME,
    display_event,
    parser,
    run,
    websocket_url,
)


def test_console_client_arguments() -> None:
    arguments = parser().parse_args(["console-one", "--to", "irc-bridge-agent"])
    assert arguments.identity == "console-one"
    assert arguments.destination == "irc-bridge-agent"
    assert CLIENT_NAME == "Trusted Speaker"
    assert parser().prog == "trusted-speaker"
    local = parser().parse_args(["console-one", "--dir", "mailbox"])
    assert local.dir == Path("mailbox")


def test_console_client_formats_mailbox_message() -> None:
    rendered = display_event(json.dumps({
        "type": "message",
        "message": {"from": "irc:nick", "to": "console-one", "text": "hello"},
    }))
    assert rendered == "[irc:nick -> console-one] hello"


def test_console_client_accepts_relay_base_urls() -> None:
    assert websocket_url("http://127.0.0.1:46667") == "ws://127.0.0.1:46667/v1/chat/ws"
    assert websocket_url("https://relay.example/") == "wss://relay.example/v1/chat/ws"
    assert websocket_url("ws://relay.example/v1/chat/ws") == "ws://relay.example/v1/chat/ws"


def test_console_client_reports_unavailable_service(monkeypatch, capsys) -> None:
    monkeypatch.setattr("mailbox_channel_relay_bridging_proxy.console_client.websocket.create_connection", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")))
    assert run("console-one", "agent-two", "ws://127.0.0.1:1/v1/chat/ws") == 2
    assert "Trusted Speaker service unavailable" in capsys.readouterr().err


def test_trusted_speaker_sends_through_local_mailbox(monkeypatch, tmp_path, capsys) -> None:
    class ThreadWithoutReceiver:
        def __init__(self, **_kwargs): pass
        def start(self): pass

    entries = iter(["hello locally", "/quit"])
    monkeypatch.setattr(console_client.threading, "Thread", ThreadWithoutReceiver)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(entries))

    assert run("speaker-one", "agent-beta", "unused", directory=tmp_path) == 0
    received = agent_mailbox.receive("agent-beta", root=tmp_path)
    assert received[0]["from"] == "speaker-one"
    assert received[0]["text"] == "hello locally"
    assert "transport is" in capsys.readouterr().out
