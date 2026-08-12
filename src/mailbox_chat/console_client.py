"""Trusted Speaker interactive client for mailbox-backed WebSocket chat."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from urllib.parse import quote

import websocket

CLIENT_NAME = "Trusted Speaker"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="trusted-speaker",
        description="Trusted Speaker console client for mailbox WebSocket chat",
    )
    result.add_argument("identity", help="Unique stable mailbox recipient identity")
    result.add_argument("--to", dest="destination", default="local-agent",
                        help="initial destination mailbox identity (default: local-agent)")
    result.add_argument("--url", default="ws://127.0.0.1:46667/v1/chat/ws",
                        help="relay WebSocket endpoint")
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


def run(identity: str, destination: str, url: str) -> int:
    try:
        connection = websocket.create_connection(f"{url}?recipient={quote(identity, safe='')}", timeout=10)
    except Exception as error:
        print(
            f"{CLIENT_NAME} service unavailable: {error}. "
            "Verify the URL, network access, and any future authentication requirements.",
            file=sys.stderr,
        )
        return 2
    connection.settimeout(None)
    stop = threading.Event()
    threading.Thread(target=_receive, args=(connection, stop), daemon=True).start()
    print(f"{CLIENT_NAME} connected as {identity}; destination is {destination}.")
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
                connection.send(json.dumps({"action": "ping"}))
            elif line.startswith("/to "):
                destination = line[4:].strip() or destination
                print(f"Destination changed to {destination}")
            else:
                connection.send(json.dumps({"action": "send", "to": destination, "text": line}))
    except (EOFError, KeyboardInterrupt):
        return 0
    finally:
        stop.set()
        connection.close()


def main() -> int:
    arguments = parser().parse_args()
    return run(arguments.identity, arguments.destination, arguments.url)


if __name__ == "__main__":
    raise SystemExit(main())
