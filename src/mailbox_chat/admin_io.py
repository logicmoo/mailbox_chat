"""Shared input and output handling for platform command families."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def normalize_options(argv: list[str]) -> list[str]:
    """Allow family-global value options on either side of the subcommand."""
    names = {"--url", "--token", "--input", "--input-format", "--format"}
    moved: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item.partition("=")[0] in names:
            if "=" in item:
                moved.append(item)
                index += 1
            elif index + 1 < len(argv):
                moved.extend((item, argv[index + 1]))
                index += 2
            else:
                remaining.append(item)
                index += 1
        else:
            remaining.append(item)
            index += 1
    return [*moved, *remaining]


def load_input(path: str | None, inline: Any, *, input_format: str, label: str) -> Any:
    if not path:
        return inline
    if inline is not None:
        raise ValueError(f"{label} accepts either inline content or --input, not both")
    try:
        content = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"cannot read --input: {error}") from error
    if input_format == "json":
        try:
            return json.loads(content)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON input: {error}") from error
    return content


def render(value: Any, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(value, ensure_ascii=False, indent=2)
    if output_format == "jsonl":
        values = value if isinstance(value, list) else [value]
        return "\n".join(json.dumps(item, ensure_ascii=False) for item in values)
    if isinstance(value, list):
        return "\n".join(str(item.get("display_name") or item.get("username") or
                             item.get("name") or item.get("id") or item)
                         if isinstance(item, dict) else str(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(f"{key}: {item}" for key, item in value.items())
    return str(value)
