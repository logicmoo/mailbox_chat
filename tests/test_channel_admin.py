import json

from mailbox_channels.channel_admin import create_channel, main
from mailbox_channels.subscriptions import channels


def test_create_local_qualified_channel(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))
    result = create_channel("local/0/debug-console", title="Debug console", topic="Diagnostics")
    assert result["address"] == "local/0/debug-console"
    assert channels()[0]["id"] == "local/0/debug-console"
    assert main(["create", "local/0/operations"]) == 0
    assert json.loads(capsys.readouterr().out)["address"] == "local/0/operations"


def test_bare_subscription_name_is_rejected(tmp_path) -> None:
    from mailbox_channels.subscriptions import set_subscription
    try:
        set_subscription("debug-console", "agent", enabled=True, path=tmp_path / "relays.json")
    except ValueError as error:
        assert "TYPE/INSTANCE/IDENTIFIER" in str(error)
    else:
        raise AssertionError("bare channel name was accepted")
