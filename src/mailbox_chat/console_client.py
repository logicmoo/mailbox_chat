"""Trusted Speaker interactive client for REST/WebSocket or local JSONL mailboxes."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import websocket

from . import agent_mailbox
from .agent_mailbox import CHAT_COMMANDS
from . import channel_admin, contact_admin, discovery_admin, irc_admin, registry_admin, route_admin, token_admin

CLIENT_NAME = "Mailbox Console"
CHAT_PATH = "/v1/chat/ws"


def _split_command(value: str) -> list[str]:
    items = shlex.split(value, posix=sys.platform != "win32")
    if sys.platform == "win32":
        return [item[1:-1] if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'"
                else item for item in items]
    return items


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mailbox-console",
        description="Trusted Speaker console client for relay WebSocket or local JSONL chat",
    )
    result.add_argument("identity", nargs="?",
                        help="unique stable agent ID; alternatively use --as")
    result.add_argument("--as", dest="agent_id",
                        help="default stable agent ID used as the sender")
    result.add_argument("--from", dest="source",
                        help="default sending presence or external source endpoint")
    result.add_argument("--to", dest="destination", default="local-agent",
                        help="initial destination mailbox identity (default: local-agent)")
    result.add_argument("--on", dest="chat_instance", default="",
                        help="default chat platform instance, such as mm/chat.singularitynet.io")
    transport = result.add_mutually_exclusive_group()
    transport.add_argument("--url", default="http://127.0.0.1:46667",
                           help="relay HTTP(S) base URL or full WebSocket endpoint "
                                "(default: http://127.0.0.1:46667)")
    transport.add_argument("--dir", type=Path,
                           help="use this local JSONL mailbox directory instead of WebSocket")
    result.add_argument("--interval", type=float, default=0.5,
                        help="local mailbox polling interval in seconds (default: 0.5)")
    return result


def websocket_url(value: str) -> str:
    """Convert a relay server address into its chat WebSocket endpoint."""
    parsed = urlsplit(value.strip())
    schemes = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}
    scheme = schemes.get(parsed.scheme.lower())
    if scheme is None or not parsed.netloc:
        raise ValueError("--url must be an HTTP(S) server address or WS(S) endpoint")
    path = parsed.path.rstrip("/")
    if not path:
        path = CHAT_PATH
    elif path != CHAT_PATH:
        path = f"{path}{CHAT_PATH}"
    return urlunsplit((scheme, parsed.netloc, path, parsed.query, ""))


def relay_http_url(value: str) -> str:
    """Convert the console transport URL back to the relay HTTP base URL."""
    parsed = urlsplit(value.strip())
    schemes = {"http": "http", "https": "https", "ws": "http", "wss": "https"}
    scheme = schemes.get(parsed.scheme.lower())
    if scheme is None or not parsed.netloc:
        raise ValueError("console URL must be HTTP(S) or WS(S)")
    path = parsed.path.rstrip("/")
    if path.endswith(CHAT_PATH):
        path = path[:-len(CHAT_PATH)]
    return urlunsplit((scheme, parsed.netloc, path, parsed.query, ""))


def run_administration(command: str, arguments: str | list[str], *, url: str,
                       directory: Path | None) -> int:
    """Run one mailbox-client administration family without leaving the console."""
    try:
        argv = (list(arguments) if isinstance(arguments, list)
                else _split_command(arguments))
    except ValueError as error:
        print(f"[error] {error}")
        return 2
    if command == "token":
        handler = token_admin.main
    elif command == "route":
        handler = route_admin.main
        if not any(item == "--url" or item.startswith("--url=") or
                   item == "--config-dir" or item.startswith("--config-dir=") for item in argv):
            argv = ["--url", relay_http_url(url), *argv]
    elif command == "contacts":
        handler = contact_admin.main
        if not any(item == "--url" or item.startswith("--url=") or
                   item == "--dir" or item.startswith("--dir=") for item in argv):
            argv = (["--dir", str(directory)] if directory is not None else
                    ["--url", relay_http_url(url)]) + argv
    elif command == "registry":
        handler = registry_admin.main
        if not any(item == "--url" or item.startswith("--url=") or
                   item == "--dir" or item.startswith("--dir=") for item in argv):
            argv = (["--dir", str(directory)] if directory is not None else
                    ["--url", relay_http_url(url)]) + argv
    elif command == "discover":
        handler = discovery_admin.main
        if directory is None and not any(
            item == "--url" or item.startswith("--url=") for item in argv
        ):
            argv = ["--url", relay_http_url(url), *argv]
    elif command == "channels":
        handler = channel_admin.main
        if directory is None and not any(
            item == "--url" or item.startswith("--url=") for item in argv
        ):
            argv = ["--url", relay_http_url(url), *argv]
    elif command in CHAT_COMMANDS:
        handler = irc_admin.main
        argv = [command, *argv]
        if directory is None and not any(
            item == "--url" or item.startswith("--url=") for item in argv
        ):
            argv = ["--url", relay_http_url(url), *argv]
    try:
        return handler(argv)
    except SystemExit as error:
        return int(error.code or 0)


def run_client_command(command_line: str, *, identity: str, destination: str,
                       url: str, directory: Path | None, chat_instance: str = "") -> int:
    """Run any mailbox-client command entered with a leading slash."""
    try:
        argv = _split_command(command_line)
    except ValueError as error:
        print(f"[error] {error}")
        return 2
    if not argv:
        return 0
    if argv[0] in CHAT_COMMANDS and chat_instance:
        from .endpoint_address import parse_endpoint
        has_on = any(item == "--on" or item.startswith("--on=") for item in argv)
        has_qualified_address = False
        for item in argv[1:]:
            try:
                if parse_endpoint(item):
                    has_qualified_address = True
                    break
            except ValueError:
                continue
        if not has_on and not has_qualified_address:
            argv.extend(["--on", chat_instance])
    if argv[0] in {"token", "route", "contacts", "registry", "discover", "channels", *CHAT_COMMANDS}:
        return run_administration(argv[0], argv[1:], url=url, directory=directory)
    transport = ["--dir", str(directory)] if directory is not None else ["--url", relay_http_url(url)]
    explicit_to = any(item == "--to" or item.startswith("--to=") for item in argv)
    explicit_as = any(item == "--as" or item.startswith("--as=") for item in argv)
    command = argv[0]
    defaults: list[str] = []
    if command == "send":
        if not explicit_as:
            defaults.extend(["--as", identity])
        if not explicit_to:
            defaults.extend(["--to", destination])
    elif command in {"receive", "peek", "poll", "follow", "unread-count",
                     "subscribe", "unsubscribe", "subscriptions"} and not explicit_to:
        defaults.extend(["--to", identity])
    try:
        return agent_mailbox.main([*transport, *defaults, *argv])
    except SystemExit as error:
        return int(error.code or 0)


def display_event(raw: str) -> str:
    event = json.loads(raw)
    if event.get("type") == "message":
        message = event.get("message") or {}
        return f"[{message.get('from', '?')} -> {message.get('to', '?')}] {message.get('text', '')}"
    if event.get("type") == "sent":
        return f"[sent {(event.get('message') or {}).get('id', '')}]"
    if event.get("type") == "error":
        return f"[error] {event.get('error', '')}"
    return f"[{event.get('type', 'event')}] {json.dumps(event, ensure_ascii=False)}"


def _receive(connection: websocket.WebSocket, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            raw = connection.recv()
        except websocket.WebSocketConnectionClosedException:
            return
        if raw is None:
            return
        print("\r" + display_event(str(raw)))


def _receive_local(directory: Path, identity: str, stop: threading.Event, interval: float) -> None:
    while not stop.is_set():
        for message in agent_mailbox.receive(identity, root=directory):
            print("\r" + display_event(json.dumps({"type": "message", "message": message})), flush=True)
        stop.wait(interval)


def _open_transport(identity: str, url: str, directory: Path | None,
                    interval: float) -> tuple[websocket.WebSocket | None, threading.Event, str, Path | None]:
    connection: websocket.WebSocket | None = None
    resolved_directory: Path | None = None
    if directory is None:
        endpoint = websocket_url(url)
        separator = "&" if "?" in endpoint else "?"
        connection = websocket.create_connection(
            f"{endpoint}{separator}recipient={quote(identity, safe='')}", timeout=10
        )
        connection.settimeout(None)
        transport = endpoint
    else:
        resolved_directory = directory.expanduser().resolve()
        resolved_directory.mkdir(parents=True, exist_ok=True)
        transport = str(resolved_directory)
    stop = threading.Event()
    if connection is not None:
        threading.Thread(target=_receive, args=(connection, stop), daemon=True).start()
    else:
        threading.Thread(
            target=_receive_local, args=(resolved_directory, identity, stop, interval), daemon=True,
        ).start()
    return connection, stop, transport, resolved_directory


def _switch_url(value: str, scheme: str | None = None) -> str:
    value = value.strip()
    if not value:
        raise ValueError("a server address is required")
    if "://" not in value and scheme:
        return f"{scheme}://{value}"
    if scheme:
        parsed = urlsplit(value)
        return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    return value


def run(identity: str, destination: str, url: str, *, source: str = "",
        directory: Path | None = None, interval: float = 0.5,
        chat_instance: str = "") -> int:
    try:
        connection, stop, transport, directory = _open_transport(identity, url, directory, interval)
    except Exception as error:
        print(
            f"{CLIENT_NAME} service unavailable: {error}. "
            "Verify the URL, network access, and authentication requirements.",
            file=sys.stderr,
        )
        return 2
    print(f"{CLIENT_NAME} connected as {identity}; destination is {destination}; transport is {transport}.")
    print("Commands: /as, /from, /to, /on, /join, /console, /leave, /url, /ws, /wss, /dir, /token, /route, "
          "/contacts, /ping, /help, /quit")
    joined: set[str] = set()
    temporary_console = ""
    try:
        while True:
            line = input(f"{identity} -> {destination}> ").strip()
            if not line:
                continue
            if line == "/quit":
                return 0
            if line == "/help":
                print("/as AGENT_ID changes agent; /from SOURCE changes presence/source; "
                      "/to DESTINATION changes destination; "
                      "/on TYPE/INSTANCE changes the default chat platform; "
                      "/join or /console ADDRESS subscribes and enters a conversation; /leave exits it; "
                      "/url, /ws, /wss, and /dir replace the active transport; "
                      "any other /COMMAND runs the matching mailbox-client command; "
                      "/ping checks the session; /quit exits")
            elif line == "/as":
                print(f"Agent ID is {identity}")
            elif line.startswith("/as "):
                requested_identity = line[4:].strip()
                if requested_identity:
                    identity = requested_identity
                    print(f"Agent ID changed to {identity}")
            elif line == "/from":
                print(f"Source is {source or '(agent default)'}")
            elif line.startswith("/from "):
                source = line[6:].strip()
                print(f"Source changed to {source or '(agent default)'}")
            elif line == "/ping":
                if chat_instance:
                    run_client_command("ping", identity=identity, destination=destination,
                                       url=url, directory=directory, chat_instance=chat_instance)
                elif connection is not None:
                    connection.send(json.dumps({"action": "ping"}))
                else:
                    print(f"[local mailbox ready: {directory}]")
            elif line.startswith("/to "):
                destination = line[4:].strip() or destination
                print(f"Destination changed to {destination}")
            elif line == "/to":
                print(f"Destination is {destination}")
            elif line == "/on":
                print(f"Chat platform is {chat_instance or '(IRC default)'}")
            elif line.startswith("/on "):
                requested_instance = line[4:].strip().rstrip("/")
                parts = requested_instance.split("/", 1)
                if len(parts) != 2 or not all(parts):
                    print("[error] /on requires TYPE/INSTANCE, such as mm/chat.singularitynet.io")
                else:
                    chat_instance = requested_instance
                    print(f"Chat platform changed to {chat_instance}")
            elif line in {"/join", "/console"}:
                print(f"Console conversation is {source or '(none)'}")
            elif line.startswith("/join ") or line.startswith("/console "):
                console_only = line.startswith("/console ")
                address = line.split(None, 1)[1].strip()
                from .endpoint_address import EndpointAddress, parse_endpoint
                parsed_address = parse_endpoint(address)
                if parsed_address and parsed_address.adapter == "irc" and not parsed_address.identifier.startswith(
                    ("#", "&", "+", "!")
                ) and parsed_address.identifier != "status":
                    address = EndpointAddress(
                        "irc", parsed_address.instance, f"#{parsed_address.identifier}",
                    ).canonical
                if console_only and temporary_console and temporary_console != address and temporary_console not in joined:
                    run_client_command(
                        f"unsubscribe {shlex.quote(temporary_console)} --to {shlex.quote(identity)}",
                        identity=identity, destination=destination, url=url, directory=directory,
                    )
                if run_client_command(
                    f"subscribe {shlex.quote(address)} --to {shlex.quote(identity)}",
                    identity=identity, destination=destination, url=url, directory=directory,
                ) == 0:
                    source = destination = address
                    if console_only:
                        temporary_console = address
                        print(f"Console temporarily connected to {address} as {identity}")
                    else:
                        joined.add(address)
                        temporary_console = ""
                        print(f"Joined {address} as {identity}")
            elif line == "/leave" or line.startswith("/leave "):
                address = line[6:].strip() or source
                if not address:
                    print("[error] No console conversation")
                elif run_client_command(
                    f"unsubscribe {shlex.quote(address)} --to {shlex.quote(identity)}",
                    identity=identity, destination=destination, url=url, directory=directory,
                ) == 0:
                    joined.discard(address)
                    if temporary_console == address:
                        temporary_console = ""
                    if source == address:
                        source = ""
                    print(f"Left {address}")
            elif line in {"/url", "/ws", "/wss", "/dir"}:
                print(f"Transport is {transport}")
            elif any(line.startswith(f"/{name} ") for name in ("url", "ws", "wss", "dir")):
                command, value = line[1:].split(None, 1)
                try:
                    next_directory = Path(value) if command == "dir" else None
                    next_url = url if command == "dir" else _switch_url(
                        value, command if command in {"ws", "wss"} else None,
                    )
                    new_connection, new_stop, new_transport, next_directory = _open_transport(
                        identity, next_url, next_directory, interval,
                    )
                except Exception as error:
                    print(f"[error] Cannot change transport: {error}")
                    continue
                stop.set()
                if connection is not None:
                    connection.close()
                connection, stop, transport, directory = (
                    new_connection, new_stop, new_transport, next_directory
                )
                if directory is None:
                    url = next_url
                print(f"Transport changed to {transport}")
            elif line == "/token" or line.startswith("/token "):
                run_administration("token", line[len("/token"):].strip(),
                                   url=url, directory=directory)
            elif line == "/route" or line.startswith("/route "):
                run_administration("route", line[len("/route"):].strip(),
                                   url=url, directory=directory)
            elif line == "/contacts" or line.startswith("/contacts "):
                run_administration("contacts", line[len("/contacts"):].strip(),
                                   url=url, directory=directory)
            elif line.startswith("/"):
                run_client_command(line[1:], identity=identity, destination=destination,
                                   url=url, directory=directory, chat_instance=chat_instance)
            else:
                from .endpoint_address import parse_endpoint
                if parse_endpoint(destination):
                    run_client_command(
                        f"send -- {shlex.quote(line)}", identity=identity,
                        destination=destination, url=url, directory=directory,
                    )
                    continue
                if connection is not None:
                    connection.send(json.dumps({
                        "action": "send", "as": identity, "from": source,
                        "to": destination, "text": line,
                    }))
                else:
                    message = agent_mailbox.send(
                        destination, line, sender=identity, root=directory,
                        extra_fields={"source_presence": source} if source else None,
                    )
                    print(f"[sent {message['id']}]")
    except (EOFError, KeyboardInterrupt):
        return 0
    finally:
        stop.set()
        if connection is not None:
            connection.close()


def main() -> int:
    arguments = parser().parse_args()
    if arguments.identity and arguments.agent_id:
        parser().error("use either positional identity or --as, not both")
    identity = arguments.agent_id or arguments.identity
    if not identity:
        parser().error("an agent ID is required; use --as AGENT_ID")
    return run(identity, arguments.destination, arguments.url,
               source=arguments.source or "", directory=arguments.dir, interval=arguments.interval,
               chat_instance=arguments.chat_instance)


if __name__ == "__main__":
    raise SystemExit(main())
