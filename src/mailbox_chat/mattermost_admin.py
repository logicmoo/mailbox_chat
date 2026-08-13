"""Familiar chat administration commands mapped onto Mattermost REST APIs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from .endpoint_address import parse_endpoint
from .admin_io import load_input, normalize_options, render
from .identifier_directory import IdentifierDirectory


COMMANDS = ("ping", "teams", "list", "names", "threads", "join", "part", "topic", "nick", "whois", "mode",
            "invite", "kick", "message", "notice", "raw")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mailbox-client mm",
        description="Run familiar channel and user commands through the relay's Mattermost bot",
    )
    result.add_argument("--url", default="http://127.0.0.1:46667",
                        help="relay HTTP base URL (default: http://127.0.0.1:46667)")
    result.add_argument("--token", help="relay REST Bearer token (or AGENT_MAILBOX_TOKEN)")
    result.add_argument("--input", help="read content from FILE")
    result.add_argument("--input-format", choices=("text", "json"), default="text",
                        help="interpret --input as text or JSON (default: text)")
    result.add_argument("--format", choices=("jsonl", "json", "text"), default="json",
                        help="output format (default: json)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("ping", help="verify the authenticated Mattermost connection")
    commands.add_parser("teams", help="list teams visible to the bot")
    listing = commands.add_parser("list", help="list channels visible to the bot")
    listing.add_argument("--team", help="limit results to one team ID")
    names = commands.add_parser("names", help="list users in a channel")
    names.add_argument("channel")
    threads = commands.add_parser("threads", help="list the bot's threads in a team")
    threads.add_argument("team", help="team ID or a discovered team name")
    join = commands.add_parser("join", help="add the bot to a channel")
    join.add_argument("channel")
    part = commands.add_parser("part", help="remove the bot from a channel")
    part.add_argument("channel")
    topic = commands.add_parser("topic", help="read or set a channel header")
    topic.add_argument("channel")
    topic.add_argument("text", nargs="?", help="new header; omit to read it")
    nick = commands.add_parser("nick", help="set the bot account nickname")
    nick.add_argument("nickname")
    whois = commands.add_parser("whois", help="show a user by ID or username")
    whois.add_argument("user")
    mode = commands.add_parser("mode", help="read or set channel visibility (public/private)")
    mode.add_argument("channel")
    mode.add_argument("visibility", nargs="?", choices=("public", "private"))
    invite = commands.add_parser("invite", help="add a user to a channel")
    invite.add_argument("user")
    invite.add_argument("channel")
    kick = commands.add_parser("kick", help="remove a user from a channel")
    kick.add_argument("channel")
    kick.add_argument("user")
    message = commands.add_parser("message", help="post a message to a channel or DM channel")
    message.add_argument("target")
    message.add_argument("text", nargs="?")
    notice = commands.add_parser("notice", help="post a message marked as a mailbox notice")
    notice.add_argument("target")
    notice.add_argument("text", nargs="?")
    raw = commands.add_parser("raw", help="make an expert-level authenticated /api/v4 request")
    raw.add_argument("method", choices=("GET", "POST", "PUT", "DELETE"), type=str.upper)
    raw.add_argument("path", help="Mattermost path beginning /api/v4/")
    raw.add_argument("json_body", nargs="?", help="optional JSON request body")
    return result


def _instance(base_url: str) -> str:
    return (urlsplit(base_url).hostname or "mattermost").lower()


def _id(value: str, *, directory: IdentifierDirectory | None = None,
        base_url: str = "", kind: str = "") -> str:
    endpoint = parse_endpoint(value)
    if endpoint:
        if endpoint.adapter != "mattermost":
            raise ValueError(f"expected an mm address, received {value}")
        value = endpoint.identifier
    if directory and not (len(value) == 26 and value.isalnum()):
        systems = [f"mm/{_instance(base_url)}", "mm/0"]
        matches = [entry for system in systems for entry in directory.find(
            system=system, text=value, kind=kind, limit=2,
        )]
        identifiers = list(dict.fromkeys(str(entry["identifier"]) for entry in matches))
        if len(identifiers) == 1:
            return identifiers[0]
        if len(identifiers) > 1:
            raise ValueError(f"ambiguous Mattermost name {value!r}; use mm/INSTANCE/ID")
    return value


def resolve_address(value: str, directory: IdentifierDirectory, *, base_url: str = "") -> str:
    endpoint = parse_endpoint(value)
    if endpoint is None or endpoint.adapter != "mattermost":
        return value
    identifier = _id(value, directory=directory, base_url=base_url, kind="channel")
    return f"mm/{endpoint.instance}/{identifier}"


def _remember(directory: IdentifierDirectory | None, base_url: str, record: dict[str, Any],
              *, kind: str) -> dict[str, Any]:
    identifier = str(record.get("id") or record.get("post", {}).get("id") or "")
    if not identifier:
        return record
    text = str(record.get("display_name") or record.get("username") or record.get("name") or
               record.get("post", {}).get("message") or identifier).strip()
    if len(text) > 512:
        text = text[:509] + "..."
    instance = _instance(base_url)
    metadata = {key: value for key, value in record.items() if key not in {"id"}}
    if directory:
        aliases = list(dict.fromkeys(str(record.get(field) or "").strip()[:512]
                                     for field in ("display_name", "name", "username")
                                     if str(record.get(field) or "").strip())) or [text]
        for alias in aliases:
            directory.remember(identifier, alias, system=f"mm/{instance}", kind=kind, metadata=metadata)
            directory.remember(identifier, alias, system="mm/0", kind=kind, metadata=metadata)
    return {**record, "address": f"mm/{instance}/{identifier}", "resource_type": kind}


def _remember_many(directory: IdentifierDirectory | None, base_url: str, value: Any,
                   *, kind: str) -> Any:
    return [_remember(directory, base_url, item, kind=kind) for item in value] if isinstance(value, list) else value


def _request(session: requests.Session, base_url: str, method: str, path: str,
             body: Any = None) -> Any:
    response = session.request(method, f"{base_url.rstrip('/')}{path}", json=body, timeout=30)
    response.raise_for_status()
    if not getattr(response, "content", b""):
        return {"ok": True}
    return response.json()


def _me(session: requests.Session, base_url: str) -> dict[str, Any]:
    return _request(session, base_url, "GET", "/api/v4/users/me")


def _user_id(session: requests.Session, base_url: str, value: str,
             directory: IdentifierDirectory | None = None) -> str:
    value = _id(value, directory=directory, base_url=base_url, kind="user")
    if len(value) == 26 and value.isalnum():
        return value
    return str(_request(session, base_url, "GET", f"/api/v4/users/username/{value}")["id"])


def execute(command: str, arguments: dict[str, Any], *, session: requests.Session,
            base_url: str, directory: IdentifierDirectory | None = None) -> Any:
    """Execute one Mattermost-flavoured command using an authenticated session."""
    if command not in COMMANDS:
        raise ValueError(f"unsupported Mattermost command: {command}")
    if command == "raw":
        path = str(arguments.get("path") or "")
        method = str(arguments.get("method") or "GET").upper()
        if not path.startswith("/api/v4/") or "://" in path or method not in {"GET", "POST", "PUT", "DELETE"}:
            raise ValueError("raw requests require GET/POST/PUT/DELETE and a local /api/v4/ path")
        return _request(session, base_url, method, path, arguments.get("body"))
    me = _me(session, base_url)
    me_id = str(me["id"])
    if command == "ping":
        return {"ok": True, "user": _remember(directory, base_url, me, kind="user")}
    if command == "teams":
        return _remember_many(directory, base_url, _request(
            session, base_url, "GET", f"/api/v4/users/{me_id}/teams",
        ), kind="team")
    if command == "list":
        team = str(arguments.get("team") or "")
        if team:
            team = _id(team, directory=directory, base_url=base_url, kind="team")
        path = (f"/api/v4/users/{me_id}/teams/{team}/channels" if team else
                f"/api/v4/users/{me_id}/channels")
        channels = _request(session, base_url, "GET", path)
        return [_remember(directory, base_url, item, kind=(
            "dm" if item.get("type") == "D" else "group" if item.get("type") == "G" else "channel"
        )) for item in channels]
    if command == "names":
        channel = _id(str(arguments["channel"]), directory=directory, base_url=base_url, kind="channel")
        return _remember_many(directory, base_url, _request(session, base_url, "GET",
            f"/api/v4/users?in_channel={quote(channel)}&per_page=200"), kind="user")
    if command == "threads":
        team = _id(str(arguments["team"]), directory=directory, base_url=base_url, kind="team")
        payload = _request(session, base_url, "GET",
                           f"/api/v4/users/{me_id}/teams/{quote(team)}/threads")
        if isinstance(payload, dict) and isinstance(payload.get("threads"), list):
            payload = {**payload, "threads": _remember_many(
                directory, base_url, payload["threads"], kind="thread")}
        return payload
    if command in {"join", "invite"}:
        user_id = (me_id if command == "join" else
                   _user_id(session, base_url, str(arguments["user"]), directory))
        channel = _id(str(arguments["channel"]), directory=directory, base_url=base_url, kind="channel")
        return _request(session, base_url, "POST", f"/api/v4/channels/{channel}/members",
                        {"user_id": user_id})
    if command in {"part", "kick"}:
        user_id = (me_id if command == "part" else
                   _user_id(session, base_url, str(arguments["user"]), directory))
        channel = _id(str(arguments["channel"]), directory=directory, base_url=base_url, kind="channel")
        return _request(session, base_url, "DELETE", f"/api/v4/channels/{channel}/members/{user_id}")
    if command == "topic":
        channel = _id(str(arguments["channel"]), directory=directory, base_url=base_url, kind="channel")
        if arguments.get("text") is None:
            return _request(session, base_url, "GET", f"/api/v4/channels/{channel}")
        return _request(session, base_url, "PUT", f"/api/v4/channels/{channel}/patch",
                        {"header": arguments["text"]})
    if command == "nick":
        return _request(session, base_url, "PUT", f"/api/v4/users/{me_id}/patch",
                        {"nickname": arguments["nickname"]})
    if command == "whois":
        value = _id(str(arguments["user"]), directory=directory, base_url=base_url, kind="user")
        path = (f"/api/v4/users/{value}" if len(value) == 26 and value.isalnum() else
                f"/api/v4/users/username/{value}")
        return _request(session, base_url, "GET", path)
    if command == "mode":
        channel = _id(str(arguments["channel"]), directory=directory, base_url=base_url, kind="channel")
        if not arguments.get("visibility"):
            return _request(session, base_url, "GET", f"/api/v4/channels/{channel}")
        return _request(session, base_url, "PUT", f"/api/v4/channels/{channel}/patch",
                        {"type": "O" if arguments["visibility"] == "public" else "P"})
    if command in {"message", "notice"}:
        body: dict[str, Any] = {"channel_id": _id(str(arguments["target"]), directory=directory,
                                                   base_url=base_url, kind="channel"),
                                "message": arguments["text"]}
        if command == "notice":
            body["props"] = {"mailbox_notice": True}
        return _request(session, base_url, "POST", "/api/v4/posts", body)
    raise AssertionError(command)


def _payload(args: argparse.Namespace) -> dict[str, Any]:
    values = vars(args).copy()
    values.pop("url", None)
    values.pop("token", None)
    values.pop("format", None)
    input_path = values.pop("input", None)
    input_format = values.pop("input_format", "text")
    command = values.pop("command")
    field = {"topic": "text", "message": "text", "notice": "text", "raw": "json_body"}.get(command)
    if input_path and not field:
        raise ValueError(f"--input is not valid with mm {command}")
    if field:
        loaded = load_input(input_path, values.get(field), input_format=input_format,
                            label=f"mm {command}")
        values[field] = loaded
        if command in {"message", "notice"} and loaded is None:
            raise ValueError(f"mm {command} requires inline text or --input FILE")
    if command == "raw" and values.get("json_body") is not None:
        raw_body = values.pop("json_body")
        if isinstance(raw_body, str):
            try:
                raw_body = json.loads(raw_body)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid raw JSON body: {error}") from error
        values["body"] = raw_body
    else:
        values.pop("json_body", None)
    return {"command": command, "arguments": values}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(normalize_options(list(sys.argv[1:] if argv is None else argv)))
    body = json.dumps(_payload(args)).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = args.token or os.environ.get("AGENT_MAILBOX_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(
        f"{args.url.rstrip('/')}/v1/mm/command", data=body, headers=headers, method="POST",
    ), timeout=35) as response:
        output = json.loads(response.read().decode("utf-8"))
    print(render(output.get("result", output), args.format))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
