"""CLI administration for cursor-driven mailbox bus relays."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .bus_relay import add_relay, delete_relay, load_relays, pump_relays
from .listener_registry import CONFIG_DIR_ENV


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mailbox-client relay",
        description="Manage cursor-driven pumps from mailbox buses to external endpoints.",
    )
    result.add_argument("--config-dir", type=Path)
    result.add_argument("--dir", type=Path, dest="mailbox_root")
    result.add_argument("--json", action="store_true")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list configured bus relays and cursor identities")
    add = commands.add_parser("add", help="add a source-bus cursor pump")
    add.add_argument("source_bus")
    add.add_argument("destination", help="external TYPE/INSTANCE/IDENTIFIER endpoint")
    add.add_argument("--id", dest="relay_id", default="")
    add.add_argument("--start", default="now",
                     help="initial cursor position: now, beginning, timestamp, or duration")
    add.add_argument("--dry-run", action="store_true")
    remove = commands.add_parser("del", help="delete relay config but retain its cursor")
    remove.add_argument("relay_id")
    remove.add_argument("--dry-run", action="store_true")
    pump = commands.add_parser("pump", help="manually pump enabled relays once")
    pump.add_argument("relay_ids", nargs="*")
    pump.add_argument("--limit", type=int)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.config_dir:
        os.environ[CONFIG_DIR_ENV] = str(args.config_dir.expanduser().resolve())
    mailbox_root = args.mailbox_root.expanduser().resolve() if args.mailbox_root else None
    if args.command == "list":
        output: object = {"relays": load_relays()}
    elif args.command == "add":
        output = add_relay(
            args.source_bus, args.destination, relay_id=args.relay_id, start=args.start,
            mailbox_root=mailbox_root, dry_run=args.dry_run,
        )
    elif args.command == "del":
        output = delete_relay(args.relay_id, dry_run=args.dry_run)
    else:
        output = {"results": pump_relays(
            mailbox_root=mailbox_root,
            relay_ids=set(args.relay_ids) if args.relay_ids else None,
            limit=args.limit,
        )}
    if args.json or isinstance(output, dict):
        print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
