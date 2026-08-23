"""Tests for the server_registry_mailboxes registry and mailbox-add/-del commands."""

import json
from pathlib import Path

import pytest

from mailbox_mailboxs import agent_mailbox, mailbox_registry


def test_register_infers_url_and_dir_transport(tmp_path: Path) -> None:
    url_entry = mailbox_registry.register_mailbox("research", "http://relay.example", path=tmp_path)
    assert url_entry["transport"] == "url"
    assert url_entry["location"] == "http://relay.example"

    dir_entry = mailbox_registry.register_mailbox("local", str(tmp_path / "inbox"), path=tmp_path)
    assert dir_entry["transport"] == "dir"

    listed = {item["id"] for item in mailbox_registry.load_mailboxes(tmp_path)}
    assert listed == {"research", "local"}


def test_register_rejects_duplicate_and_empty(tmp_path: Path) -> None:
    mailbox_registry.register_mailbox("research", "http://relay.example", path=tmp_path)
    with pytest.raises(ValueError):
        mailbox_registry.register_mailbox("research", "http://relay.example", path=tmp_path)
    with pytest.raises(ValueError):
        mailbox_registry.register_mailbox("", "http://relay.example", path=tmp_path)
    with pytest.raises(ValueError):
        mailbox_registry.register_mailbox("nolocation", "", path=tmp_path)


def test_register_dry_run_does_not_persist(tmp_path: Path) -> None:
    result = mailbox_registry.register_mailbox("research", "http://relay.example",
                                               dry_run=True, path=tmp_path)
    assert result["dry_run"] is True
    assert mailbox_registry.load_mailboxes(tmp_path) == []


def test_unregister_missing_is_noop(tmp_path: Path) -> None:
    assert mailbox_registry.unregister_mailbox("nope", path=tmp_path)["deleted"] is False


def test_load_mailboxes_scans_mailbox_documents(tmp_path: Path) -> None:
    from mailbox_mailboxs import mailbox_store

    mailbox_store.append_message(tmp_path, "worker-1", {"text": "hi"})
    mailbox_store.append_message(tmp_path, "worker-2", {"text": "yo"})
    mailbox_registry.register_mailbox("research", "http://relay.example", path=tmp_path)

    listed = {item["id"]: item for item in mailbox_registry.load_mailboxes(tmp_path)}
    assert {"worker-1", "worker-2", "research"} <= set(listed)
    # Scanned mailbox documents are reported as derived dir mailboxes.
    assert listed["worker-1"]["derived"] is True and listed["worker-1"]["transport"] == "dir"
    # The registry document itself is not a mailbox.
    assert "server_registry_mailboxes" not in listed


def test_mailbox_add_and_del_cli_updates_registry_and_config(tmp_path: Path, capsys) -> None:
    store = tmp_path / "store"
    config = tmp_path / "conf.json"

    assert agent_mailbox.main([
        "--dir", str(store), "--config", str(config),
        "mailbox-add", "research", "http://relay.example",
    ]) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["registry"]["transport"] == "url"
    assert added["named_mailbox"]["url"] == "http://relay.example"

    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["mailboxes"]["research"] == {"url": "http://relay.example"}
    assert [item["id"] for item in mailbox_registry.load_mailboxes(store)] == ["research"]

    assert agent_mailbox.main([
        "--dir", str(store), "--config", str(config), "mailbox-del", "research",
    ]) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["registry"]["deleted"] is True
    assert removed["named_mailbox"]["deleted"] is True
    assert json.loads(config.read_text(encoding="utf-8"))["mailboxes"] == {}
    assert mailbox_registry.load_mailboxes(store) == []
