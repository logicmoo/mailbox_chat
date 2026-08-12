import json
from pathlib import Path

import pytest

from mailbox_channel_relay_bridging_proxy.listener_registry import config_dir, listeners_file, load_listeners, relays_file


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
