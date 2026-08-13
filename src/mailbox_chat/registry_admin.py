"""Inspect and update the durable UUID/identifier registry."""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import agent_mailbox
from .identifier_directory import IdentifierDirectory


def _request(base: str, method: str, path: str, *, token: str = "",
             payload: dict[str, Any] | None = None) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(
        f"{base.rstrip('/')}{path}", data=body, headers=headers, method=method,
    ), timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mailbox-client registry",
        description="Manage durable system-scoped UUID and identifier-to-text mappings",
    )
    transport = result.add_mutually_exclusive_group()
    transport.add_argument("--dir", type=Path, help="local mailbox directory")
    transport.add_argument("--url", help="relay HTTP base URL")
    result.add_argument("--token", help="REST Bearer token (or AGENT_MAILBOX_TOKEN)")
    commands = result.add_subparsers(dest="command", required=True)
    find = commands.add_parser("find", help="find saved identifier mappings")
    find.add_argument("--system", default="", help="source system")
    find.add_argument("--identifier", default="", help="UUID or opaque platform identifier")
    find.add_argument("--text", default="", help="readable label")
    find.add_argument("--kind", default="", help="user, channel, group, thread, or other kind")
    find.add_argument("--limit", type=int, default=100, help="maximum results")
    remember = commands.add_parser("remember", help="save an identifier-to-text mapping")
    remember.add_argument("system", help="source system")
    remember.add_argument("identifier", help="UUID or opaque platform identifier")
    remember.add_argument("text", help="readable label")
    remember.add_argument("--kind", default="", help="identifier kind")
    request = commands.add_parser("request", help="request identifier resolution once per system")
    request.add_argument("system", help="source system")
    request.add_argument("identifier", help="UUID or opaque platform identifier")
    request.add_argument("--resolver", required=True, help="platform resolver operation")
    request.add_argument("--force", action="store_true", help="repeat an existing request")
    requests = commands.add_parser("requests", help="list resolution requests")
    requests.add_argument("--system", default="", help="source system")
    requests.add_argument("--identifier", default="", help="UUID or opaque platform identifier")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    token = args.token or os.environ.get(agent_mailbox.MAILBOX_TOKEN_ENV, "")
    directory = IdentifierDirectory((args.dir or agent_mailbox.mailbox_dir()).expanduser().resolve())
    if args.command == "find":
        query = urllib.parse.urlencode({
            "system": args.system, "identifier": args.identifier, "text": args.text,
            "kind": args.kind, "limit": args.limit,
        })
        result = (_request(args.url, "GET", f"/v1/identifiers?{query}", token=token)
                  if args.url else {"identifiers": directory.find(
                      system=args.system, identifier=args.identifier, text=args.text,
                      kind=args.kind, limit=args.limit,
                  )})
    elif args.command == "remember":
        entry = {"system": args.system, "identifier": args.identifier,
                 "text": args.text, "kind": args.kind}
        result = (_request(args.url, "POST", "/v1/identifiers", token=token, payload=entry)
                  if args.url else {"identifiers": [directory.remember(**entry)]})
    elif args.command == "request":
        payload = {"system": args.system, "identifier": args.identifier,
                   "resolver": args.resolver, "force": args.force}
        result = (_request(args.url, "POST", "/v1/identifier-resolution-requests",
                           token=token, payload=payload)
                  if args.url else {"request": directory.request_resolution(**payload)})
    else:
        query = urllib.parse.urlencode({"system": args.system, "identifier": args.identifier})
        result = (_request(args.url, "GET", f"/v1/identifier-resolution-requests?{query}", token=token)
                  if args.url else {"requests": directory.resolution_requests(
                      system=args.system, identifier=args.identifier,
                  )})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
