"""Mattermost transport adapter for Mailbox Channel Relay Bridging Proxy."""

from __future__ import annotations

import hashlib
import argparse
import json
import logging
import mimetypes
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from ..attachment_storage import write_bytes
from ..connector_registry import config_dir, connectors_for
from ..delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from ..subscriptions import ensure_mailbox, subscribers
from ..endpoint_address import EndpointAddress, mailbox_resource_id, endpoint_instance, parse_endpoint
from ..admin_io import load_input, normalize_options, render
from ..identifier_directory import IdentifierDirectory
from ..agent_mailbox import MATTERMOST_COMMANDS
from urllib.parse import quote, unquote, urlencode, urlsplit


LOGGER = logging.getLogger(__name__)
RELAY_RECIPIENT = "server_outbound_relay_agent_to_mailbox"
DEFAULT_INBOUND_RECIPIENTS: tuple[str, ...] = ()
RELAY_PORT = 46667


def _mailbox_module():
    from .. import agent_mailbox

    return agent_mailbox


class MattermostRelay:
    def __init__(self, *, session: requests.Session | None = None, sleep: Any = time.sleep) -> None:
        self.session = session or requests.Session()
        self.sleep = sleep
        self.stop_requested = False
        self.status: dict[str, Any] = {
            "enabled": False,
            "running": False,
            "connected": False,
            "lastError": None,
            "lastCycleAt": None,
            "mailboxes": [],
        }
        self._latest_create_at: dict[str, int] = {}
        self._bot_user_id = ""
        self._next_dm_refresh = 0.0
        self.connector: dict[str, Any] = {}
        self.base_url = ""
        self.token = ""
        self.default_mailbox = ""
        self._mailbox_metadata: dict[str, dict[str, str]] = {}

    def configure(self) -> bool:
        load_dotenv(config_dir() / ".env", override=False)
        configured = connectors_for("mattermost")
        self.connector = configured[0] if configured else {}
        self.base_url = str(self.connector.get("base_url") or os.environ.get("MM_URL") or "").rstrip("/")
        token_env = str(self.connector.get("token_env") or "MM_BOT_TOKEN")
        self.token = os.environ.get(token_env, "").strip()
        configured_mailboxes = list(self.connector.get("mailbox_ids") or [])
        legacy_mailboxes = [item.strip() for item in os.environ.get(
            "MM_MAILBOX_IDS", os.environ.get("MM_MAILBOX_ID", ""),
        ).split(",") if item.strip()]
        mailboxes = configured_mailboxes or legacy_mailboxes
        self.default_mailbox = mailboxes[0] if mailboxes else ""
        missing = [name for name, value in (
            ("connectors[].base_url (or MM_URL)", self.base_url),
            (f"environment variable {token_env}", self.token),
            ("connectors[].mailbox_ids (or MM_MAILBOX_ID)", self.default_mailbox),
        ) if not value]
        enabled = os.environ.get("MATTERMOST_RELAY_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
        self.status["enabled"] = enabled and not missing
        if enabled and missing:
            self.status["lastError"] = f"Missing Mattermost settings: {', '.join(missing)}"
        return bool(self.status["enabled"])

    def stop(self) -> None:
        self.stop_requested = True
        self.status["running"] = False
        self.status["connected"] = False

    def _mailboxes(self) -> list[str]:
        configured_connectors = connectors_for("mattermost", direction="inbound")
        configured_mailboxes = [
            mailbox_id for connector in configured_connectors for mailbox_id in connector["mailbox_ids"]
        ]
        if configured_mailboxes:
            return list(dict.fromkeys(configured_mailboxes))
        return [self.default_mailbox] if self.default_mailbox else []

    def _inbound_recipients(self, mailbox_id: str) -> list[str]:
        configured = [
            recipient
            for connector in connectors_for("mattermost", direction="inbound")
            if mailbox_id in connector["mailbox_ids"] or (
                connector.get("include_direct_messages") and mailbox_id not in self._mailboxes()
            )
            for recipient in [connector.get("bridge_agent"), *connector.get("mailbox_recipients", [])]
            if recipient
        ]
        if not configured:
            fallback = os.environ.get("MATTERMOST_RELAY_RECIPIENTS", ",".join(DEFAULT_INBOUND_RECIPIENTS))
            configured = [item.strip() for item in fallback.split(",") if item.strip()]
        instance = endpoint_instance("mattermost", self.connector) if self.connector else (
            urlsplit(self.base_url).hostname or "mattermost").lower()
        address = EndpointAddress("mattermost", instance, mailbox_id).canonical
        identity = self._mattermost_mailbox_identity(mailbox_id)
        resource_id = mailbox_resource_id(
            "mattermost", instance, mailbox_id,
        )
        ensure_mailbox(resource_id, metadata={
            "connector": self.connector.get("id", ""),
            "endpoint": instance,
            "external_address": address,
            **identity,
        })
        return list(dict.fromkeys([
            *configured,
            resource_id,
            *subscribers(resource_id),
        ]))

    def _mattermost_mailbox_identity(self, mailbox_id: str) -> dict[str, str]:
        """Resolve a mailbox's readable team/workspace identity once per process."""
        if mailbox_id in self._mailbox_metadata:
            return self._mailbox_metadata[mailbox_id]
        identity: dict[str, str] = {}
        try:
            response = self.session.get(f"{self.base_url}/api/v4/mailboxes/{mailbox_id}", timeout=15)
            response.raise_for_status()
            mailbox = response.json()
            if isinstance(mailbox, dict) and mailbox.get("id") == mailbox_id:
                mailbox_name = str(mailbox.get("name") or mailbox.get("display_name") or "").strip()
                team_id = str(mailbox.get("team_id") or "").strip()
                if mailbox_name:
                    identity["mailbox_name"] = mailbox_name
                if team_id:
                    identity["workspace_id"] = team_id
                    team_response = self.session.get(
                        f"{self.base_url}/api/v4/teams/{team_id}", timeout=15,
                    )
                    team_response.raise_for_status()
                    team = team_response.json()
                    if isinstance(team, dict):
                        workspace_name = str(
                            team.get("name") or team.get("display_name") or ""
                        ).strip()
                        if workspace_name:
                            identity["workspace_name"] = workspace_name
                directory = IdentifierDirectory(_mailbox_module().mailbox_dir())
                system = f"mm/{endpoint_instance('mattermost', self.connector)}"
                if mailbox_name:
                    directory.remember(
                        mailbox_id, mailbox_name, system=system, kind="mailbox", metadata=mailbox,
                    )
                if team_id and identity.get("workspace_name"):
                    directory.remember(
                        team_id, identity["workspace_name"], system=system,
                        kind="team", metadata=team,
                    )
        except (requests.RequestException, KeyError, TypeError, ValueError):
            LOGGER.debug("Could not resolve Mattermost mailbox identity for %s", mailbox_id,
                         exc_info=True)
        self._mailbox_metadata[mailbox_id] = identity
        return identity

    def _post_recipients(self, mailbox_id: str, author_id: str) -> list[str]:
        instance = endpoint_instance("mattermost", self.connector) if self.connector else (
            urlsplit(self.base_url).hostname or "mattermost").lower()
        direct_subscribers = (list(dict.fromkeys([
            *subscribers(author_id),
        ])) if mailbox_id not in self._mailboxes() else [])
        return list(dict.fromkeys([*self._inbound_recipients(mailbox_id), *direct_subscribers]))

    def _connect(self) -> None:
        base_url = self.base_url
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})
        response = self.session.get(f"{base_url}/api/v4/users/me", timeout=15)
        response.raise_for_status()
        self._bot_user_id = str(response.json()["id"])
        mailboxes = self._mailboxes()
        now_ms = int(time.time() * 1000)
        self._latest_create_at = {mailbox_id: now_ms for mailbox_id in mailboxes}
        self.status.update({"connected": True, "mailboxes": mailboxes, "lastError": None})

    def _refresh_direct_mailboxes(self, base_url: str) -> None:
        if time.time() < self._next_dm_refresh:
            return
        response = self.session.get(f"{base_url}/api/v4/users/{self._bot_user_id}/mailboxes", timeout=15)
        response.raise_for_status()
        direct = [str(item["id"]) for item in response.json() if item.get("type") == "D" and item.get("id")]
        now_ms = int(time.time() * 1000)
        mailboxes = list(dict.fromkeys([*self._mailboxes(), *direct]))
        for mailbox_id in mailboxes:
            self._latest_create_at.setdefault(mailbox_id, now_ms)
        self.status["mailboxes"] = mailboxes
        self._next_dm_refresh = time.time() + 30

    def _download_attachments(self, base_url: str, post: dict[str, Any]) -> list[dict[str, Any]]:
        mailbox = _mailbox_module()
        attachments: list[dict[str, Any]] = []
        for file_id in post.get("file_ids") or []:
            info_response = self.session.get(f"{base_url}/api/v4/files/{file_id}/info", timeout=15)
            info_response.raise_for_status()
            info = info_response.json()
            name = Path(str(info.get("name") or file_id)).name
            target_dir = mailbox.mailbox_dir() / "attachments" / str(post["id"])
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / name
            if not target.exists():
                file_response = self.session.get(f"{base_url}/api/v4/files/{file_id}", timeout=30)
                file_response.raise_for_status()
                write_bytes(mailbox.mailbox_dir(), target, file_response.content)
            content = target.read_bytes()
            attachments.append({
                "path": str(target),
                "name": name,
                "mime_type": str(info.get("mime_type") or mimetypes.guess_type(name)[0] or "application/octet-stream"),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "mattermost_file_id": str(file_id),
            })
        return attachments

    def _poll_inbound(self, base_url: str) -> None:
        mailbox = _mailbox_module()
        for mailbox_id in self.status["mailboxes"]:
            response = self.session.get(
                f"{base_url}/api/v4/mailboxes/{mailbox_id}/posts",
                params={"page": 0, "per_page": 200, "since": self._latest_create_at.get(mailbox_id, 0)},
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
            posts = payload.get("posts", {})
            for post_id in reversed(payload.get("order", [])):
                post = posts.get(post_id) or {}
                created = int(post.get("create_at", 0))
                self._latest_create_at[mailbox_id] = max(self._latest_create_at.get(mailbox_id, 0), created + 1)
                if str(post.get("user_id", "")) == self._bot_user_id:
                    continue
                source_id = str(post.get("id", ""))
                if self._source_seen(mailbox.mailbox_dir(), source_id):
                    continue
                attachments = self._download_attachments(base_url, post)
                matching = next((
                    item for item in connectors_for("mattermost", direction="inbound")
                    if mailbox_id in item["mailbox_ids"]
                ), {})
                mailbox_identity = self._mattermost_mailbox_identity(mailbox_id)
                origin_fields = with_origin(
                    {
                        "author": str(post.get("user_id", "")),
                        "attachments": attachments,
                        **mailbox_identity,
                        **({"team_id": mailbox_identity["workspace_id"]}
                           if mailbox_identity.get("workspace_id") else {}),
                    },
                    adapter="mattermost",
                    connector_id=str(matching.get("id") or "mattermost-default"),
                    source_id=source_id,
                    mailbox_id=mailbox_id,
                    presence_id=str(matching.get("presence_id") or ""),
                )
                DeliveryLedger(mailbox.mailbox_dir()).claim(
                    origin_fields,
                    endpoint_id(
                        "mattermost",
                        connector_id=str(matching.get("id") or "mattermost-default"),
                        mailbox_id=mailbox_id,
                        presence_id=str(matching.get("presence_id") or ""),
                    ),
                )
                for recipient in self._post_recipients(mailbox_id, str(post.get("user_id", ""))):
                    mailbox.send(
                        recipient,
                        str(post.get("message", "")),
                        sender="mattermost",
                        message_type="mattermost_message",
                        mailbox_id=mailbox_id,
                        mailbox_type="mattermost",
                        source_id=source_id,
                        thread_id=str(post.get("root_id", "") or "") or None,
                        extra_fields=origin_fields,
                    )

    @staticmethod
    def _source_seen(mailbox_root: Path, source_id: str) -> bool:
        if not source_id:
            return False
        from .. import mailbox_store

        for recipient in DEFAULT_INBOUND_RECIPIENTS:
            document = mailbox_store.load_mailbox(mailbox_root, recipient)
            for _key, record in mailbox_store.ordered_messages(document):
                if record.get("source_id") == source_id:
                    return True
        return False

    def _upload_attachments(self, base_url: str, mailbox_id: str, records: list[dict[str, Any]]) -> list[str]:
        file_ids: list[str] = []
        for record in records:
            path = Path(str(record.get("path", ""))).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(f"Attachment does not exist: {path}")
            with path.open("rb") as stream:
                response = self.session.post(
                    f"{base_url}/api/v4/files",
                    data={"mailbox_id": mailbox_id},
                    files={"files": (path.name, stream, record.get("mime_type") or "application/octet-stream")},
                    timeout=60,
                )
            response.raise_for_status()
            file_ids.extend(str(item["id"]) for item in response.json().get("file_infos", []))
        return file_ids

    def _send_outbound(self, base_url: str) -> None:
        mailbox = _mailbox_module()
        for message in mailbox.receive(RELAY_RECIPIENT):
            mailbox_id = str(message.get("mailbox_id") or self.default_mailbox)
            payload: dict[str, Any] = {
                "mailbox_id": mailbox_id,
                "message": str(message.get("text", "")),
                "file_ids": self._upload_attachments(base_url, mailbox_id, list(message.get("attachments") or [])),
            }
            root_id = str(message.get("root_id") or message.get("thread_id") or "")
            if root_id:
                payload["root_id"] = root_id
            response = self.session.post(f"{base_url}/api/v4/posts", json=payload, timeout=15)
            response.raise_for_status()

    def cycle(self) -> None:
        if not self._bot_user_id:
            self._connect()
        base_url = self.base_url
        self._refresh_direct_mailboxes(base_url)
        self._poll_inbound(base_url)
        self._send_outbound(base_url)
        self.status.update({"connected": True, "lastCycleAt": time.time(), "lastError": None})

    def run_forever(self) -> None:
        if not self.configure():
            raise RuntimeError(str(self.status["lastError"] or "Mattermost relay is disabled"))
        self.stop_requested = False
        self.status["running"] = True
        while not self.stop_requested:
            try:
                self.cycle()
            except Exception as error:  # keep transport alive across transient network failures
                self.status.update({"connected": False, "lastError": str(error)})
                LOGGER.exception("Mattermost relay cycle failed")
            self.sleep(1)
        self.status["running"] = False

COMMANDS = MATTERMOST_COMMANDS


def post_relay_command(url: str, token: str, command: str,
                       arguments: dict[str, Any]) -> dict[str, Any]:
    """Submit one Mattermost operation to the mailbox relay."""
    body = json.dumps({"command": command, "arguments": arguments}).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(
        f"{url.rstrip('/')}/v1/mm/command", data=body, headers=headers, method="POST",
    ), timeout=35) as response:
        return json.loads(response.read().decode("utf-8"))


def arguments_from_namespace(args: argparse.Namespace, loaded: object | None) -> dict[str, Any]:
    """Translate the generic chat CLI namespace into Mattermost command arguments."""
    command = args.command
    if command in {"ping", "teams"}:
        return {}
    if command == "list":
        return {"team": args.team, "bot": getattr(args, "bot", None),
                "page": getattr(args, "page", None),
                "per_page": getattr(args, "per_page", None)}
    if command == "names":
        return {"mailbox": args.mailbox}
    if command == "threads":
        return {"team": args.team}
    if command in {"join", "part"}:
        return {
            "mailbox": args.mailbox,
            **({"subscribe_all": True} if command == "join" and args.subscribe_all else {}),
        }
    if command == "topic":
        return {"mailbox": args.mailbox, "text": loaded}
    if command == "nick":
        return {"nickname": args.nickname}
    if command == "whois":
        return {"user": args.nickname}
    if command == "mode":
        return {"mailbox": args.target,
                "setting": args.modes[0] if args.modes else None,
                "user": args.modes[1] if len(args.modes) > 1 else None}
    if command == "invite":
        return {"user": args.nickname, "mailbox": args.mailbox}
    if command == "kick":
        return {"mailbox": args.mailbox, "user": args.nickname}
    if command in {"message", "notice"}:
        return {"target": args.target, "text": loaded,
                **({"user": args.user} if command == "notice" else {})}
    if command == "raw":
        if len(args.arguments) < 2:
            raise ValueError("Mattermost raw requires METHOD and /api/v4/PATH")
        body = loaded
        if body is None and len(args.arguments) > 2:
            try:
                body = json.loads(args.arguments[2])
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid Mattermost raw JSON: {error}") from error
        return {"method": args.arguments[0], "path": args.arguments[1], "body": body}
    raise ValueError(f"{command} is not supported by Mattermost")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mailbox-client",
        description="Run familiar mailbox and user commands through the relay's Mattermost bot",
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
    listing = commands.add_parser(
        "list", help="discover a team's public mailboxes, or --bot for the bot's own memberships")
    listing.add_argument("--team", help="limit results to one team ID")
    listing.add_argument(
        "--bot", nargs="?", const="", metavar="BOTNICK",
        help="list mailboxes the bot (or BOTNICK) has joined, like the older listing")
    listing.add_argument("--page", type=int, default=None,
                         help="discovery start page (default 0; discovery pages to the end)")
    listing.add_argument("--per-page", "--per_page", dest="per_page", type=int, default=None,
                         help="mailboxes per discovery page (default 100)")
    names = commands.add_parser("names", help="list users in a mailbox")
    names.add_argument("mailbox")
    threads = commands.add_parser("threads", help="list the bot's threads in a team")
    threads.add_argument("team", help="team ID or a discovered team name")
    join = commands.add_parser("join", help="add the bot to a mailbox")
    join.add_argument("mailbox")
    part = commands.add_parser("part", help="remove the bot from a mailbox")
    part.add_argument("mailbox")
    topic = commands.add_parser("topic", help="read or set a mailbox header")
    topic.add_argument("mailbox")
    topic.add_argument("text", nargs="?", help="new header; omit to read it")
    nick = commands.add_parser("nick", help="set the bot account nickname")
    nick.add_argument("nickname")
    whois = commands.add_parser("whois", help="show a user by ID or username")
    whois.add_argument("user")
    mode = commands.add_parser(
        "mode", help="read/set public/private visibility or mailbox operator (+o/-o)",
        description=("Read mailbox details; change public/private visibility; or map IRC-style "
                     "+o/-o to Mattermost mailbox-admin membership."),
        epilog=("Examples: mailbox-client mode mm/0/MAILBOX public; "
                "mailbox-client mode mm/0/MAILBOX +o USER"),
    )
    mode.add_argument("mailbox", help="mailbox ID, discovered name, or mm/INSTANCE/ID address")
    mode.add_argument("setting", nargs="?", help="public, private, +o, or -o; omit to inspect")
    mode.add_argument("user", nargs="?", help="user ID/name required by +o or -o")
    invite = commands.add_parser("invite", help="add a user to a mailbox")
    invite.add_argument("user")
    invite.add_argument("mailbox")
    kick = commands.add_parser("kick", help="remove a user from a mailbox")
    kick.add_argument("mailbox")
    kick.add_argument("user")
    message = commands.add_parser("message", help="post a message to a mailbox or DM mailbox")
    message.add_argument("target")
    message.add_argument("text", nargs="?")
    notice = commands.add_parser(
        "notice", help="post a tagged notice or an ephemeral user-only notice",
        description=("Post a mailbox-tagged mailbox message. With --user, use Mattermost's "
                     "ephemeral-post API so only that user sees it."),
        epilog="Example: mailbox-client notice mm/0/MAILBOX --user USER 'Maintenance soon'",
    )
    notice.add_argument("target", help="mailbox ID, discovered name, or mm/INSTANCE/ID address")
    notice.add_argument("text", nargs="?", help="notice text; alternatively use --input FILE")
    notice.add_argument("--user", help="user ID or discovered username for an ephemeral post")
    raw = commands.add_parser("raw", help="make an expert-level authenticated /api/v4 request")
    raw.add_argument("method", choices=("GET", "POST", "PUT", "DELETE"), type=str.upper)
    raw.add_argument("path", help="Mattermost path beginning /api/v4/")
    raw.add_argument("json_body", nargs="?", help="optional JSON request body")
    return result


def _instance(base_url: str) -> str:
    return (urlsplit(base_url).hostname or "mattermost").lower()


def _split_mm_sigil(value: str) -> tuple[str, str]:
    """Split a leading @ (user) or ~ (mailbox) sigil off a Mattermost identifier.

    Mirrors Mattermost markdown, where ``@name`` mentions a user and ``~name`` links
    a mailbox. Returns ``(kind, name)`` with kind ``"user"``, ``"mailbox"``, or ``""``
    (no sigil, caller's kind stands). Channel names and usernames never legitimately
    begin with these characters, so a leading sigil is unambiguous.
    """
    if value.startswith("@"):
        return "user", value[1:]
    if value.startswith("~"):
        return "mailbox", value[1:]
    return "", value


def _id(value: str, *, directory: IdentifierDirectory | None = None,
        base_url: str = "", kind: str = "") -> str:
    parsed_url = urlsplit(value)
    if parsed_url.scheme in {"http", "https"} and parsed_url.hostname:
        parts = [unquote(item) for item in parsed_url.path.split("/") if item]
        try:
            mailbox_index = parts.index("mailboxes")
            value = parts[mailbox_index + 1]
        except (ValueError, IndexError) as error:
            raise ValueError(
                "Mattermost URLs must contain /mailboxes/MAILBOX_NAME"
            ) from error
        active_host = _instance(base_url)
        if active_host != "mattermost" and parsed_url.hostname.lower() != active_host:
            raise ValueError(
                f"Mattermost URL server {parsed_url.hostname} does not match {active_host}"
            )
    endpoint = parse_endpoint(value)
    if endpoint:
        if endpoint.adapter != "mattermost":
            raise ValueError(f"expected an mm address, received {value}")
        value = endpoint.identifier
    sigil_kind, value = _split_mm_sigil(value)
    if sigil_kind:
        kind = sigil_kind
    if directory and not (len(value) == 26 and value.isalnum()):
        # mm/0 is a routing alias, not a second registry namespace. Resolve it
        # against the canonical instance derived from the active base URL.
        systems = [f"mm/{_instance(base_url)}"]
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
    sigil_kind, _ = _split_mm_sigil(endpoint.identifier)
    identifier = _id(value, directory=directory, base_url=base_url, kind=sigil_kind or "mailbox")
    if sigil_kind == "user":
        # Preserve the user marker so the resolved address stays self-describing.
        return f"mm/{endpoint.instance}/@{identifier}"
    return f"mm/{endpoint.instance}/{identifier}"


def _record_aliases(record: dict[str, Any], *, kind: str = "") -> list[str]:
    """Return trustworthy human-entered aliases for a Mattermost object."""
    fields = ["display_name", "name", "username", "nickname"]
    if kind == "user" or record.get("username"):
        fields.append("email")
    aliases = [
        str(record.get(field) or "").strip()[:512]
        for field in fields
        if isinstance(record.get(field), (str, int)) and str(record.get(field) or "").strip()
    ]
    if kind == "user" or record.get("username"):
        full_name = " ".join(filter(None, (
            str(record.get("first_name") or "").strip(),
            str(record.get("last_name") or "").strip(),
        )))[:512]
        if full_name:
            aliases.append(full_name)
    return list(dict.fromkeys(aliases))


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
        aliases = _record_aliases(record, kind=kind) or [text]
        for alias in aliases:
            directory.remember(identifier, alias, system=f"mm/{instance}", kind=kind, metadata=metadata)
    return {**record, "address": f"mm/{instance}/{identifier}", "resource_type": kind}


def _remember_many(directory: IdentifierDirectory | None, base_url: str, value: Any,
                   *, kind: str) -> Any:
    return [_remember(directory, base_url, item, kind=kind) for item in value] if isinstance(value, list) else value


def remember_named_ids(directory: IdentifierDirectory | None, base_url: str, value: Any,
                       *, kind: str = "") -> Any:
    """Visit every JSON object and persist each trustworthy ID/name pairing."""
    if directory is None:
        return value
    if isinstance(value, list):
        for item in value:
            remember_named_ids(directory, base_url, item, kind=kind)
        return value
    if not isinstance(value, dict):
        return value
    pairs: list[tuple[str, list[str], str]] = []
    direct_id = str(value.get("id") or "").strip()
    direct_kind = kind or ("user" if value.get("username") else
                           "mailbox" if value.get("team_id") else "")
    direct_aliases = _record_aliases(value, kind=direct_kind)
    if direct_id and direct_aliases:
        pairs.append((direct_id, direct_aliases, direct_kind))
    for field, raw_identifier in value.items():
        if not field.endswith("_id") or not isinstance(raw_identifier, (str, int)):
            continue
        stem = field[:-3]
        aliases = list(dict.fromkeys(
            str(value.get(alias_field) or "").strip()[:512]
            for alias_field in (
                f"{stem}_display_name", f"{stem}_name", f"{stem}_username",
                # Common JSON objects use username beside user_id.
                *(('username', 'display_name', 'nickname') if stem == 'user' else ()),
            )
            if isinstance(value.get(alias_field), (str, int)) and
            str(value.get(alias_field) or "").strip()
        ))
        identifier = str(raw_identifier).strip()
        if identifier and aliases:
            inferred_kind = ({"user": "user", "owner": "user", "creator": "user",
                              "mailbox": "mailbox", "team": "team", "post": "post",
                              "root": "thread"}.get(stem, stem))
            pairs.append((identifier, aliases, inferred_kind))
    if pairs:
        metadata = dict(value)
        instance = _instance(base_url)
        for identifier, aliases, inferred_kind in pairs:
            for alias in aliases:
                directory.remember(identifier, alias, system=f"mm/{instance}",
                                   kind=inferred_kind, metadata=metadata)
    for item in value.values():
        if isinstance(item, (dict, list)):
            remember_named_ids(directory, base_url, item)
    return value


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


def _direct_mailbox_id(session: requests.Session, base_url: str, me_id: str, user_id: str) -> str:
    """Get-or-create the direct-message mailbox between the bot and a user.

    In Mattermost a DM is itself a mailbox (type ``"D"``); POST /mailboxes/direct is
    idempotent and returns the existing DM mailbox when one already exists.
    """
    mailbox = _request(session, base_url, "POST", "/api/v4/mailboxes/direct", [me_id, user_id])
    return str(mailbox["id"])


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
        return remember_named_ids(directory, base_url, _request(
            session, base_url, method, path, arguments.get("body"),
        ))
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
        bot = arguments.get("bot")
        if bot is not None:
            # Older behaviour: only the mailboxes the bot (or a named user) has joined.
            nick = str(bot or "")
            user_id = _user_id(session, base_url, nick, directory) if nick else me_id
            path = (f"/api/v4/users/{user_id}/teams/{team}/mailboxes" if team else
                    f"/api/v4/users/{user_id}/mailboxes")
            mailboxes = _request(session, base_url, "GET", path)
        else:
            # Discover every public mailbox each team exposes. The list API is
            # page/per_page only and the list can shift while we read it, so we
            # de-duplicate by mailbox id and keep going for a couple of pages past
            # the first empty one as a safety margin (Mattermost caps per_page
            # ~200, so large pages still need several requests).
            start_page = int(arguments.get("page") or 0)
            per_page = int(arguments.get("per_page") or 100)
            empty_page_margin = 2
            team_ids = [team] if team else [
                str(item["id"]) for item in _request(
                    session, base_url, "GET", f"/api/v4/users/{me_id}/teams")
            ]
            mailboxes = []
            seen_ids: set[str] = set()
            for team_id in team_ids:
                page = start_page
                empty_pages = 0
                while empty_pages <= empty_page_margin:
                    query = urlencode({"page": page, "per_page": per_page})
                    batch = _request(session, base_url, "GET",
                                     f"/api/v4/teams/{team_id}/mailboxes?{query}")
                    if isinstance(batch, list) and batch:
                        empty_pages = 0
                        for item in batch:
                            mailbox_id = str(item.get("id") or "")
                            if mailbox_id and mailbox_id not in seen_ids:
                                seen_ids.add(mailbox_id)
                                mailboxes.append(item)
                    else:
                        empty_pages += 1
                    page += 1
                    if page - start_page >= 10000:
                        break
        return [_remember(directory, base_url, item, kind=(
            "dm" if item.get("type") == "D" else "group" if item.get("type") == "G" else "mailbox"
        )) for item in mailboxes]
    if command == "names":
        mailbox = _id(str(arguments["mailbox"]), directory=directory, base_url=base_url, kind="mailbox")
        return _remember_many(directory, base_url, _request(session, base_url, "GET",
            f"/api/v4/users?in_mailbox={quote(mailbox)}&per_page=200"), kind="user")
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
        mailbox = _id(str(arguments["mailbox"]), directory=directory, base_url=base_url, kind="mailbox")
        return _request(session, base_url, "POST", f"/api/v4/mailboxes/{mailbox}/members",
                        {"user_id": user_id})
    if command in {"part", "kick"}:
        user_id = (me_id if command == "part" else
                   _user_id(session, base_url, str(arguments["user"]), directory))
        mailbox = _id(str(arguments["mailbox"]), directory=directory, base_url=base_url, kind="mailbox")
        return _request(session, base_url, "DELETE", f"/api/v4/mailboxes/{mailbox}/members/{user_id}")
    if command == "topic":
        mailbox = _id(str(arguments["mailbox"]), directory=directory, base_url=base_url, kind="mailbox")
        if arguments.get("text") is None:
            return _request(session, base_url, "GET", f"/api/v4/mailboxes/{mailbox}")
        return _request(session, base_url, "PUT", f"/api/v4/mailboxes/{mailbox}/patch",
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
        mailbox = _id(str(arguments["mailbox"]), directory=directory, base_url=base_url, kind="mailbox")
        setting = str(arguments.get("setting") or "").lower()
        if not setting:
            return _request(session, base_url, "GET", f"/api/v4/mailboxes/{mailbox}")
        if setting in {"public", "private"}:
            return _request(session, base_url, "PUT", f"/api/v4/mailboxes/{mailbox}/patch",
                            {"type": "O" if setting == "public" else "P"})
        if setting in {"+o", "-o"}:
            if not arguments.get("user"):
                raise ValueError(f"mm mode {setting} requires a user")
            user_id = _user_id(session, base_url, str(arguments["user"]), directory)
            roles = "mailbox_user mailbox_admin" if setting == "+o" else "mailbox_user"
            return _request(session, base_url, "PUT",
                            f"/api/v4/mailboxes/{mailbox}/members/{user_id}/roles",
                            {"roles": roles})
        raise ValueError("Mattermost mode supports public, private, +o, and -o")
    if command in {"message", "notice"}:
        target = str(arguments["target"])
        endpoint = parse_endpoint(target)
        raw_identifier = (endpoint.identifier
                          if endpoint and endpoint.adapter == "mattermost" else target)
        target_kind, _ = _split_mm_sigil(raw_identifier)
        if target_kind == "user":
            # @user addresses a person: resolve the user and post to their DM mailbox.
            user_id = _user_id(session, base_url, target, directory)
            mailbox = _direct_mailbox_id(session, base_url, me_id, user_id)
        else:
            mailbox = _id(target, directory=directory,
                          base_url=base_url, kind="mailbox")
        body: dict[str, Any] = {"mailbox_id": mailbox, "message": arguments["text"]}
        if command == "notice":
            body["props"] = {"mailbox_notice": True}
            if arguments.get("user"):
                user_id = _user_id(session, base_url, str(arguments["user"]), directory)
                return _request(session, base_url, "POST", "/api/v4/posts/ephemeral",
                                {"user_id": user_id, "post": body})
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
    token = args.token or os.environ.get("AGENT_MAILBOX_TOKEN", "")
    payload = _payload(args)
    output = post_relay_command(args.url, token, payload["command"], payload["arguments"])
    print(render(output.get("result", output), args.format))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
