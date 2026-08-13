"""Platform-neutral top-level chat command parsing and dispatch."""

from __future__ import annotations

import argparse
import os
import sys

from .admin_io import load_input, normalize_options, render
from .agent_mailbox import CHAT_COMMANDS
from .endpoint_address import parse_endpoint
from .adapters.irc_adapter import post_relay_command as post_irc_command, protocol_line
from .adapters.mattermost_adapter import (
    arguments_from_namespace as mattermost_arguments,
    post_relay_command as post_mattermost_command,
)


def _registered_platform(identifier: str) -> str:
    """Infer a supported platform from an exact identifier saved by discovery."""
    if not identifier or "/" in identifier:
        return ""
    from .agent_mailbox import mailbox_dir
    from .identifier_directory import IdentifierDirectory

    matches = IdentifierDirectory(mailbox_dir()).find(identifier=identifier, limit=100)
    platforms = {
        "mm" if str(entry["system"]).split("/", 1)[0] in {"mm", "mattermost"}
        else "irc" if str(entry["system"]).split("/", 1)[0] == "irc"
        else ""
        for entry in matches
    } - {""}
    if len(platforms) > 1:
        raise ValueError(
            f"identifier {identifier!r} occurs on multiple platforms; select one with --on"
        )
    return next(iter(platforms), "")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mailbox-client",
        description=("Run chat commands on the platform inferred from an address or selected "
                     "with --on TYPE/INSTANCE (default: IRC)"),
    )
    result.add_argument("--url", default="http://127.0.0.1:46667",
                        help="relay HTTP base URL (default: http://127.0.0.1:46667)")
    result.add_argument("--token", help="REST Bearer token (or AGENT_MAILBOX_TOKEN)")
    result.add_argument("--on", metavar="TYPE/INSTANCE",
                        help="chat platform instance for commands without a qualified address")
    result.add_argument("--input", help="read command content from FILE")
    result.add_argument("--input-format", choices=("text", "json"), default="text",
                        help="interpret --input as text or JSON (default: text)")
    result.add_argument("--format", choices=("jsonl", "json", "text"), default="json",
                        help="output format (default: json)")
    commands = result.add_subparsers(dest="command", required=True)
    ping = commands.add_parser("ping", help="check connectivity to the selected platform")
    ping.add_argument("token", nargs="?", default="mailbox-client",
                      help="IRC PING token/server; ignored by Mattermost")
    commands.add_parser("teams", help="list visible teams (Mattermost)")
    listing = commands.add_parser("list", help="list visible channels and conversations")
    listing.add_argument("--team", help="limit Mattermost results to a team ID or name")
    names = commands.add_parser("names", help="list users in a channel")
    names.add_argument("channel", help="qualified channel address or IRC channel name")
    threads = commands.add_parser("threads", help="list followed threads (Mattermost)")
    threads.add_argument("team", help="Mattermost team ID or discovered name")
    join = commands.add_parser("join", help="join or create a channel")
    join.add_argument("channel", help="qualified channel address or IRC channel name")
    join.add_argument("key", nargs="?", help="optional channel key")
    part = commands.add_parser("part", help="leave a channel")
    part.add_argument("channel", help="qualified channel address or IRC channel name")
    part.add_argument("message", nargs="?", help="optional part message")
    topic = commands.add_parser("topic", help="read or set a channel topic")
    topic.add_argument("channel", help="qualified channel address or IRC channel name")
    topic.add_argument("text", nargs="?", help="new topic; omit to query")
    nick = commands.add_parser("nick", help="change the connected account nickname")
    nick.add_argument("nickname", help="new IRC nickname or Mattermost account nickname")
    whois = commands.add_parser("whois", help="show information about a user")
    whois.add_argument("nickname", help="qualified user address, user ID, username, or IRC nickname")
    mode = commands.add_parser("mode", help="inspect or change channel visibility, roles, or modes")
    mode.add_argument("target", help="qualified channel/user address or IRC target")
    mode.add_argument("modes", nargs="*",
                      help="IRC modes, or Mattermost public/private/+o USER/-o USER")
    invite = commands.add_parser("invite", help="add or invite a user to a channel")
    invite.add_argument("nickname", help="user ID, username, or IRC nickname")
    invite.add_argument("channel", help="qualified channel address or IRC channel name")
    kick = commands.add_parser("kick", help="remove a user from a channel")
    kick.add_argument("channel", help="qualified channel address or IRC channel name")
    kick.add_argument("nickname", help="user ID, username, or IRC nickname")
    kick.add_argument("reason", nargs="?", help="optional reason")
    message = commands.add_parser("message", help="send a message to a channel or user")
    message.add_argument("target", help="qualified channel/user address or IRC target")
    message.add_argument("text", nargs="?", help="message text; alternatively use --input FILE")
    notice = commands.add_parser("notice", help="send an informational or user-only notice")
    notice.add_argument("target", help="qualified channel/user address or IRC target")
    notice.add_argument("text", nargs="?", help="notice text; alternatively use --input FILE")
    notice.add_argument("--user", help="Mattermost user for an ephemeral notice")
    raw = commands.add_parser("raw", help="perform an advanced platform-specific operation")
    raw.add_argument("arguments", nargs="*", help="IRC line, or Mattermost METHOD /api/v4/PATH [JSON]")
    for command_parser in commands.choices.values():
        command_parser.add_argument(
            "--on", metavar="TYPE/INSTANCE", default=argparse.SUPPRESS,
            help="select a platform instance when no qualified address determines it",
        )
    return result


def _platform(args: argparse.Namespace) -> str:
    selected = str(args.on or "").split("/", 1)[0].lower()
    if selected:
        if selected not in {"irc", "mm", "mattermost"}:
            raise ValueError("--on currently supports irc/INSTANCE or mm/INSTANCE")
        return "mm" if selected in {"mm", "mattermost"} else "irc"
    for value in vars(args).values():
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            try:
                endpoint = parse_endpoint(candidate)
            except ValueError:
                continue
            if endpoint and endpoint.adapter == "mattermost":
                return "mm"
    # Discovery stores opaque IDs with their source system. A later command may
    # therefore use the bare ID without repeating TYPE/INSTANCE.
    for field in ("nickname", "target", "channel", "team"):
        candidate = getattr(args, field, None)
        if isinstance(candidate, str):
            registered = _registered_platform(candidate)
            if registered:
                return registered
    return "mm" if args.command in {"teams", "threads"} else "irc"


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(normalize_options(list(sys.argv[1:] if argv is None else argv)))
    platform = _platform(args)
    field = {"part": "message", "topic": "text", "message": "text",
             "notice": "text", "raw": "arguments"}.get(args.command)
    if args.input and not field:
        raise ValueError(f"--input is not valid with irc {args.command}")
    if field:
        inline = None if field == "arguments" else getattr(args, field)
        loaded = load_input(args.input, inline, input_format=args.input_format,
                            label=args.command)
        if field != "arguments":
            setattr(args, field, loaded)
        if args.command in {"message", "notice", "raw"} and loaded is None:
            if args.command != "raw" or not args.arguments:
                raise ValueError(f"{args.command} requires inline content or --input FILE")
    else:
        loaded = None
    token = args.token or os.environ.get("AGENT_MAILBOX_TOKEN", "")
    if platform == "mm":
        result = post_mattermost_command(args.url, token, args.command,
                                         mattermost_arguments(args, loaded))
        print(render(result.get("result", result), args.format))
        registry = result.get("registry") if isinstance(result, dict) else None
        if isinstance(registry, dict):
            count = int(registry.get("new_entries") or 0)
            print(f"[registry] {count} new entr{'y' if count == 1 else 'ies'} found",
                  file=sys.stderr)
        return 0
    if args.command in {"teams", "threads"}:
        raise ValueError(f"{args.command} is not available on IRC")
    if args.command in {"list", "names"}:
        from .discovery_admin import main as discover
        nested = ["--url", args.url, "--format", args.format,
                  "channels" if args.command == "list" else "users",
                  "--platform", "irc"]
        if args.command == "names":
            nested.extend(["--channel", args.channel])
        return discover(nested)
    line = protocol_line(args)
    result = post_irc_command(args.url, token, line)
    print(render(result, args.format))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
