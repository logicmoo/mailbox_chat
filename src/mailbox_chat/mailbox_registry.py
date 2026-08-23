"""Register named mailboxes in the ``server_registry_mailboxes`` registry.

Each entry records a stable mailbox name and the transport used to reach it: a
local directory (``transport: "dir"``) or a relay REST origin
(``transport: "url"``). The transport is inferred from the location the same way
the client's named-mailbox config resolves it: an ``http(s)://`` value is a URL,
anything else is a directory. No secrets are stored here; REST tokens continue to
travel through ``AGENT_MAILBOX_TOKEN``.
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any

from . import mailbox_store
from .connector_registry import store_root

_REGISTRY_WRITE_LOCK = Lock()


def mailbox_transport(location: str) -> str:
    """Return ``"url"`` for an http(s) origin, otherwise ``"dir"``."""
    return "url" if str(location).startswith(("http://", "https://")) else "dir"


def _scanned_mailbox_ids(root: Path) -> list[str]:
    """Every message-mailbox document in the store; each ``.json`` file is a mailbox.

    The ``server_registry_*`` documents are ``ordered_id_mailbox`` registries, not
    mailboxes, so they are excluded from the scan.
    """
    if not root.is_dir():
        return []
    ids: list[str] = []
    for path in sorted(root.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(document, dict):
            continue
        if document.get("kind") != "mailbox" or document.get("sub_kind") == "ordered_id_mailbox":
            continue
        ids.append(str(document.get("id") or path.stem))
    return ids


def load_mailboxes(path: Path | None = None) -> list[dict[str, Any]]:
    """List mailboxes: explicit ``mailbox-add`` entries plus every stored mailbox document.

    Explicitly registered mailboxes come first (and win on id conflicts). When the
    registry is built from scratch it still reports every mailbox by scanning the
    store directory, because each per-mailbox ``.json`` document is a mailbox.
    """
    root = store_root(path)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in mailbox_store.registry_entries(root, mailbox_store.MAILBOXES_REGISTRY):
        mailbox_id = str(item["id"])
        result.append({
            "id": mailbox_id, "kind": "mailbox",
            "transport": item.get("transport") or mailbox_transport(item.get("location") or ""),
            "location": item.get("location") or "",
            "enabled": bool(item.get("enabled", True)),
        })
        seen.add(mailbox_id)
    for mailbox_id in _scanned_mailbox_ids(root):
        if mailbox_id in seen:
            continue
        result.append({"id": mailbox_id, "kind": "mailbox", "transport": "dir",
                       "location": str(root), "enabled": True, "derived": True})
        seen.add(mailbox_id)
    return result


def register_mailbox(mailbox_id: str, location: str, *, dry_run: bool = False,
                     path: Path | None = None) -> dict[str, Any]:
    """Register one named mailbox in the canonical registry."""
    mailbox_id, location = mailbox_id.strip(), location.strip()
    if not mailbox_id:
        raise ValueError("mailbox id is required")
    if not location:
        raise ValueError("mailbox location (directory or URL) is required")
    root = store_root(path)
    record = {
        "kind": "mailbox", "id": mailbox_id,
        "transport": mailbox_transport(location), "location": location,
        "enabled": True,
    }
    if dry_run:
        return {**record, "dry_run": True}
    with _REGISTRY_WRITE_LOCK:
        if mailbox_store.get_entry(root, mailbox_store.MAILBOXES_REGISTRY, mailbox_id):
            raise ValueError(f"mailbox id is already registered: {mailbox_id}")
        mailbox_store.put_entry(root, mailbox_store.MAILBOXES_REGISTRY, mailbox_id, record)
    return record


def unregister_mailbox(mailbox_id: str, *, dry_run: bool = False,
                       path: Path | None = None) -> dict[str, Any]:
    """Remove one named mailbox from the registry."""
    mailbox_id = mailbox_id.strip()
    root = store_root(path)
    with _REGISTRY_WRITE_LOCK:
        record = mailbox_store.get_entry(root, mailbox_store.MAILBOXES_REGISTRY, mailbox_id)
        if record is None:
            return {"id": mailbox_id, "deleted": False, "dry_run": dry_run}
        if dry_run:
            return {"id": mailbox_id, "deleted": False, "dry_run": True,
                    "would_delete": record.get("payload") or {}}
        mailbox_store.delete_entry(root, mailbox_store.MAILBOXES_REGISTRY, mailbox_id)
    return {"id": mailbox_id, "deleted": True}
