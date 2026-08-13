from pathlib import Path

from mailbox_channels import agent_mailbox
from mailbox_channels.attachment_gateway import attachment_url


def test_attachment_url_only_accepts_managed_attachments(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(agent_mailbox.MAILBOX_ENV, str(tmp_path))
    source = tmp_path / "source.png"
    source.write_bytes(b"png")
    record = agent_mailbox.send("agent", "image", attachments=[source], root=tmp_path)["attachments"][0]
    assert attachment_url(record, "https://relay.example/").startswith(
        "https://relay.example/v1/attachments/"
    )
