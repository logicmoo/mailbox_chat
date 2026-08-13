"""Discover addressable resources exposed by configured chat platforms."""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from typing import Any

from . import agent_mailbox
from .endpoint_address import endpoint_instance
from .identifier_directory import IdentifierDirectory
from .irc_adapter import IrcAdapter
from .listener_registry import listeners_for


def discover_irc_channels(*, timeout: float = 30.0) -> list[dict[str, Any]]:
    adapter = IrcAdapter()
    if not adapter.configure():
        raise ValueError("no enabled IRC listener is configured")
    listener = adapter.listener or {}
    instance = endpoint_instance("irc", listener)
    try:
        entries = adapter.list_channels(timeout=timeout)
    finally:
        adapter.close()
    directory = IdentifierDirectory(agent_mailbox.mailbox_dir())
    results = []
    for entry in entries:
        saved = directory.remember(
            entry["identifier"], entry["text"] or entry["identifier"],
            system=f"irc/{instance}", kind="channel", metadata=entry["metadata"],
        )
        results.append({**saved, "address": f"irc/{instance}/{entry['identifier']}"})
    return results


def _remote(base: str, platform: str, timeout: float) -> dict[str, Any]:
    query = urllib.parse.urlencode({"platform": platform, "timeout": timeout})
    headers = {"Accept": "application/json"}
    token = os.environ.get(agent_mailbox.MAILBOX_TOKEN_ENV, "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(
        f"{base.rstrip('/')}/v1/discovery/channels?{query}", headers=headers,
    ), timeout=timeout + 5) as response:
        return json.loads(response.read().decode("utf-8"))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mailbox-client discover",
        description="Discover channels and other resources visible to configured platform accounts",
    )
    result.add_argument("--url", help="ask a running relay server to perform discovery")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("platforms", help="list platforms with implemented discovery")
    channels = commands.add_parser("channels", help="list visible platform channels")
    channels.add_argument("--platform", required=True, choices=("irc",), help="platform adapter")
    channels.add_argument("--timeout", type=float, default=30.0, help="discovery timeout in seconds")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "platforms":
        result: Any = {"platforms": [{"id": "irc", "resources": ["channels"]}]}
    else:
        result = (_remote(args.url, args.platform, args.timeout) if args.url else
                  {"platform": args.platform, "channels": discover_irc_channels(timeout=args.timeout)})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
