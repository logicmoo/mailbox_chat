"""Manage persistent channel relay routes from the server command line."""

from __future__ import annotations

import argparse
import json
import os
import re
import uuid
import sys
from pathlib import Path
from typing import Any

from .listener_registry import CONFIG_DIR_ENV, load_listeners, relays_file


SAFE_ID = re.compile(r"[^a-zA-Z0-9_.-]+")


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read relay registry {path}: {error}") from error
    if not isinstance(payload.get("routes"), list):
        payload["routes"] = []
    return payload


def _write(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def attach(source_listener: str, source_channel: str, destination_listener: str,
           destination_channel: str, *, controller: str = "presence",
           route_id: str = "") -> dict[str, Any]:
    listeners = {item["id"]: item for item in load_listeners()}
    if source_listener not in listeners:
        raise ValueError(f"unknown source listener: {source_listener}")
    if destination_listener not in listeners:
        raise ValueError(f"unknown destination listener: {destination_listener}")
    if controller != "presence" and not controller.startswith("agent:"):
        raise ValueError("controller must be presence or agent:MAILBOX_RECIPIENT")
    if controller.startswith("agent:") and not controller[6:].strip():
        raise ValueError("agent controller requires a mailbox recipient")
    path = relays_file()
    payload = _load(path)
    clean_id = SAFE_ID.sub("-", route_id.strip()) if route_id else SAFE_ID.sub(
        "-", f"route-{source_listener}-{destination_listener}-{uuid.uuid4().hex[:8]}"
    )
    if not clean_id or any(item.get("id") == clean_id for item in payload["routes"]):
        raise ValueError(f"route id must be non-empty and unique: {clean_id or '<empty>'}")
    source: dict[str, Any] = {"listener_id": source_listener}
    if source_channel not in {"", "*"}:
        source["channel_id"] = source_channel
    destination = listeners[destination_listener]
    controller_record = ({"type": "relay_agent", "mailbox_recipient": controller[6:]}
                         if controller.startswith("agent:") else
                         {"type": "presence_controller",
                          "presence_id": str(destination.get("presence_id") or destination_listener)})
    record = {
        "id": clean_id,
        "enabled": True,
        "source": source,
        "destinations": [{"adapter": destination["adapter"],
                          "listener_id": destination_listener,
                          "channel_id": destination_channel}],
        "controller": controller_record,
        "created_by": "mailbox-relay-route",
    }
    payload["routes"].append(record)
    _write(path, payload)
    return record


def detach(route_id: str) -> bool:
    path = relays_file()
    payload = _load(path)
    found = False
    for route in payload["routes"]:
        if route.get("id") == route_id:
            route["enabled"] = False
            found = True
    if found:
        _write(path, payload)
    return found


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mailbox-relay-route",
        description="List, attach, or detach persistent routes in relays.json",
        epilog="Use '*' as SOURCE_CHANNEL to match every channel heard by the source listener.",
    )
    result.add_argument("--config-dir", type=Path,
                        help="directory containing relays.json (default: project config directory)")
    result.add_argument("--json", action="store_true", help="render machine-readable JSON output")
    commands = result.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("list", help="list all configured routes",
                        description="List enabled and disabled persistent relay routes.")
    add = commands.add_parser("attach", help="create and enable a route",
                              description="Attach one source listener/channel to a destination.")
    add.add_argument("source_listener", help="source listener ID from relays.json")
    add.add_argument("source_channel", help="source channel ID, or * to match every source channel")
    add.add_argument("destination_listener", help="destination listener ID from relays.json")
    add.add_argument("destination_channel", help="destination channel, conversation, user, or group ID")
    add.add_argument("--controller", default="presence",
                     help="presence or agent:MAILBOX_RECIPIENT (default: presence)")
    add.add_argument("--id", dest="route_id", default="", help="explicit unique route ID")
    remove = commands.add_parser("detach", help="disable a route",
                                 description="Disable a persistent route without deleting its history.")
    remove.add_argument("route_id", help="route ID shown by the list command")
    return result


def _normalize_global_options(argv: list[str]) -> list[str]:
    leading: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--json":
            leading.append(argument)
            index += 1
        elif argument == "--config-dir" and index + 1 < len(argv):
            leading.extend(argv[index:index + 2])
            index += 2
        elif argument.startswith("--config-dir="):
            leading.append(argument)
            index += 1
        else:
            remaining.append(argument)
            index += 1
    return leading + remaining


def main(argv: list[str] | None = None) -> int:
    supplied = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(_normalize_global_options(supplied))
    if args.config_dir:
        os.environ[CONFIG_DIR_ENV] = str(args.config_dir.expanduser().resolve())
    try:
        if args.command == "list":
            routes = _load(relays_file())["routes"]
            if args.json:
                print(json.dumps({"routes": routes}, ensure_ascii=False, indent=2))
            elif routes:
                for route in routes:
                    state = "enabled" if route.get("enabled", True) else "disabled"
                    print(f"{route.get('id')}\t{state}\t{route.get('source')} -> {route.get('destinations')}")
            else:
                print("No routes configured.")
            return 0
        if args.command == "attach":
            route = attach(args.source_listener, args.source_channel, args.destination_listener,
                           args.destination_channel, controller=args.controller, route_id=args.route_id)
            print(json.dumps(route, ensure_ascii=False, indent=2) if args.json else
                  f"Attached route {route['id']}")
            return 0
        if not detach(args.route_id):
            print(f"Unknown route: {args.route_id}")
            return 1
        print(json.dumps({"detached": args.route_id}) if args.json else f"Detached route {args.route_id}")
        return 0
    except ValueError as error:
        parser().error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
