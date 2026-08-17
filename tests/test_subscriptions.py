import json

from mailbox_channels import agent_mailbox
from mailbox_channels.subscriptions import (
    available_buses, channel_bus, set_subscription, subscribers, subscriptions,
)


def test_monitored_sources_have_stable_polling_bus_names(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text(json.dumps({
        "version": 1,
        "listeners": [{
            "id": "mattermost-primary", "adapter": "mattermost", "enabled": True,
            "direction": "inbound", "base_url": "https://chat.singularitynet.io",
            "channel_ids": ["channel-123"], "mailbox_recipients": [],
        }],
    }), encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    assert channel_bus("local/0/server_events") == "mailbox-server-events-bus"
    sources = available_buses()
    assert {item["bus"] for item in sources} == {
        "mailbox-server-events-bus", "mailbox-server-agent-to-agent-bus",
        "mailbox-server-agent-to-channel-bus", "chat-singularitynet-io-mm-channel-123-bus",
    }


def test_mattermost_bus_is_derived_from_endpoint_workspace_and_channel() -> None:
    assert channel_bus(
        "mm/chat.singularitynet.io/opaque-channel-id",
        workspace="OpenCog Hyperon", channel_name="Agent Test",
    ) == "chat-singularitynet-io-mm-opencog-hyperon-agent-test-bus"
    assert channel_bus("mm/community.example/opaque-channel-id") == (
        "community-example-mm-opaque-channel-id-bus"
    )


def test_available_buses_reuses_resolved_bus_metadata(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text(json.dumps({
        "version": 1,
        "subscriptions": [{
            "id": "mm/chat.singularitynet.io/opaque-id",
            "subscribers": [],
            "metadata": {"bus": "snet-mm-team-test-bus"},
        }],
        "listeners": [],
    }), encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    source = next(item for item in available_buses() if item["channel"].endswith("/opaque-id"))
    assert source["bus"] == "snet-mm-team-test-bus"


def test_registered_agents_have_discoverable_presence_ingress_buses(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text(json.dumps({
        "version": 1,
        "agents": [{"agent_id": "workspace-codex-agent", "presences": []}],
        "listeners": [],
    }), encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    source = next(
        item for item in available_buses()
        if item["bus"] == "mailbox-server-presence-to-workspace-codex-agent"
    )
    assert source["metadata"] == {
        "kind": "agent_presence_ingress", "agent_id": "workspace-codex-agent",
    }


def test_local_channel_subscription_is_durable(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    path = config / "relays.json"
    path.write_text('{"version":1,"listeners":[]}', encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    set_subscription("local/0/server_events", "symbolic-workbench-codex", enabled=True)
    assert subscribers("local/0/server_events") == ["symbolic-workbench-codex"]
    assert subscriptions("symbolic-workbench-codex") == ["local/0/server_events"]
    assert json.loads(path.read_text(encoding="utf-8"))["subscriptions"][0]["subscribers"] == [
        "symbolic-workbench-codex",
    ]
    set_subscription("local/0/server_events", "symbolic-workbench-codex", enabled=False)
    assert subscribers("local/0/server_events") == []


def test_mailbox_client_manages_local_channel_subscription(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text('{"version":1,"listeners":[]}', encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    assert agent_mailbox.main(["subscribe", "local/0/server_events", "--to", "omegaclaw-min"]) == 0
    assert json.loads(capsys.readouterr().out)["subscribed"] is True
    assert agent_mailbox.main(["subscriptions", "--to", "omegaclaw-min"]) == 0
    assert json.loads(capsys.readouterr().out)["channels"] == ["local/0/server_events"]
    assert agent_mailbox.main(["unsubscribe", "local/0/server_events", "--to", "omegaclaw-min"]) == 0
    assert json.loads(capsys.readouterr().out)["subscribed"] is False


def test_mailbox_client_lists_every_subscribed_mailbox(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text('{"version":1,"listeners":[]}', encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))
    set_subscription("local/0/server_events", "agent-two", enabled=True)
    set_subscription("mm/0/channel-one", "agent-one", enabled=True)
    set_subscription("local/0/server_events", "agent-one", enabled=True)

    assert agent_mailbox.main(["subscriptions", "--all"]) == 0
    assert json.loads(capsys.readouterr().out) == {"mailboxes": [
        {"identity": "agent-one", "channels": ["local/0/server_events", "mm/0/channel-one"]},
        {"identity": "agent-two", "channels": ["local/0/server_events"]},
    ]}


def test_poll_can_idempotently_ensure_all_declared_subscriptions(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text('{"version":1,"listeners":[]}', encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    arguments = [
        "--dir", str(tmp_path / "mailbox"), "poll", "--to", "symbolic-workbench-codex",
        "--subscribed", "local/0/server_events,mm/0/3423423434234,mm/0/2342444444444",
        "--checks", "1",
    ]
    assert agent_mailbox.main(arguments) == 0
    assert agent_mailbox.main(arguments) == 0
    capsys.readouterr()
    assert subscriptions("symbolic-workbench-codex") == [
        "local/0/server_events", "mm/0/3423423434234", "mm/0/2342444444444",
    ]
