import json

from mailbox_channel_relay_bridging_proxy.console_client import CLIENT_NAME, display_event, parser, run


def test_console_client_arguments() -> None:
    arguments = parser().parse_args(["console-one", "--to", "irc-bridge-agent"])
    assert arguments.identity == "console-one"
    assert arguments.destination == "irc-bridge-agent"
    assert CLIENT_NAME == "Trusted Speaker"
    assert parser().prog == "trusted-speaker"


def test_console_client_formats_mailbox_message() -> None:
    rendered = display_event(json.dumps({
        "type": "message",
        "message": {"from": "irc:nick", "to": "console-one", "text": "hello"},
    }))
    assert rendered == "[irc:nick -> console-one] hello"


def test_console_client_reports_unavailable_service(monkeypatch, capsys) -> None:
    monkeypatch.setattr("mailbox_channel_relay_bridging_proxy.console_client.websocket.create_connection", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")))
    assert run("console-one", "agent-two", "ws://127.0.0.1:1/v1/chat/ws") == 2
    assert "Trusted Speaker service unavailable" in capsys.readouterr().err
