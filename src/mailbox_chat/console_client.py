"""Trusted Speaker interactive client for REST/WebSocket or local JSONL mailboxes."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from urllib.parse import quote

import websocket

from . import agent_mailbox

CLIENT_NAME = "Trusted Speaker"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="trusted-speaker",
        description="Trusted Speaker console client for relay WebSocket or local JSONL chat",
    )
    result.add_argument("identity", help="Unique stable mailbox recipient identity")
    result.add_argument("--to", dest="destination", default="local-agent",
                        help="initial destination mailbox identity (default: local-agent)")
    transport = result.add_mutually_exclusive_group()
    transport.add_argument("--url", default="ws://127.0.0.1:46667/v1/chat/ws",
                           help="relay WebSocket endpoint (default: local relay on port 46667)")
    transport.add_argument("--dir", type=Path,
                           help="use this local JSONL mailbox directory instead of WebSocket")
    result.add_argument("--interval", type=float, default=0.5,
                        help="local mailbox polling interval in seconds (default: 0.5)")
    return result


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


def run(identity: str, destination: str, url: str, *, directory: Path | None = None,
        interval: float = 0.5) -> int:
    connection: websocket.WebSocket | None = None
    if directory is None:
        try:
            connection = websocket.create_connection(f"{url}?recipient={quote(identity, safe='')}", timeout=10)
        except Exception as error:
            print(
                f"{CLIENT_NAME} service unavailable: {error}. "
                "Verify the URL, network access, and authentication requirements.",
                file=sys.stderr,
            )
            return 2
        connection.settimeout(None)
    else:
        directory = directory.expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    if connection is not None:
        threading.Thread(target=_receive, args=(connection, stop), daemon=True).start()
        transport = url
    else:
        threading.Thread(target=_receive_local, args=(directory, identity, stop, interval), daemon=True).start()
        transport = str(directory)
    print(f"{CLIENT_NAME} connected as {identity}; destination is {destination}; transport is {transport}.")
    print("Commands: /to ID, /ping, /help, /quit")
    try:
        while True:
            line = input(f"{identity} -> {destination}> ").strip()
            if not line:
                continue
            if line == "/quit":
                return 0
            if line == "/help":
                print("/to ID changes destination; /ping checks the session; /quit exits")
            elif line == "/ping":
                if connection is not None:
                    connection.send(json.dumps({"action": "ping"}))
                else:
                    print(f"[local mailbox ready: {directory}]")
            elif line.startswith("/to "):
                destination = line[4:].strip() or destination
                print(f"Destination changed to {destination}")
            else:
                if connection is not None:
                    connection.send(json.dumps({"action": "send", "to": destination, "text": line}))
                else:
                    message = agent_mailbox.send(destination, line, sender=identity, root=directory)
                    print(f"[sent {message['id']}]")
    except (EOFError, KeyboardInterrupt):
        return 0
    finally:
        stop.set()
        if connection is not None:
            connection.close()


def main() -> int:
    arguments = parser().parse_args()
    return run(arguments.identity, arguments.destination, arguments.url,
               directory=arguments.dir, interval=arguments.interval)


if __name__ == "__main__":
    raise SystemExit(main())
