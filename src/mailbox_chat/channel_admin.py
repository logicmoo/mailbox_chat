"""Create qualified conversations on supported platforms."""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from typing import Any

import requests
from dotenv import load_dotenv

from .endpoint_address import EndpointAddress, endpoint_instance, parse_endpoint
from .identifier_directory import IdentifierDirectory
from .listener_registry import config_dir, listeners_for
from .subscriptions import ensure_channel


def _listener(adapter: str, instance: str) -> dict[str, Any]:
    matches = [item for item in listeners_for(adapter, direction="outbound")
               if instance == "0" or endpoint_instance(adapter, item) == instance]
    if not matches:
        raise ValueError(f"no matching enabled {adapter} listener")
    return matches[0]


def _token(listener: dict[str, Any], default_env: str) -> str:
    value = os.environ.get(str(listener.get("token_env") or default_env), "").strip()
    if not value:
        raise ValueError(f"missing platform token: {listener.get('token_env') or default_env}")
    return value


def create_channel(address_text: str, *, title: str = "", topic: str = "",
                   private: bool = False, container: str = "",
                   session: requests.Session | None = None) -> dict[str, Any]:
    load_dotenv(config_dir() / ".env", override=False)
    address = parse_endpoint(address_text)
    if address is None:
        raise ValueError("channel address must use TYPE/INSTANCE/NAME")
    name = address.identifier.lstrip("#")
    client = session or requests.Session()
    if address.adapter == "local":
        canonical = address.canonical
        ensure_channel(canonical, metadata={"title": title or name, "topic": topic})
        return {**address.describe(resource_type="channel", properties={
                    "title": title or name, "topic": topic, "private": private,
                }), "identifier": address.identifier, "text": title or name,
                "kind": "channel", "created": True}
    listener = _listener(address.adapter, address.instance)
    instance = endpoint_instance(address.adapter, listener)
    if address.adapter == "mattermost":
        base = str(listener.get("base_url") or os.environ.get("MM_URL") or "").rstrip("/")
        team_id = container or str(listener.get("team_id") or "")
        if not base or not team_id:
            raise ValueError("Mattermost creation requires listener base_url and --container TEAM_ID")
        response = client.post(f"{base}/api/v4/channels", headers={
            "Authorization": f"Bearer {_token(listener, 'MM_BOT_TOKEN')}",
        }, json={"team_id": team_id, "name": name, "display_name": title or name,
                 "type": "P" if private else "O", "purpose": topic}, timeout=30)
        response.raise_for_status()
        record = response.json()
    elif address.adapter == "discord":
        guild = container or str(listener.get("guild_id") or "")
        if not guild:
            raise ValueError("Discord creation requires --container GUILD_ID")
        response = client.post(f"https://discord.com/api/v10/guilds/{guild}/channels",
                               headers={"Authorization": f"Bot {_token(listener, 'DISCORD_BOT_TOKEN')}"},
                               json={"name": name, "type": 0, "topic": topic}, timeout=30)
        response.raise_for_status()
        record = response.json()
    elif address.adapter == "slack":
        response = client.post("https://slack.com/api/conversations.create",
                               headers={"Authorization": f"Bearer {_token(listener, 'SLACK_BOT_TOKEN')}"},
                               json={"name": name, "is_private": private,
                                     **({"team_id": container} if container else {})}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise ValueError(f"Slack channel creation failed: {payload.get('error', 'unknown error')}")
        record = payload["channel"]
    elif address.adapter == "matrix":
        homeserver = str(listener.get("homeserver") or "").rstrip("/")
        response = client.post(f"{homeserver}/_matrix/client/v3/createRoom",
                               headers={"Authorization": f"Bearer {_token(listener, 'MATRIX_ACCESS_TOKEN')}"},
                               json={"room_alias_name": name, "name": title or name,
                                     "topic": topic, "visibility": "private" if private else "public"}, timeout=30)
        response.raise_for_status()
        record = {"id": response.json()["room_id"], "name": title or name}
    elif address.adapter == "irc":
        from .adapters.irc_adapter import IrcAdapter
        adapter = IrcAdapter()
        adapter.listener = listener
        adapter.status["enabled"] = True
        try:
            adapter.connect()
            adapter._write(f"JOIN #{name}")
        finally:
            adapter.close()
        record = {"id": f"#{name}", "name": title or f"#{name}"}
    else:
        raise ValueError(f"{address.adapter} does not support channel creation through this API")
    identifier = str(record.get("id") or record.get("channel_id") or "")
    text = str(record.get("display_name") or record.get("name") or title or name)
    canonical = EndpointAddress(address.adapter, instance, identifier).canonical
    saved = IdentifierDirectory(__import__("mailbox_channels.agent_mailbox", fromlist=["mailbox_dir"]).mailbox_dir()).remember(
        identifier, text, system=f"{address.adapter}/{instance}", kind="channel",
        metadata={"topic": topic, "private": private, "created_by": "mailbox-client channels"},
    )
    return {**saved, **EndpointAddress(address.adapter, instance, identifier).describe(
        resource_type="channel", properties={"topic": topic, "private": private}),
        "address": canonical, "created": True}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="mailbox-client channels",
                                     description="Create conversations on supported platforms")
    result.add_argument("--url", help="ask a running relay server to create the channel")
    result.add_argument("--token", help="REST Bearer token (or AGENT_MAILBOX_TOKEN)")
    commands = result.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create a qualified channel or room")
    create.add_argument("address", help="TYPE/INSTANCE/NAME address")
    create.add_argument("--title", default="", help="display name")
    create.add_argument("--topic", default="", help="channel topic or purpose")
    create.add_argument("--private", action="store_true", help="request a private channel")
    create.add_argument("--container", default="", help="team or guild ID when required")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.url:
        payload = json.dumps({"address": args.address, "title": args.title, "topic": args.topic,
                              "private": args.private, "container": args.container}).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        token = args.token or os.environ.get("AGENT_MAILBOX_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with urllib.request.urlopen(urllib.request.Request(
            f"{args.url.rstrip('/')}/v1/channels", data=payload, headers=headers, method="POST",
        ), timeout=35) as response:
            result = json.loads(response.read().decode("utf-8"))["channel"]
    else:
        result = create_channel(args.address, title=args.title, topic=args.topic,
                                private=args.private, container=args.container)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
