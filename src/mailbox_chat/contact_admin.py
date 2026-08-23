"""Import and inspect relay contact identifiers."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

from . import agent_mailbox
from .identifier_directory import IdentifierDirectory


def _vcard(path: Path, system: str, kind: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8-sig").replace("\r\n ", "").splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        field = key.split(";", 1)[0].upper()
        if field == "BEGIN":
            current = {}
        elif field == "END" and value.upper() == "VCARD":
            name = (current.get("FN") or current.get("N") or [""])[0].replace(";", " ").strip()
            identifiers = current.get("TEL", []) or current.get("EMAIL", []) or current.get("UID", [])
            for identifier in identifiers:
                canonical = re.sub(r"[^0-9]", "", identifier) if system == "whatsapp" else identifier.strip()
                if canonical and name:
                    entries.append({"system": system, "identifier": canonical, "text": name, "kind": kind})
        else:
            current.setdefault(field, []).append(value.strip())
    return entries


def load_contacts(path: Path, *, system: str, kind: str = "user") -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix in {".vcf", ".vcard"}:
        return _vcard(path, system, kind)
    if suffix == ".csv":
        raw: Any = list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
    else:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            raw = raw.get("contacts") or raw.get("entries") or [raw]
    if not isinstance(raw, list):
        raise ValueError("contact file must contain a list of contacts")
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each contact must be an object or CSV row")
        identifier = str(item.get("identifier") or item.get("phone") or item.get("phone_number")
                         or item.get("id") or "").strip()
        text = str(item.get("text") or item.get("name") or item.get("display_name") or "").strip()
        source_system = str(item.get("system") or system).strip().lower()
        if source_system == "whatsapp":
            identifier = re.sub(r"[^0-9]", "", identifier)
        if not identifier or not text:
            raise ValueError("every contact requires an identifier/phone and text/name")
        entries.append({"system": source_system, "identifier": identifier, "text": text,
                        "kind": str(item.get("kind") or kind),
                        "metadata": {key: value for key, value in item.items() if key not in {
                            "system", "identifier", "phone", "phone_number", "id", "text", "name",
                            "display_name", "kind",
                        }}})
    return entries


def _request(url: str, method: str, *, token: str = "", payload: Any = None) -> Any:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    if body is not None:
        headers["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method=method),
                                timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="mailbox-client contacts",
                                     description="Import or list durable relay contact identifiers")
    transport = result.add_mutually_exclusive_group()
    transport.add_argument("--dir", type=Path, help="local mailbox directory")
    transport.add_argument("--url", help="relay HTTP base URL")
    result.add_argument("--token", help="REST Bearer token (or AGENT_MAILBOX_TOKEN)")
    commands = result.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="import JSON, CSV, vCard/VCF contacts")
    importer.add_argument("file", type=Path, help="contact file to import")
    importer.add_argument("--system", required=True, help="source system, such as whatsapp")
    importer.add_argument("--kind", default="user", help="identifier kind (default: user)")
    listing = commands.add_parser("list", help="list imported contacts")
    listing.add_argument("--system", default="", help="filter by source system")
    listing.add_argument("--limit", type=int, default=100, help="maximum contacts (default: 100)")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    token = args.token or os.environ.get(agent_mailbox.MAILBOX_TOKEN_ENV, "")
    root = (args.dir or agent_mailbox.mailbox_dir()).expanduser().resolve()
    if args.command == "import":
        entries = load_contacts(args.file.expanduser().resolve(strict=True), system=args.system, kind=args.kind)
        result = (_request(f"{args.url.rstrip('/')}/v1/identifiers", "POST", token=token,
                           payload={"entries": entries})["identifiers"] if args.url
                  else IdentifierDirectory(root).remember_many(entries))
        print(json.dumps({"imported": len(result), "contacts": result}, ensure_ascii=False, indent=2))
        return 0
    result = (_request(f"{args.url.rstrip('/')}/v1/identifiers?system={args.system}&limit={args.limit}",
                       "GET", token=token)["identifiers"] if args.url
              else IdentifierDirectory(root).find(system=args.system, limit=args.limit))
    print(json.dumps({"contacts": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
