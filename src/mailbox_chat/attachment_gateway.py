"""Generate safe relay-hosted URLs for mailbox attachment records."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from . import agent_mailbox


PUBLIC_URL_ENV = "MAILBOX_RELAY_PUBLIC_URL"
ATTACHMENT_PREFIX = "/v1/attachments/"


def attachment_relative_path(record: dict) -> Path:
    path = Path(str(record.get("path") or "")).expanduser().resolve(strict=True)
    root = (agent_mailbox.mailbox_dir() / "attachments").resolve()
    try:
        return path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"Attachment is outside the public mailbox attachment directory: {path}") from error


def attachment_url(record: dict, public_url: str | None = None) -> str:
    base = (public_url or os.environ.get(PUBLIC_URL_ENV) or "").rstrip("/")
    if not base:
        raise ValueError(f"{PUBLIC_URL_ENV} is required to publish attachment URLs")
    relative = attachment_relative_path(record)
    encoded = "/".join(quote(part, safe="") for part in relative.parts)
    return f"{base}{ATTACHMENT_PREFIX}{encoded}"
