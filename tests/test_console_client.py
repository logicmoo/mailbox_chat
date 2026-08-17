import json
from pathlib import Path

from mailbox_channels import agent_mailbox, console_client
from mailbox_channels.console_client import (
    CLIENT_NAME,
    display_event,
    parser,
    run,
    run_administration,
    run_client_command,
    relay_http_url,
    websocket_url,
)


def test_console_client_arguments() -> None:
    arguments = parser().parse_args(["console-one", "--to", "irc-bridge-agent"])
    assert arguments.identity == "console-one"
    assert arguments.destination == "irc-bridge-agent"
    assert CLIENT_NAME == "Mailbox Console"
    assert parser().prog == "mailbox-console"
    local = parser().parse_args(["console-one", "--dir", "mailbox"])
    assert local.dir == Path("mailbox")
    switched = parser().parse_args(["--as", "console-two", "--from", "console-presence", "--to", "agent-two"])
    assert switched.agent_id == "console-two"
    assert switched.source == "console-presence"
    assert switched.destination == "agent-two"
    assert parser().parse_args(["--as", "agent-three"]).agent_id == "agent-three"
    assert parser().parse_args(["--as", "agent-three", "--on", "mm/chat.singularitynet.io"]).chat_instance == "mm/chat.singularitynet.io"


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
    assert relay_http_url("wss://relay.example/v1/chat/ws") == "https://relay.example"


def test_console_administration_uses_active_transport(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(console_client.contact_admin, "main", lambda argv: calls.append(("contacts", argv)) or 0)
    monkeypatch.setattr(console_client.token_admin, "main", lambda argv: calls.append(("token", argv)) or 0)

    assert run_administration("contacts", "list", url="http://relay:46667", directory=tmp_path) == 0
    assert run_administration("token", "status", url="unused", directory=tmp_path) == 0
    assert calls == [
        ("contacts", ["--dir", str(tmp_path), "list"]),
        ("token", ["status"]),
    ]


def test_any_console_slash_command_dispatches_to_mailbox_client(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(console_client.agent_mailbox, "main", lambda argv: calls.append(argv) or 0)
    assert run_client_command(
        'send "hello there"', identity="agent-one", destination="agent-two",
        url="unused", directory=tmp_path,
    ) == 0
    assert calls == [[
        "--dir", str(tmp_path), "--as", "agent-one", "--to", "agent-two",
        "send", "hello there",
    ]]


def test_console_default_on_is_injected_but_qualified_address_wins(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(console_client.chat_admin, "main", lambda argv: calls.append(argv) or 0)
    run_client_command("list", identity="agent", destination="unused",
                       url="http://relay:46667", directory=None,
                       chat_instance="mm/chat.singularitynet.io")
    run_client_command("names irc/0/%23agents", identity="agent", destination="unused",
                       url="http://relay:46667", directory=None,
                       chat_instance="mm/chat.singularitynet.io")
    assert "--on" in calls[0] and "mm/chat.singularitynet.io" in calls[0]
    assert "--on" not in calls[1]


def test_console_on_command_changes_persistent_chat_instance(monkeypatch, tmp_path, capsys) -> None:
    class ThreadWithoutReceiver:
        def __init__(self, **_kwargs): pass
        def start(self): pass

    entries = iter(["/on mm/chat.singularitynet.io", "/on", "/quit"])
    monkeypatch.setattr(console_client.threading, "Thread", ThreadWithoutReceiver)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(entries))
    assert run("agent", "unused", "unused", directory=tmp_path) == 0
    output = capsys.readouterr().out
    assert "Chat platform changed to mm/chat.singularitynet.io" in output
    assert "Chat platform is mm/chat.singularitynet.io" in output


def test_console_on_qualified_address_changes_conversation(monkeypatch, tmp_path, capsys) -> None:
    class ThreadWithoutReceiver:
        def __init__(self, **_kwargs): pass
        def start(self): pass

    entries = iter(["/on mm/chat.singularitynet.io/Town Hypercube", "hello", "/quit"])
    monkeypatch.setattr(console_client.threading, "Thread", ThreadWithoutReceiver)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(entries))
    monkeypatch.setattr(console_client, "run_client_command",
                        lambda command, **kwargs: calls.append((command, kwargs)) or 0)
    calls = []
    assert run("agent", "unused", "unused", directory=tmp_path) == 0
    assert calls[0][1]["destination"] == "mm/chat.singularitynet.io/Town%20Hypercube"
    assert "Chat conversation changed to mm/chat.singularitynet.io/Town%20Hypercube" in capsys.readouterr().out


def test_console_client_reports_unavailable_service(monkeypatch, capsys) -> None:
    monkeypatch.setattr("mailbox_channels.console_client.websocket.create_connection", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")))
    assert run("console-one", "agent-two", "ws://127.0.0.1:1/v1/chat/ws") == 2
    assert "Mailbox Console service unavailable" in capsys.readouterr().err


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


def test_trusted_speaker_can_change_default_agent_id(monkeypatch, tmp_path, capsys) -> None:
    class ThreadWithoutReceiver:
        def __init__(self, **_kwargs): pass
        def start(self): pass

    entries = iter(["/as agent-two", "/as", "sent as two", "/quit"])
    monkeypatch.setattr(console_client.threading, "Thread", ThreadWithoutReceiver)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(entries))

    assert run("agent-one", "recipient", "unused", directory=tmp_path) == 0
    assert agent_mailbox.receive("recipient", root=tmp_path)[0]["from"] == "agent-two"
    output = capsys.readouterr().out
    assert "Agent ID changed to agent-two" in output
    assert "Agent ID is agent-two" in output


def test_trusted_speaker_can_change_source_presence(monkeypatch, tmp_path) -> None:
    class ThreadWithoutReceiver:
        def __init__(self, **_kwargs): pass
        def start(self): pass

    entries = iter(["/from symbolic-mm", "hello", "/quit"])
    monkeypatch.setattr(console_client.threading, "Thread", ThreadWithoutReceiver)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(entries))

    assert run("symbolic-workbench-codex", "recipient", "unused", directory=tmp_path) == 0
    message = agent_mailbox.receive("recipient", root=tmp_path)[0]
    assert message["from"] == "symbolic-workbench-codex"
    assert message["source_presence"] == "symbolic-mm"


def test_console_can_switch_local_mailbox_directory(monkeypatch, tmp_path) -> None:
    class ThreadWithoutReceiver:
        def __init__(self, **_kwargs): pass
        def start(self): pass

    first = tmp_path / "first"
    second = tmp_path / "second"
    entries = iter([f"/dir {second}", "after switch", "/quit"])
    monkeypatch.setattr(console_client.threading, "Thread", ThreadWithoutReceiver)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(entries))

    assert run("agent-one", "recipient", "unused", directory=first) == 0
    assert agent_mailbox.receive("recipient", root=first) == []
    assert agent_mailbox.receive("recipient", root=second)[0]["text"] == "after switch"


def test_console_can_emulate_external_channel_presence(monkeypatch, tmp_path) -> None:
    class ThreadWithoutReceiver:
        def __init__(self, **_kwargs): pass
        def start(self): pass

    entries = iter(["/join irc/0/agents", "hello channel", "/quit"])
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))
    monkeypatch.setattr(console_client.threading, "Thread", ThreadWithoutReceiver)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(entries))

    assert run("agent-one", "unused", "unused", directory=tmp_path) == 0
    assert "irc-0-agents" in __import__(
        "mailbox_channels.subscriptions", fromlist=["subscriptions"],
    ).subscriptions("agent-one")
    relayed = agent_mailbox.receive("outbound_delivery", root=tmp_path)[0]
    assert relayed["from"] == "agent-one"
    assert relayed["endpoint_address"] == "irc/0/%23agents"


def test_temporary_console_switch_unsubscribes_previous_view(monkeypatch, tmp_path) -> None:
    class ThreadWithoutReceiver:
        def __init__(self, **_kwargs): pass
        def start(self): pass

    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))
    entries = iter(["/console irc/0/one", "/console irc/0/two", "/quit"])
    monkeypatch.setattr(console_client.threading, "Thread", ThreadWithoutReceiver)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(entries))
    assert run("agent-one", "unused", "unused", directory=tmp_path) == 0
    subscriptions = __import__(
        "mailbox_channels.subscriptions", fromlist=["subscriptions"],
    ).subscriptions("agent-one")
    assert "irc-0-one" not in subscriptions
    assert "irc-0-two" in subscriptions
