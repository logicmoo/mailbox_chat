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
from .adapters.irc_adapter import IrcAdapter
from .listener_registry import listeners_for
from .admin_io import render


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


def discover_irc_users(channel: str, *, timeout: float = 15.0) -> list[dict[str, Any]]:
    adapter = IrcAdapter()
    if not adapter.configure():
        raise ValueError("no enabled IRC listener is configured")
    listener = adapter.listener or {}
    instance = endpoint_instance("irc", listener)
    if "/" in channel:
        from .endpoint_address import parse_endpoint
        address = parse_endpoint(channel)
        if address is None or address.adapter != "irc":
            raise ValueError("--channel must be an IRC address or channel name")
        if address.instance not in {"0", instance}:
            raise ValueError(f"IRC address instance does not match configured instance {instance}")
        channel = address.identifier
    if not channel.startswith(("#", "&", "+", "!")):
        channel = f"#{channel}"
    try:
        entries = adapter.list_channel_users(channel, timeout=timeout)
    finally:
        adapter.close()
    directory = IdentifierDirectory(agent_mailbox.mailbox_dir())
    return [{
        **directory.remember(entry["identifier"], entry["text"], system=f"irc/{instance}",
                             kind="user", metadata=entry["metadata"]),
        "address": f"irc/{instance}/{entry['identifier']}",
    } for entry in entries]


def _remote(base: str, resource: str, platform: str, timeout: float,
            channel: str = "") -> dict[str, Any]:
    query = urllib.parse.urlencode({"platform": platform, "timeout": timeout, "channel": channel})
    headers = {"Accept": "application/json"}
    token = os.environ.get(agent_mailbox.MAILBOX_TOKEN_ENV, "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(
        f"{base.rstrip('/')}/v1/discovery/{resource}?{query}", headers=headers,
    ), timeout=timeout + 5) as response:
        return json.loads(response.read().decode("utf-8"))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mailbox-client discover",
        description="Discover channels and other resources visible to configured platform accounts",
    )
    result.add_argument("--url", help="ask a running relay server to perform discovery")
    result.add_argument("--format", choices=("jsonl", "json", "text"), default="json",
                        help="output format (default: json)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("platforms", help="list platforms with implemented discovery")
    channels = commands.add_parser("channels", help="list visible platform channels")
    channels.add_argument("--platform", required=True, choices=("irc",), help="platform adapter")
    channels.add_argument("--timeout", type=float, default=30.0, help="discovery timeout in seconds")
    users = commands.add_parser("users", help="list users visible in one platform channel")
    users.add_argument("--platform", required=True, choices=("irc",), help="platform adapter")
    users.add_argument("--channel", required=True, help="channel address or platform channel ID")
    users.add_argument("--timeout", type=float, default=15.0, help="discovery timeout in seconds")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "platforms":
        result: Any = {"platforms": [{"id": "irc", "resources": ["channels", "users"]}]}
    else:
        if args.command == "channels":
            result = (_remote(args.url, "channels", args.platform, args.timeout) if args.url else
                      {"platform": args.platform, "channels": discover_irc_channels(timeout=args.timeout)})
        else:
            result = (_remote(args.url, "users", args.platform, args.timeout, args.channel) if args.url else
                      {"platform": args.platform, "channel": args.channel,
                       "users": discover_irc_users(args.channel, timeout=args.timeout)})
    print(render(result, args.format))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
