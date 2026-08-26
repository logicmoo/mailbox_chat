"""Discover addressable resources exposed by configured chat platforms."""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from typing import Any

import requests

from . import agent_mailbox
from .endpoint_address import endpoint_instance, parse_endpoint
from .identifier_directory import IdentifierDirectory
from .adapters.irc_adapter import IrcAdapter
from .connector_registry import connectors_for
from .admin_io import curl_command, render


def _irc_connector(instance: str = "") -> dict[str, Any]:
    """Select the enabled IRC connector for a server instance (or the first)."""
    connectors = connectors_for("irc")
    if not connectors:
        raise ValueError("no enabled IRC connector is configured")
    if instance:
        for connector in connectors:
            if endpoint_instance("irc", connector) == instance:
                return connector
        raise ValueError(f"no enabled IRC connector for instance {instance}")
    return connectors[0]


def _discord_connectors(instance: str = "") -> list[dict[str, Any]]:
    connectors = [item for item in connectors_for("discord") if item.get("enabled", True)]
    if not connectors:
        raise ValueError("no enabled Discord connector is configured")
    if not instance:
        return connectors
    filtered = [
        connector for connector in connectors
        if endpoint_instance("discord", connector) == instance
    ]
    if not filtered:
        raise ValueError(f"no enabled Discord connector for instance {instance}")
    return filtered


def _discord_headers(connector: dict[str, Any]) -> dict[str, str]:
    token_env = str(connector.get("token_env") or "DISCORD_BOT_TOKEN")
    token = os.environ.get(token_env, "").strip()
    if not token:
        raise ValueError(f"Discord connector {connector.get('id')} is missing {token_env}")
    return {"Authorization": f"Bot {token}"}


def discover_irc_mailboxes(*, instance: str = "", timeout: float = 30.0) -> list[dict[str, Any]]:
    connector = _irc_connector(instance)
    adapter = IrcAdapter()
    adapter.configure(connector)
    instance = endpoint_instance("irc", connector)
    try:
        entries = adapter.list_mailboxes(timeout=timeout)
    finally:
        adapter.close()
    directory = IdentifierDirectory(agent_mailbox.mailbox_dir())
    results = []
    for entry in entries:
        saved = directory.remember(
            entry["identifier"], entry["text"] or entry["identifier"],
            system=f"irc/{instance}", kind="mailbox", metadata=entry["metadata"],
        )
        results.append({**saved, "address": f"irc/{instance}/{entry['identifier']}"})
    return results


def discover_irc_users(mailbox: str, *, instance: str = "", timeout: float = 15.0) -> list[dict[str, Any]]:
    connector = _irc_connector(instance)
    adapter = IrcAdapter()
    adapter.configure(connector)
    instance = endpoint_instance("irc", connector)
    if "/" in mailbox:
        address = parse_endpoint(mailbox)
        if address is None or address.adapter != "irc":
            raise ValueError("--mailbox must be an IRC address or mailbox name")
        if address.instance not in {"0", instance}:
            raise ValueError(f"IRC address instance does not match configured instance {instance}")
        mailbox = address.identifier
    if not mailbox.startswith(("#", "&", "+", "!")):
        mailbox = f"#{mailbox}"
    try:
        entries = adapter.list_mailbox_users(mailbox, timeout=timeout)
    finally:
        adapter.close()
    directory = IdentifierDirectory(agent_mailbox.mailbox_dir())
    return [{
        **directory.remember(entry["identifier"], entry["text"], system=f"irc/{instance}",
                             kind="user", metadata=entry["metadata"]),
        "address": f"irc/{instance}/{entry['identifier']}",
    } for entry in entries]


def discover_discord_mailboxes(*, instance: str = "", timeout: float = 30.0) -> list[dict[str, Any]]:
    connectors = _discord_connectors(instance)
    directory = IdentifierDirectory(agent_mailbox.mailbox_dir())
    results: list[dict[str, Any]] = []
    for connector in connectors:
        connector_instance = endpoint_instance("discord", connector)
        for mailbox_id in connector.get("mailbox_ids", []):
            mailbox_text = str(mailbox_id).strip()
            if not mailbox_text:
                continue
            saved = directory.remember(
                mailbox_text,
                mailbox_text,
                system=f"discord/{connector_instance}",
                kind="mailbox",
                metadata={"connector_id": str(connector.get("id") or ""), "mailbox_id": mailbox_text},
            )
            results.append({**saved, "address": f"discord/{connector_instance}/{mailbox_text}"})
    return results


def discover_discord_users(mailbox: str, *, instance: str = "", timeout: float = 15.0) -> list[dict[str, Any]]:
    mailbox = str(mailbox or "").strip()
    if not mailbox:
        raise ValueError("mailbox is required")
    if "/" in mailbox:
        address = parse_endpoint(mailbox)
        if address is None or address.adapter != "discord":
            raise ValueError("--mailbox must be a Discord address or mailbox ID")
        if instance and address.instance not in {"0", instance}:
            raise ValueError(f"Discord address instance does not match configured instance {instance}")
        mailbox = address.identifier
        if not instance or address.instance != "0":
            instance = address.instance
    connectors = _discord_connectors(instance)
    connector = next(
        (item for item in connectors if mailbox in set(item.get("mailbox_ids") or [])),
        connectors[0],
    )
    connector_instance = endpoint_instance("discord", connector)
    response = requests.get(
        f"https://discord.com/api/v10/channels/{mailbox}/messages",
        headers=_discord_headers(connector),
        params={"limit": 100},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("Discord message listing response was not an array")
    users: dict[str, dict[str, Any]] = {}
    for message in payload:
        if not isinstance(message, dict):
            continue
        author = message.get("author") or {}
        if not isinstance(author, dict):
            continue
        identifier = str(author.get("id") or "").strip()
        if not identifier:
            continue
        text = str(author.get("username") or identifier).strip() or identifier
        users[identifier] = {
            "identifier": identifier,
            "text": text,
            "kind": "user",
            "metadata": {"mailbox": mailbox, "author": author},
        }
    directory = IdentifierDirectory(agent_mailbox.mailbox_dir())
    return [{
        **directory.remember(
            entry["identifier"],
            entry["text"],
            system=f"discord/{connector_instance}",
            kind="user",
            metadata=entry["metadata"],
        ),
        "address": f"discord/{connector_instance}/{entry['identifier']}",
    } for entry in users.values()]


def _remote(base: str, resource: str, platform: str, timeout: float,
            mailbox: str = "", instance: str = "") -> dict[str, Any]:
    query = urllib.parse.urlencode({"platform": platform, "timeout": timeout,
                                    "mailbox": mailbox, "instance": instance})
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
        description="Discover mailboxes and other resources visible to configured platform accounts",
    )
    result.add_argument("--url", help="ask a running relay server to perform discovery")
    result.add_argument("--format", choices=("jsonl", "json", "text"), default="json",
                        help="output format (default: json)")
    result.add_argument("--curl", action="store_true",
                        help="print the equivalent REST curl command without executing it")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("platforms", help="list platforms with implemented discovery")
    mailboxes = commands.add_parser("mailboxes", help="list visible platform mailboxes")
    mailboxes.add_argument("--platform", required=True, choices=("irc", "discord"), help="platform adapter")
    mailboxes.add_argument("--instance", default="", help="IRC server instance to query (default: first)")
    mailboxes.add_argument("--timeout", type=float, default=30.0, help="discovery timeout in seconds")
    users = commands.add_parser("users", help="list users visible in one platform mailbox")
    users.add_argument("--platform", required=True, choices=("irc", "discord"), help="platform adapter")
    users.add_argument("--mailbox", required=True, help="mailbox address or platform mailbox ID")
    users.add_argument("--instance", default="", help="IRC server instance to query (default: first)")
    users.add_argument("--timeout", type=float, default=15.0, help="discovery timeout in seconds")
    for command_parser in commands.choices.values():
        command_parser.add_argument(
            "--curl", action="store_true", default=argparse.SUPPRESS,
            help="print the equivalent REST curl command without executing it",
        )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if getattr(args, "curl", False):
        if not args.url:
            raise ValueError("--curl requires --url")
        if args.command == "platforms":
            raise ValueError("--curl has no REST equivalent for the platforms command")
        resource = "mailboxes" if args.command == "mailboxes" else "users"
        query = urllib.parse.urlencode({
            "platform": args.platform, "timeout": args.timeout,
            "mailbox": getattr(args, "mailbox", "") if args.command == "users" else "",
            "instance": args.instance,
        })
        token = os.environ.get(agent_mailbox.MAILBOX_TOKEN_ENV, "")
        print(curl_command("GET", f"{args.url.rstrip('/')}/v1/discovery/{resource}?{query}",
                           token=token))
        return 0
    if args.command == "platforms":
        result: Any = {
            "platforms": [
                {"id": "irc", "resources": ["mailboxes", "users"]},
                {"id": "discord", "resources": ["mailboxes", "users"]},
            ]
        }
    else:
        if args.command == "mailboxes":
            result = (_remote(args.url, "mailboxes", args.platform, args.timeout,
                              instance=args.instance) if args.url else
                      {"platform": args.platform, "mailboxes": (
                          discover_irc_mailboxes(instance=args.instance, timeout=args.timeout)
                          if args.platform == "irc"
                          else discover_discord_mailboxes(instance=args.instance, timeout=args.timeout)
                      )})
        else:
            result = (_remote(args.url, "users", args.platform, args.timeout, args.mailbox,
                              instance=args.instance) if args.url else
                      {"platform": args.platform, "mailbox": args.mailbox, "users": (
                          discover_irc_users(args.mailbox, instance=args.instance, timeout=args.timeout)
                          if args.platform == "irc"
                          else discover_discord_users(args.mailbox, instance=args.instance, timeout=args.timeout)
                      )})
    print(render(result, args.format))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
