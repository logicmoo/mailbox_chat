import json
from pathlib import Path

import pytest

from conftest import seed_store

from mailbox_channels import channel_store
from mailbox_channels.connector_registry import (
    agent_for_presence, config_dir, connectors_file, load_agents, load_connectors, register_agent,
    relays_file, unregister_agent,
)
from mailbox_channels.agent_mailbox import peek


def test_connector_registry_expands_environment_channel_lists(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_CHANNELS", "one,two")
    seed_store(tmp_path, connectors=[{
        "id": "test",
        "adapter": "mattermost",
        "channel_ids": ["$TEST_CHANNELS"],
        "mailbox_recipients": ["alpha", "alpha", "beta"],
    }])
    connector = load_connectors(tmp_path)[0]
    assert connector["channel_ids"] == ["one", "two"]
    assert connector["mailbox_recipients"] == ["alpha", "beta"]


def test_connector_registry_rejects_duplicate_ids(tmp_path: Path) -> None:
    seed_store(tmp_path, connectors=[{"id": "same", "adapter": "irc"}])
    # Force a duplicate ordinal slot pointing at the same entry to exercise the
    # uniqueness guard (the entries map itself cannot hold duplicate ids).
    document = channel_store.load_registry(tmp_path, channel_store.CONNECTORS_REGISTRY)
    document["ordinal"]["entry_1"] = "same"
    document["count"] = 2
    channel_store.save_channel(tmp_path, document)
    with pytest.raises(ValueError, match="unique"):
        load_connectors(tmp_path)


def test_config_directory_environment_selects_connector_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(tmp_path))
    assert config_dir() == tmp_path.resolve()
    assert relays_file() == tmp_path.resolve() / "relays.json"
    assert connectors_file() == tmp_path.resolve() / "relays.json"


def test_registry_falls_back_to_legacy_connectors_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(tmp_path))
    legacy = tmp_path / "connectors.json"
    legacy.write_text('{"version": 1, "connectors": []}', encoding="utf-8")
    assert relays_file() == legacy


def test_agent_can_own_multiple_presences_and_connector_delivers_to_agent(tmp_path: Path) -> None:
    seed_store(
        tmp_path,
        agents=[{
            "agent_id": "symbolic-workbench-codex",
            "presences": [
                {"presence_id": "symbolic-codex-app"},
                {"presence_id": "symbolic-console"},
                {"presence_id": "symbolic-mm"},
                {"presence_id": "symbolic-wa"},
            ],
        }],
        connectors=[{
            "id": "mm", "adapter": "mattermost",
            "agent_id": "symbolic-workbench-codex", "presence_id": "symbolic-mm",
        }],
    )
    assert len(load_agents(tmp_path)[0]["presences"]) == 4
    assert agent_for_presence("symbolic-wa", tmp_path) == "symbolic-workbench-codex"
    assert load_connectors(tmp_path)[0]["mailbox_recipients"] == ["symbolic-workbench-codex"]


def test_connector_rejects_presence_owned_by_another_agent(tmp_path: Path) -> None:
    seed_store(
        tmp_path,
        agents=[
            {"agent_id": "one", "presences": [{"presence_id": "one-mm"}]},
            {"agent_id": "two", "presences": []},
        ],
        connectors=[{
            "id": "mm", "adapter": "mattermost", "agent_id": "two", "presence_id": "one-mm",
        }],
    )
    with pytest.raises(ValueError, match="different agent"):
        load_connectors(tmp_path)


def test_new_agent_and_presence_are_announced_on_server_events_channel(tmp_path: Path) -> None:
    mailbox = tmp_path / "mailbox"

    result = register_agent(
        "worker-one", presence_id="worker-one-app",
        path=mailbox, mailbox_root=mailbox,
    )

    assert result["agent_created"] is True
    assert result["presence_created"] is True
    assert result["agent"]["mailbox"] == "worker_one"
    messages = peek("server_events_log", root=mailbox)
    assert [(item["type"], item["agent_id"]) for item in messages] == [
        ("agent_registered", "worker-one"),
        ("presence_registered", "worker-one"),
    ]
    assert messages[1]["presence_id"] == "worker-one-app"
    assert "presence_kind" not in messages[1]


def test_reregistering_agent_and_presence_is_idempotent_and_silent(tmp_path: Path) -> None:
    mailbox = tmp_path / "mailbox"
    register_agent("worker-one", presence_id="worker-one-console",
                   path=mailbox, mailbox_root=mailbox)

    result = register_agent("worker-one", presence_id="worker-one-console",
                            path=mailbox, mailbox_root=mailbox)

    assert result["agent_created"] is False
    assert result["presence_created"] is False
    assert len(peek("server_events_log", root=mailbox)) == 2


def test_new_presence_for_existing_agent_gets_its_own_event(tmp_path: Path) -> None:
    mailbox = tmp_path / "mailbox"
    register_agent("worker-one", path=mailbox, mailbox_root=mailbox)

    result = register_agent(
        "worker-one", presence_id="worker-one-mm",
        path=mailbox, mailbox_root=mailbox,
    )

    assert result["agent_created"] is False
    assert result["presence_created"] is True
    messages = peek("server_events_log", root=mailbox)
    assert [item["type"] for item in messages] == ["agent_registered", "presence_registered"]


def test_unregister_refuses_agent_or_presence_referenced_by_connector(tmp_path: Path) -> None:
    mailbox = tmp_path / "mailbox"
    seed_store(
        mailbox,
        agents=[{
            "agent_id": "worker-one",
            "presences": [{"presence_id": "worker-one-mm"}],
        }],
        connectors=[{
            "id": "mm", "adapter": "mattermost", "agent_id": "worker-one",
            "presence_id": "worker-one-mm",
        }],
    )

    with pytest.raises(ValueError, match="referenced by connector.*mm"):
        unregister_agent("worker-one", presence_id="worker-one-mm", path=mailbox)
    with pytest.raises(ValueError, match="referenced by connector.*mm"):
        unregister_agent("worker-one", path=mailbox)
    assert load_agents(mailbox)[0]["agent_id"] == "worker-one"

