import json
from pathlib import Path

import pytest

from mailbox_channels.listener_registry import (
    agent_for_presence, config_dir, listeners_file, load_agents, load_listeners, relays_file,
)


def test_listener_registry_expands_environment_channel_lists(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_CHANNELS", "one,two")
    path = tmp_path / "listeners.json"
    path.write_text(json.dumps({
        "version": 1,
        "listeners": [{
            "id": "test",
            "adapter": "mattermost",
            "channel_ids": ["$TEST_CHANNELS"],
            "mailbox_recipients": ["alpha", "alpha", "beta"],
        }],
    }), encoding="utf-8")
    listener = load_listeners(path)[0]
    assert listener["channel_ids"] == ["one", "two"]
    assert listener["mailbox_recipients"] == ["alpha", "beta"]


def test_listener_registry_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = tmp_path / "listeners.json"
    path.write_text(json.dumps({
        "version": 1,
        "listeners": [
            {"id": "same", "adapter": "irc"},
            {"id": "same", "adapter": "discord"},
        ],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_listeners(path)


def test_config_directory_environment_selects_listener_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(tmp_path))
    assert config_dir() == tmp_path.resolve()
    assert relays_file() == tmp_path.resolve() / "relays.json"
    assert listeners_file() == tmp_path.resolve() / "relays.json"


def test_registry_falls_back_to_legacy_listeners_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(tmp_path))
    legacy = tmp_path / "listeners.json"
    legacy.write_text('{"version": 1, "listeners": []}', encoding="utf-8")
    assert relays_file() == legacy


def test_agent_can_own_multiple_presences_and_listener_delivers_to_agent(tmp_path: Path) -> None:
    path = tmp_path / "relays.json"
    path.write_text(json.dumps({
        "version": 1,
        "agents": [{
            "agent_id": "symbolic-workbench-codex",
            "presences": [
                {"presence_id": "symbolic-codex-app", "kind": "codex"},
                {"presence_id": "symbolic-console", "kind": "console"},
                {"presence_id": "symbolic-mm", "kind": "platform"},
                {"presence_id": "symbolic-wa", "kind": "platform"},
            ],
        }],
        "listeners": [{
            "id": "mm", "adapter": "mattermost",
            "agent_id": "symbolic-workbench-codex", "presence_id": "symbolic-mm",
        }],
    }), encoding="utf-8")
    assert len(load_agents(path)[0]["presences"]) == 4
    assert agent_for_presence("symbolic-wa", path) == "symbolic-workbench-codex"
    assert load_listeners(path)[0]["mailbox_recipients"] == ["symbolic-workbench-codex"]


def test_listener_rejects_presence_owned_by_another_agent(tmp_path: Path) -> None:
    path = tmp_path / "relays.json"
    path.write_text(json.dumps({
        "version": 1,
        "agents": [
            {"agent_id": "one", "presences": [{"presence_id": "one-mm"}]},
            {"agent_id": "two", "presences": []},
        ],
        "listeners": [{
            "id": "mm", "adapter": "mattermost", "agent_id": "two", "presence_id": "one-mm",
        }],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="different agent"):
        load_listeners(path)
