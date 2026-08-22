import json

from conftest import seed_store

from mailbox_channels import agent_mailbox, channel_store
from mailbox_channels.subscriptions import (
    available_sources, ensure_channel, set_subscription, subscribers, subscriptions,
)


def test_monitored_sources_use_resource_ids_not_transport_addresses(tmp_path, monkeypatch) -> None:
    mailbox = tmp_path / "mailbox"
    seed_store(mailbox, connectors=[{
        "id": "mattermost-primary", "adapter": "mattermost", "enabled": True,
        "direction": "inbound", "base_url": "https://chat.singularitynet.io",
        "channel_ids": ["channel-123"], "mailbox_recipients": [],
    }])

    sources = available_sources(mailbox)
    assert {item["id"] for item in sources} == {
        "server_events_log", "server_outbound_relay_agent_to_agent",
        "server_outbound_relay_agent_to_channel", "mm-chat-singularitynet-io-channel-123",
    }
    assert {item["kind"] for item in sources} == {"mailbox"}
    assert {item["channel_type"] for item in sources} == {"system", "audit", "mattermost"}
    assert all(set(item) == {"id", "kind", "channel_type", "aliases", "subscribers", "metadata"}
               for item in sources)


def test_local_audit_subscription_uses_one_canonical_channel(tmp_path, monkeypatch) -> None:
    mailbox = tmp_path / "mailbox"
    seed_store(mailbox, subscriptions=[{
        "id": "server_outbound_relay_agent_to_agent",
        "subscribers": ["worker"],
    }])

    matches = [
        item for item in available_sources(mailbox)
        if item["id"] == "server_outbound_relay_agent_to_agent"
    ]
    assert matches == [{
        "id": "server_outbound_relay_agent_to_agent",
        "kind": "mailbox",
        "channel_type": "audit",
        "aliases": [],
        "subscribers": ["worker"],
        "metadata": {"scope": "direct_agent"},
    }]


def test_available_sources_keep_channel_metadata_without_mapping(tmp_path, monkeypatch) -> None:
    mailbox = tmp_path / "mailbox"
    seed_store(
        mailbox,
        subscriptions=[{
            "id": "mm-chat-singularitynet-io-opaque-id",
            "subscribers": [],
            "metadata": {"channel_name": "test", "connector": "mattermost-primary"},
        }],
        connectors=[{"id": "mattermost-primary", "adapter": "mattermost"}],
    )

    source = next(item for item in available_sources(mailbox)
                  if item["id"] == "mm-chat-singularitynet-io-opaque-id")
    assert source == {
        "id": "mm-chat-singularitynet-io-opaque-id",
        "kind": "mailbox",
        "channel_type": "mattermost",
        "aliases": ["test"],
        "subscribers": [],
        "metadata": {"channel_name": "test", "connector": "mattermost-primary"},
    }


def test_registered_agents_are_exposed_as_direct_channels(tmp_path, monkeypatch) -> None:
    mailbox = tmp_path / "mailbox"
    seed_store(mailbox, agents=[{"agent_id": "workspace-codex-agent", "presences": []}])

    direct = next(item for item in available_sources(mailbox) if item["id"] == "workspace_codex_agent")
    assert direct == {
        "id": "workspace_codex_agent",
        "kind": "mailbox",
        "channel_type": "agent_direct",
        "aliases": [],
        "subscribers": [],
        "metadata": {"agent_id": "workspace-codex-agent"},
    }


def test_local_channel_subscription_is_durable(tmp_path, monkeypatch) -> None:
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir(parents=True)

    set_subscription("server_events_log", "symbolic-workbench-codex", enabled=True, path=mailbox)
    assert subscribers("server_events_log", mailbox) == ["symbolic-workbench-codex"]
    assert subscriptions("symbolic-workbench-codex", mailbox) == ["server_events_log"]
    stored = channel_store.get_entry(mailbox, channel_store.CHANNELS_REGISTRY, "server_events_log")
    assert stored["payload"]["subscribers"] == ["symbolic-workbench-codex"]
    set_subscription("server_events_log", "symbolic-workbench-codex", enabled=False, path=mailbox)
    assert subscribers("server_events_log", mailbox) == []


def test_mailbox_client_manages_local_channel_subscription(tmp_path, monkeypatch, capsys) -> None:
    mailbox = tmp_path / "mailbox"

    assert agent_mailbox.main([
        "--dir", str(mailbox), "subscribe", "server_events_log", "--to", "omegaclaw-min",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["subscribed"] is True
    assert agent_mailbox.main([
        "--dir", str(mailbox), "subscriptions", "--to", "omegaclaw-min",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["channels"] == ["server_events_log"]
    assert agent_mailbox.main([
        "--dir", str(mailbox), "unsubscribe", "server_events_log", "--to", "omegaclaw-min",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["subscribed"] is False


def test_channel_add_subscribe_all_resolves_alias_and_subscribes_agents(
    tmp_path, monkeypatch, capsys,
) -> None:
    mailbox = tmp_path / "mailbox"
    address = "mm/chat.example/channel-one"
    stable_id = "mm-chat-example-team-channel-one"
    seed_store(
        mailbox,
        agents=[
            {"agent_id": "agent-one", "presences": []},
            {"agent_id": "agent-two", "presences": []},
        ],
        connectors=[{
            "kind": "connector", "id": "mm-primary", "adapter": "mattermost",
            "direction": "bidirectional", "enabled": True, "channel_ids": [],
        }],
        subscriptions=[{
            "kind": "channel", "id": stable_id, "aliases": [address], "subscribers": [],
        }],
    )

    assert agent_mailbox.main([
        "--dir", str(mailbox), "channel-add", "mm/0/channel-one", "--subscribe-all",
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["channel"]["id"] == stable_id
    assert output["subscribed_agents"] == ["agent-one", "agent-two"]
    assert output["connector"] == {
        "connector_id": "mm-primary", "channel_id": "channel-one", "monitored": True,
    }
    assert subscribers(stable_id, mailbox) == ["agent-one", "agent-two"]
    assert ensure_channel(address, path=mailbox)["id"] == stable_id
    connector = channel_store.get_entry(mailbox, channel_store.CONNECTORS_REGISTRY, "mm-primary")
    assert connector["payload"]["channel_ids"] == ["channel-one"]
    assert agent_mailbox.cursor_subscriptions("agent-one", root=mailbox) == [stable_id]


def test_mailbox_client_lists_every_subscribed_mailbox(tmp_path, monkeypatch, capsys) -> None:
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir(parents=True)
    set_subscription("server_events_log", "agent-two", enabled=True, path=mailbox)
    set_subscription("mm/0/channel-one", "agent-one", enabled=True, path=mailbox)
    set_subscription("server_events_log", "agent-one", enabled=True, path=mailbox)

    assert agent_mailbox.main(["--dir", str(mailbox), "subscriptions", "--all"]) == 0
    assert json.loads(capsys.readouterr().out) == {"mailboxes": [
        {"identity": "agent-one", "channels": ["server_events_log", "mm-0-channel-one"]},
        {"identity": "agent-two", "channels": ["server_events_log"]},
    ]}


def test_poll_can_idempotently_ensure_all_declared_subscriptions(tmp_path, monkeypatch, capsys) -> None:
    mailbox = tmp_path / "mailbox"

    arguments = [
        "--dir", str(mailbox), "poll", "--to", "symbolic-workbench-codex",
        "--subscribed", "server_events_log,mm/0/3423423434234,mm/0/2342444444444",
        "--checks", "1",
    ]
    assert agent_mailbox.main(arguments) == 0
    assert agent_mailbox.main(arguments) == 0
    capsys.readouterr()
    assert subscriptions("symbolic-workbench-codex", mailbox) == [
        "server_events_log", "mm-0-3423423434234", "mm-0-2342444444444",
    ]


def test_poll_subscriptions_reads_new_registry_subscription_and_unblocks(
    tmp_path, monkeypatch, capsys,
) -> None:
    mailbox = tmp_path / "mailbox"
    seed_store(
        mailbox,
        agents=[{"agent_id": "agent-one", "presences": []}],
        subscriptions=[{
            "kind": "channel", "id": "new-channel", "subscribers": ["agent-one"],
        }],
    )
    agent_mailbox.send("new-channel", "arrived", root=mailbox)

    assert agent_mailbox.main([
        "--dir", str(mailbox), "poll", "--subscriptions", "--as", "agent-one",
        "--checks", "1", "--interval", "0",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == "arrived"
