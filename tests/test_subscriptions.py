import json

from mailbox_channels import agent_mailbox
from mailbox_channels.subscriptions import (
    available_sources, set_subscription, subscribers, subscriptions,
)


def test_monitored_sources_use_canonical_channel_addresses(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text(json.dumps({
        "version": 1,
        "connectors": [{
            "id": "mattermost-primary", "adapter": "mattermost", "enabled": True,
            "direction": "inbound", "base_url": "https://chat.singularitynet.io",
            "channel_ids": ["channel-123"], "mailbox_recipients": [],
        }],
    }), encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    sources = available_sources()
    assert {item["id"] for item in sources} == {
        "server_events", "agent_to_agent",
        "agent_to_channel", "mm/chat.singularitynet.io/channel-123",
    }
    assert {item["kind"] for item in sources} == {"channel"}
    assert {item["channel_type"] for item in sources} == {"system", "audit", "mattermost"}
    assert all(set(item) == {"id", "kind", "channel_type", "subscribers", "metadata"}
               for item in sources)


def test_local_audit_subscription_uses_one_canonical_channel(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text(json.dumps({
        "version": 1,
        "subscriptions": [{
            "id": "agent_to_agent",
            "subscribers": ["worker"],
        }],
        "connectors": [],
    }), encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    matches = [
        item for item in available_sources()
        if item["id"] == "agent_to_agent"
    ]
    assert matches == [{
        "id": "agent_to_agent",
        "kind": "channel",
        "channel_type": "audit",
        "subscribers": ["worker"],
        "metadata": {"scope": "direct_agent"},
    }]


def test_available_sources_keep_channel_metadata_without_mapping(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text(json.dumps({
        "version": 1,
        "subscriptions": [{
            "id": "mm/chat.singularitynet.io/opaque-id",
            "subscribers": [],
            "metadata": {"channel_name": "test"},
        }],
        "connectors": [],
    }), encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    source = next(item for item in available_sources() if item["id"].endswith("/opaque-id"))
    assert source == {
        "id": "mm/chat.singularitynet.io/opaque-id",
        "kind": "channel",
        "channel_type": "mattermost",
        "subscribers": [],
        "metadata": {"channel_name": "test"},
    }


def test_registered_agents_are_exposed_as_direct_channels(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text(json.dumps({
        "version": 1,
        "agents": [{"agent_id": "workspace-codex-agent", "presences": []}],
        "connectors": [],
    }), encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    direct = next(item for item in available_sources() if item["id"] == "workspace-codex-agent")
    assert direct == {
        "id": "workspace-codex-agent",
        "kind": "channel",
        "channel_type": "agent_direct",
        "subscribers": [],
        "metadata": {"agent_id": "workspace-codex-agent"},
    }


def test_local_channel_subscription_is_durable(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    path = config / "relays.json"
    path.write_text('{"version":1,"connectors":[]}', encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    set_subscription("server_events", "symbolic-workbench-codex", enabled=True)
    assert subscribers("server_events") == ["symbolic-workbench-codex"]
    assert subscriptions("symbolic-workbench-codex") == ["server_events"]
    assert json.loads(path.read_text(encoding="utf-8"))["subscriptions"][0]["subscribers"] == [
        "symbolic-workbench-codex",
    ]
    set_subscription("server_events", "symbolic-workbench-codex", enabled=False)
    assert subscribers("server_events") == []


def test_mailbox_client_manages_local_channel_subscription(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text('{"version":1,"connectors":[]}', encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    assert agent_mailbox.main(["subscribe", "server_events", "--to", "omegaclaw-min"]) == 0
    assert json.loads(capsys.readouterr().out)["subscribed"] is True
    assert agent_mailbox.main(["subscriptions", "--to", "omegaclaw-min"]) == 0
    assert json.loads(capsys.readouterr().out)["channels"] == ["server_events"]
    assert agent_mailbox.main(["unsubscribe", "server_events", "--to", "omegaclaw-min"]) == 0
    assert json.loads(capsys.readouterr().out)["subscribed"] is False


def test_mailbox_client_lists_every_subscribed_mailbox(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text('{"version":1,"connectors":[]}', encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))
    set_subscription("server_events", "agent-two", enabled=True)
    set_subscription("mm/0/channel-one", "agent-one", enabled=True)
    set_subscription("server_events", "agent-one", enabled=True)

    assert agent_mailbox.main(["subscriptions", "--all"]) == 0
    assert json.loads(capsys.readouterr().out) == {"mailboxes": [
        {"identity": "agent-one", "channels": ["server_events", "mm/0/channel-one"]},
        {"identity": "agent-two", "channels": ["server_events"]},
    ]}


def test_poll_can_idempotently_ensure_all_declared_subscriptions(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text('{"version":1,"connectors":[]}', encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    arguments = [
        "--dir", str(tmp_path / "mailbox"), "poll", "--to", "symbolic-workbench-codex",
        "--subscribed", "server_events,mm/0/3423423434234,mm/0/2342444444444",
        "--checks", "1",
    ]
    assert agent_mailbox.main(arguments) == 0
    assert agent_mailbox.main(arguments) == 0
    capsys.readouterr()
    assert subscriptions("symbolic-workbench-codex") == [
        "server_events", "mm/0/3423423434234", "mm/0/2342444444444",
    ]
