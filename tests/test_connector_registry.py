import json
from pathlib import Path

import pytest

from mailbox_channels.connector_registry import (
    agent_for_presence, config_dir, connectors_file, load_agents, load_connectors, register_agent,
    relays_file, unregister_agent,
)
from mailbox_channels.agent_mailbox import peek


def test_connector_registry_expands_environment_channel_lists(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_CHANNELS", "one,two")
    path = tmp_path / "connectors.json"
    path.write_text(json.dumps({
        "version": 1,
        "connectors": [{
            "id": "test",
            "adapter": "mattermost",
            "channel_ids": ["$TEST_CHANNELS"],
            "mailbox_recipients": ["alpha", "alpha", "beta"],
        }],
    }), encoding="utf-8")
    connector = load_connectors(path)[0]
    assert connector["channel_ids"] == ["one", "two"]
    assert connector["mailbox_recipients"] == ["alpha", "beta"]


def test_connector_registry_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = tmp_path / "connectors.json"
    path.write_text(json.dumps({
        "version": 1,
        "connectors": [
            {"id": "same", "adapter": "irc"},
            {"id": "same", "adapter": "discord"},
        ],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_connectors(path)


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
    path = tmp_path / "relays.json"
    path.write_text(json.dumps({
        "version": 1,
        "agents": [{
            "agent_id": "symbolic-workbench-codex",
            "presences": [
                {"presence_id": "symbolic-codex-app"},
                {"presence_id": "symbolic-console"},
                {"presence_id": "symbolic-mm"},
                {"presence_id": "symbolic-wa"},
            ],
        }],
        "connectors": [{
            "id": "mm", "adapter": "mattermost",
            "agent_id": "symbolic-workbench-codex", "presence_id": "symbolic-mm",
        }],
    }), encoding="utf-8")
    assert len(load_agents(path)[0]["presences"]) == 4
    assert agent_for_presence("symbolic-wa", path) == "symbolic-workbench-codex"
    assert load_connectors(path)[0]["mailbox_recipients"] == ["symbolic-workbench-codex"]


def test_connector_rejects_presence_owned_by_another_agent(tmp_path: Path) -> None:
    path = tmp_path / "relays.json"
    path.write_text(json.dumps({
        "version": 1,
        "agents": [
            {"agent_id": "one", "presences": [{"presence_id": "one-mm"}]},
            {"agent_id": "two", "presences": []},
        ],
        "connectors": [{
            "id": "mm", "adapter": "mattermost", "agent_id": "two", "presence_id": "one-mm",
        }],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="different agent"):
        load_connectors(path)


def test_new_agent_and_presence_are_announced_on_server_events_channel(tmp_path: Path) -> None:
    path = tmp_path / "config" / "relays.json"
    mailbox = tmp_path / "mailbox"

    result = register_agent(
        "worker-one", presence_id="worker-one-app",
        path=path, mailbox_root=mailbox,
    )

    assert result["agent_created"] is True
    assert result["presence_created"] is True
    assert result["agent"]["mailbox"] == "worker-one"
    messages = peek("server_events", root=mailbox)
    assert [(item["type"], item["agent_id"]) for item in messages] == [
        ("agent_registered", "worker-one"),
        ("presence_registered", "worker-one"),
    ]
    assert messages[1]["presence_id"] == "worker-one-app"
    assert "presence_kind" not in messages[1]


def test_reregistering_agent_and_presence_is_idempotent_and_silent(tmp_path: Path) -> None:
    path = tmp_path / "config" / "relays.json"
    mailbox = tmp_path / "mailbox"
    register_agent("worker-one", presence_id="worker-one-console",
                   path=path, mailbox_root=mailbox)

    result = register_agent("worker-one", presence_id="worker-one-console",
                            path=path, mailbox_root=mailbox)

    assert result["agent_created"] is False
    assert result["presence_created"] is False
    assert len(peek("server_events", root=mailbox)) == 2


def test_new_presence_for_existing_agent_gets_its_own_event(tmp_path: Path) -> None:
    path = tmp_path / "config" / "relays.json"
    mailbox = tmp_path / "mailbox"
    register_agent("worker-one", path=path, mailbox_root=mailbox)

    result = register_agent(
        "worker-one", presence_id="worker-one-mm",
        path=path, mailbox_root=mailbox,
    )

    assert result["agent_created"] is False
    assert result["presence_created"] is True
    messages = peek("server_events", root=mailbox)
    assert [item["type"] for item in messages] == ["agent_registered", "presence_registered"]


def test_unregister_refuses_agent_or_presence_referenced_by_connector(tmp_path: Path) -> None:
    path = tmp_path / "config" / "relays.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "version": 1,
        "agents": [{
            "agent_id": "worker-one",
            "presences": [{"presence_id": "worker-one-mm"}],
        }],
        "connectors": [{
            "id": "mm", "adapter": "mattermost", "agent_id": "worker-one",
            "presence_id": "worker-one-mm",
        }],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="referenced by connector.*mm"):
        unregister_agent("worker-one", presence_id="worker-one-mm", path=path)
    with pytest.raises(ValueError, match="referenced by connector.*mm"):
        unregister_agent("worker-one", path=path)
    assert load_agents(path)[0]["agent_id"] == "worker-one"
