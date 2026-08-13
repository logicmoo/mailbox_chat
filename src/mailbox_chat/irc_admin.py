"""Standard IRC commands through the running mailbox relay connection."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

from .admin_io import load_input, normalize_options, render


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mailbox-client irc",
        description="Run standard IRC commands through the relay's active IRC connection",
    )
    result.add_argument("--url", default="http://127.0.0.1:46667",
                        help="relay HTTP base URL (default: http://127.0.0.1:46667)")
    result.add_argument("--token", help="REST Bearer token (or AGENT_MAILBOX_TOKEN)")
    result.add_argument("--input", help="read command content from FILE")
    result.add_argument("--input-format", choices=("text", "json"), default="text",
                        help="interpret --input as text or JSON (default: text)")
    result.add_argument("--format", choices=("jsonl", "json", "text"), default="json",
                        help="output format (default: json)")
    commands = result.add_subparsers(dest="command", required=True)
    ping = commands.add_parser("ping", help="send IRC PING over the active connection")
    ping.add_argument("token", nargs="?", default="mailbox-client", help="PING token or server")
    commands.add_parser("list", help="list visible public channels (IRC LIST)")
    names = commands.add_parser("names", help="list visible users in a channel (IRC NAMES)")
    names.add_argument("channel")
    join = commands.add_parser("join", help="join or create a channel")
    join.add_argument("channel")
    join.add_argument("key", nargs="?", help="optional channel key")
    part = commands.add_parser("part", help="leave a channel")
    part.add_argument("channel")
    part.add_argument("message", nargs="?", help="optional part message")
    topic = commands.add_parser("topic", help="read or set a channel topic")
    topic.add_argument("channel")
    topic.add_argument("text", nargs="?", help="new topic; omit to query")
    nick = commands.add_parser("nick", help="change the relay IRC nickname")
    nick.add_argument("nickname")
    whois = commands.add_parser("whois", help="request information about a nickname")
    whois.add_argument("nickname")
    mode = commands.add_parser("mode", help="read or change channel/user modes")
    mode.add_argument("target")
    mode.add_argument("modes", nargs="*")
    invite = commands.add_parser("invite", help="invite a nickname to a channel")
    invite.add_argument("nickname")
    invite.add_argument("channel")
    kick = commands.add_parser("kick", help="remove a nickname from a channel")
    kick.add_argument("channel")
    kick.add_argument("nickname")
    kick.add_argument("reason", nargs="?", help="optional reason")
    message = commands.add_parser("message", help="send PRIVMSG to a channel or nickname")
    message.add_argument("target")
    message.add_argument("text", nargs="?")
    notice = commands.add_parser("notice", help="send NOTICE to a channel or nickname")
    notice.add_argument("target")
    notice.add_argument("text", nargs="?")
    raw = commands.add_parser("raw", help="send one expert-level raw IRC protocol line")
    raw.add_argument("line", nargs="?")
    return result


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
        return args.line
    raise ValueError(f"{command} is handled by IRC discovery")


def _post(url: str, token: str, line: str) -> dict:
    body = json.dumps({"line": line}).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(
        f"{url.rstrip('/')}/v1/irc/command", data=body, headers=headers, method="POST",
    ), timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(normalize_options(list(sys.argv[1:] if argv is None else argv)))
    field = {"part": "message", "topic": "text", "message": "text",
             "notice": "text", "raw": "line"}.get(args.command)
    if args.input and not field:
        raise ValueError(f"--input is not valid with irc {args.command}")
    if field:
        loaded = load_input(args.input, getattr(args, field), input_format=args.input_format,
                            label=f"irc {args.command}")
        if isinstance(loaded, (dict, list)):
            loaded = json.dumps(loaded, ensure_ascii=False)
        setattr(args, field, loaded)
        if args.command in {"message", "notice", "raw"} and loaded is None:
            raise ValueError(f"irc {args.command} requires inline text or --input FILE")
    if args.command in {"list", "names"}:
        from .discovery_admin import main as discover
        nested = ["--url", args.url, "--format", args.format,
                  "channels" if args.command == "list" else "users",
                  "--platform", "irc"]
        if args.command == "names":
            nested.extend(["--channel", args.channel])
        return discover(nested)
    line = protocol_line(args)
    result = _post(args.url, args.token or os.environ.get("AGENT_MAILBOX_TOKEN", ""), line)
    print(render(result, args.format))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
