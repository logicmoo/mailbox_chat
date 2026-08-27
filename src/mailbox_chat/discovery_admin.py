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


def _discover_discord_chats_via_api(
    connector: dict[str, Any],
    *,
    timeout: float,
) -> list[dict[str, Any]]:
    headers = _discord_headers(connector)
    response = requests.get(
        "https://discord.com/api/v10/users/@me/guilds",
        headers=headers,
        params={"limit": 200},
        timeout=timeout,
    )
    response.raise_for_status()
    guilds = response.json()
    if not isinstance(guilds, list):
        raise ValueError("Discord guild listing response was not an array")
    discovered: list[dict[str, Any]] = []
    for guild in guilds:
        if not isinstance(guild, dict):
            continue
        guild_id = str(guild.get("id") or "").strip()
        guild_name = str(guild.get("name") or guild_id).strip() or guild_id
        if not guild_id:
            continue
        channels_response = requests.get(
            f"https://discord.com/api/v10/guilds/{guild_id}/channels",
            headers=headers,
            timeout=timeout,
        )
        channels_response.raise_for_status()
        channels = channels_response.json()
        if not isinstance(channels, list):
            continue
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            channel_id = str(channel.get("id") or "").strip()
            channel_name = str(channel.get("name") or channel_id).strip() or channel_id
            channel_type = channel.get("type")
            if not channel_id:
                continue
            if isinstance(channel_type, (int, float)) and int(channel_type) not in {0, 5}:
                continue
            discovered.append({
                "mailbox_id": channel_id,
                "text": f"{guild_name}#{channel_name}",
                "metadata": {
                    "guild_id": guild_id,
                    "guild_name": guild_name,
                    "channel_name": channel_name,
                    "channel_type": channel_type,
                },
            })
    return discovered


def _remember_discord_users_from_messages(
    payload: list[Any],
    *,
    mailbox: str,
    connector_instance: str,
    directory: IdentifierDirectory,
) -> dict[str, dict[str, Any]]:
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
        saved = directory.remember(
            identifier,
            text,
            system=f"discord/{connector_instance}",
            kind="user",
            metadata={
                "mailbox": mailbox,
                "mailbox_id": mailbox,
                "author": author,
            },
        )
        users[identifier] = {
            **saved,
            "address": f"discord/{connector_instance}/{identifier}",
        }
    return users


def _load_cached_discord_users(
    *,
    mailbox: str,
    connector_instance: str,
    connector_mailboxes: set[str],
    directory: IdentifierDirectory,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for entry in directory.find(system=f"discord/{connector_instance}", kind="user", limit=1000):
        metadata = entry.get("metadata")
        if not isinstance(metadata, dict):
            continue
        mailbox_id = str(metadata.get("mailbox_id") or metadata.get("mailbox") or "").strip()
        if mailbox_id and mailbox_id != mailbox:
            continue
        if not mailbox_id and (len(connector_mailboxes) != 1 or mailbox not in connector_mailboxes):
            continue
        identifier = str(entry.get("identifier") or "").strip()
        if not identifier:
            continue
        results[identifier] = {
            **entry,
            "address": f"discord/{connector_instance}/{identifier}",
        }
    return results


def discover_irc_chats(*, instance: str = "", timeout: float = 30.0) -> list[dict[str, Any]]:
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


def discover_discord_chats(*, instance: str = "", timeout: float = 30.0) -> list[dict[str, Any]]:
    connectors = _discord_connectors(instance)
    directory = IdentifierDirectory(agent_mailbox.mailbox_dir())
    results_by_id: dict[str, dict[str, Any]] = {}
    for connector in connectors:
        connector_instance = endpoint_instance("discord", connector)
        configured_mailboxes = [
            {
                "mailbox_id": str(mailbox_id).strip(),
                "text": str(mailbox_id).strip(),
                "metadata": {},
            }
            for mailbox_id in connector.get("mailbox_ids", [])
            if str(mailbox_id).strip()
        ]
        discovered_chats = []
        if str(connector.get("auto_discover_channels", True)).strip().lower() not in {"0", "false", "no"}:
            try:
                discovered_chats = _discover_discord_chats_via_api(connector, timeout=timeout)
            except requests.RequestException:
                discovered_chats = []
        for mailbox in [*configured_mailboxes, *discovered_chats]:
            mailbox_text = str(mailbox.get("mailbox_id") or "").strip()
            if not mailbox_text:
                continue
            saved = directory.remember(
                mailbox_text,
                str(mailbox.get("text") or mailbox_text).strip() or mailbox_text,
                system=f"discord/{connector_instance}",
                kind="mailbox",
                metadata={
                    "connector_id": str(connector.get("id") or ""),
                    "mailbox_id": mailbox_text,
                    **(mailbox.get("metadata") if isinstance(mailbox.get("metadata"), dict) else {}),
                },
            )
            results_by_id[mailbox_text] = {**saved, "address": f"discord/{connector_instance}/{mailbox_text}"}
    return sorted(
        results_by_id.values(),
        key=lambda entry: (str(entry.get("text") or "").lower(), str(entry.get("identifier") or "")),
    )


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
    connector_mailboxes = {
        str(mailbox_id).strip() for mailbox_id in (connector.get("mailbox_ids") or [])
        if str(mailbox_id).strip()
    }
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
    directory = IdentifierDirectory(agent_mailbox.mailbox_dir())
    remembered = _remember_discord_users_from_messages(
        payload,
        mailbox=mailbox,
        connector_instance=connector_instance,
        directory=directory,
    )
    cached = _load_cached_discord_users(
        mailbox=mailbox,
        connector_instance=connector_instance,
        connector_mailboxes=connector_mailboxes,
        directory=directory,
    )
    users = {**cached, **remembered}
    return sorted(
        users.values(),
        key=lambda entry: (
            str(entry.get("text") or "").lower(),
            str(entry.get("identifier") or ""),
        ),
    )


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
        description="Discover chats and other resources visible to configured platform accounts",
    )
    result.add_argument("--url", help="ask a running relay server to perform discovery")
    result.add_argument("--format", choices=("jsonl", "json", "text"), default="json",
                        help="output format (default: json)")
    result.add_argument("--curl", action="store_true",
                        help="print the equivalent REST curl command without executing it")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("platforms", help="list platforms with implemented discovery")
    chats = commands.add_parser("chats", help="list visible platform chats")
    chats.add_argument("--platform", required=True, choices=("irc", "discord"), help="platform adapter")
    chats.add_argument("--instance", default="", help="IRC server instance to query (default: first)")
    chats.add_argument("--timeout", type=float, default=30.0, help="discovery timeout in seconds")
    users = commands.add_parser("users", help="list users visible in one platform mailbox")
    users.add_argument("--platform", required=True, choices=("irc", "discord"), help="platform adapter")
    users.add_argument("--mailbox", required=True, help="mailbox address or platform mailbox ID")
    users.add_argument("--instance", default="", help="IRC server instance to query (default: first)")
    users.add_argument("--timeout", type=float, default=15.0, help="discovery timeout in seconds")
    seen_parsers: set[int] = set()
    for command_parser in commands.choices.values():
        marker = id(command_parser)
        if marker in seen_parsers:
            continue
        seen_parsers.add(marker)
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
        resource = "chats" if args.command == "chats" else "users"
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
                {"id": "irc", "resources": ["chats", "users"]},
                {"id": "discord", "resources": ["chats", "users"]},
            ]
        }
    else:
        if args.command == "chats":
            discovered = (
                _remote(args.url, "chats", args.platform, args.timeout, instance=args.instance)
                if args.url
                else (
                    discover_irc_chats(instance=args.instance, timeout=args.timeout)
                    if args.platform == "irc"
                    else discover_discord_chats(instance=args.instance, timeout=args.timeout)
                )
            )
            chats = discovered.get("chats", []) if isinstance(discovered, dict) else discovered
            result = (
                {**discovered, "chats": chats}
                if isinstance(discovered, dict)
                else {"platform": args.platform, "chats": chats}
            )
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
