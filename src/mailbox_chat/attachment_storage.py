"""Bounded storage for mailbox-managed attachments."""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path


MAX_FILE_ENV = "MAILBOX_RELAY_MAX_ATTACHMENT_BYTES"
MAX_STORAGE_ENV = "MAILBOX_RELAY_MAX_ATTACHMENT_STORAGE_BYTES"
DEFAULT_MAX_FILE_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_STORAGE_BYTES = 25 * 1024 * 1024 * 1024
_LOCK = threading.Lock()


def limits() -> tuple[int, int]:
    per_file = int(os.environ.get(MAX_FILE_ENV, DEFAULT_MAX_FILE_BYTES))
    total = int(os.environ.get(MAX_STORAGE_ENV, DEFAULT_MAX_STORAGE_BYTES))
    if per_file < 1 or total < 1:
        raise ValueError("attachment limits must be positive")
    return per_file, total


def storage_used(root: Path) -> int:
    attachment_root = root / "attachments"
    return sum(path.stat().st_size for path in attachment_root.rglob("*") if path.is_file())


def _check(root: Path, size: int, *, replacing: int = 0) -> None:
    per_file, total = limits()
    if size > per_file:
        raise ValueError(f"attachment is {size} bytes; maximum is {per_file} bytes")
    used = storage_used(root) - replacing
    if used + size > total:
        raise ValueError(
            f"attachment storage quota exceeded: {used} + {size} bytes is greater than {total} bytes"
        )


def copy_file(root: Path, source: Path, destination: Path) -> None:
    with _LOCK:
        _check(root, source.stat().st_size)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def write_bytes(root: Path, destination: Path, content: bytes) -> None:
    with _LOCK:
        replacing = destination.stat().st_size if destination.is_file() else 0
        _check(root, len(content), replacing=replacing)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
