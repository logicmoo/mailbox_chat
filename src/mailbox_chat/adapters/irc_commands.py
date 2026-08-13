"""IRC-specific command encoding and relay submission."""

from __future__ import annotations

import argparse
import json
import urllib.request


def protocol_line(args: argparse.Namespace) -> str:
    command = args.command
    if command == "ping":
        return f"PING :{args.token}"
    if command == "join":
        return f"JOIN {args.channel}" + (f" {args.key}" if args.key else "")
    if command == "part":
        return f"PART {args.channel}" + (f" :{args.message}" if args.message else "")
    if command == "topic":
        return f"TOPIC {args.channel}" + (f" :{args.text}" if args.text is not None else "")
    if command == "nick":
        return f"NICK {args.nickname}"
    if command == "whois":
        return f"WHOIS {args.nickname}"
    if command == "mode":
        return " ".join(["MODE", args.target, *args.modes])
    if command == "invite":
        return f"INVITE {args.nickname} {args.channel}"
    if command == "kick":
        return f"KICK {args.channel} {args.nickname}" + (f" :{args.reason}" if args.reason else "")
    if command == "message":
        return f"PRIVMSG {args.target} :{args.text}"
    if command == "notice":
        return f"NOTICE {args.target} :{args.text}"
    if command == "raw":
        return " ".join(args.arguments)
    raise ValueError(f"{command} is handled by IRC discovery")


def post_relay_command(url: str, token: str, line: str) -> dict:
    body = json.dumps({"line": line}).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(
        f"{url.rstrip('/')}/v1/irc/command", data=body, headers=headers, method="POST",
    ), timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))
