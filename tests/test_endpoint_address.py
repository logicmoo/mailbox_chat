import json

from mailbox_channels import agent_mailbox
from mailbox_channels.endpoint_address import parse_endpoint, subscription_recipients
from mailbox_channels.subscriptions import subscriptions


def test_mattermost_endpoint_address_parses_canonically() -> None:
    endpoint = parse_endpoint("mm/Chat.SNT/abc123")
    assert endpoint is not None
    assert (endpoint.adapter, endpoint.instance, endpoint.identifier) == (
        "mattermost", "chat.snt", "abc123",
    )
    assert endpoint.canonical == "mm/chat.snt/abc123"


def test_zero_instance_is_canonical_default_instance(monkeypatch) -> None:
    endpoint = parse_endpoint("mm/0/channel-1")
    assert endpoint is not None
    assert endpoint.canonical == "mm/0/channel-1"
    monkeypatch.setattr(
        "mailbox_channels.subscriptions.subscribers",
        lambda address: ["default-agent"] if address == "mm/0/channel-1" else ["specific-agent"],
    )
    assert subscription_recipients(
        "mattermost", {"id": "primary", "instance": "chat.snt"}, "channel-1",
    ) == ["specific-agent", "default-agent"]


def test_endpoint_descriptions_have_common_typed_properties() -> None:
    status = parse_endpoint("irc/0/status")
    channel = parse_endpoint("local/0/debug-console")
    assert status is not None and status.describe()["type"] == "status"
    assert channel is not None and channel.describe(properties={"topic": "debug"}) == {
        "address": "local/0/debug-console", "platform": "local", "adapter": "local",
        "instance": "0", "id": "debug-console", "type": "channel",
        "properties": {"topic": "debug"},
    }


def test_every_platform_endpoint_type_round_trips() -> None:
    expected = {
        "mm": "mattermost", "discord": "discord", "slack": "slack", "matrix": "matrix",
        "irc": "irc", "telegram": "telegram", "wab": "whatsapp",
        "wa": "whatsapp_personal", "facebook": "facebook_messenger",
        "viber": "viber", "line": "line", "discourse": "discourse",
    }
    for address_type, adapter in expected.items():
        endpoint = parse_endpoint(f"{address_type}/instance/source-destination")
        assert endpoint is not None
        assert endpoint.adapter == adapter
        assert endpoint.canonical == f"{address_type}/instance/source-destination"

    irc = parse_endpoint("irc/irc.libera.chat/%23agents")
    assert irc is not None and irc.identifier == "#agents"
    assert irc.canonical == "irc/irc.libera.chat/%23agents"


def test_remote_from_subscribes_as_local_identity(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text('{"version":1,"listeners":[]}', encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))
    assert agent_mailbox.main([
        "--dir", str(tmp_path / "mailbox"), "poll", "--as", "symbolic-workbench-codex",
        "--from", "mm/chat.snt/channel-1", "--checks", "1",
    ]) == 0
    capsys.readouterr()
    assert subscriptions("symbolic-workbench-codex") == ["mm/chat.snt/channel-1"]


def test_remote_to_becomes_channel_relay_request(tmp_path, capsys) -> None:
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "send", "--as", "symbolic-workbench-codex",
        "--to", "mm/chat.snt/person-1", "hello",
    ]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert sent["from"] == "symbolic-workbench-codex"
    assert sent["to"] == "channel-relay"
    assert sent["channel_type"] == "mattermost"
    assert sent["channel_id"] == "person-1"
    assert sent["endpoint_address"] == "mm/chat.snt/person-1"
