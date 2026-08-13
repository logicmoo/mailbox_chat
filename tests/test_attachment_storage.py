from pathlib import Path

import pytest

from mailbox_channels import agent_mailbox
from mailbox_channels.attachment_storage import MAX_FILE_ENV, MAX_STORAGE_ENV


def test_rejects_attachment_larger_than_per_file_limit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(MAX_FILE_ENV, "3")
    monkeypatch.setenv(MAX_STORAGE_ENV, "100")
    source = tmp_path / "large.bin"
    source.write_bytes(b"four")
    with pytest.raises(ValueError, match="maximum is 3 bytes"):
        agent_mailbox.send("agent", "file", attachments=[source], root=tmp_path / "mailbox")


def test_rejects_attachment_when_total_storage_quota_is_full(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(MAX_FILE_ENV, "10")
    monkeypatch.setenv(MAX_STORAGE_ENV, "6")
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"1234")
    second.write_bytes(b"567")
    root = tmp_path / "mailbox"
    agent_mailbox.send("agent", "first", attachments=[first], root=root)
    with pytest.raises(ValueError, match="storage quota exceeded"):
        agent_mailbox.send("agent", "second", attachments=[second], root=root)


def test_rejects_message_when_jsonl_quota_is_full(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(agent_mailbox.MAX_JSONL_ENV, "1")
    with pytest.raises(ValueError, match="JSONL mailbox quota exceeded"):
        agent_mailbox.send("agent", "too large", root=tmp_path / "mailbox")
