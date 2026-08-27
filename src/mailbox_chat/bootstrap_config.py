"""Bootstrap runtime registries by scanning all JSON files in mailbox/config."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import mailbox_store
from .endpoint_address import endpoint_instance, mailbox_resource_id


BOOTSTRAP_CONFIG_DIR_ENV = "MAILBOX_BOOTSTRAP_CONFIG_DIR"
_SAFE_FILE = re.compile(r"[^A-Za-z0-9_.-]")


def _clean_name(value: str) -> str:
    clean = _SAFE_FILE.sub("_", str(value).strip())
    return clean or "mailbox"


def _resolve_path(path_value: str, *, base_dir: Path) -> Path:
    target = Path(path_value).expanduser()
    return target if target.is_absolute() else (base_dir / target).resolve()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _agent_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        payload = {
            key: value for key, value in record.items()
            if key not in {"id", "file", "description"}
        }
    normalized = dict(payload)
    normalized.setdefault("kind", "agent")
    if record.get("description") and "description" not in normalized:
        normalized["description"] = str(record["description"])
    return normalized


def _expand_connector_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = dict(record)
    payload.setdefault("kind", "connector")
    payload.setdefault("direction", "bidirectional")
    mailbox_ids: list[str] = []
    recipients: list[str] = []
    for value in payload.get("mailbox_ids") or []:
        text = str(value).strip()
        if text:
            mailbox_ids.append(text)
    for instance in payload.get("instances") or []:
        if not isinstance(instance, dict):
            continue
        for field in ("mailbox_ids", "mailbox_types"):
            for value in instance.get(field) or []:
                text = str(value).strip()
                if text:
                    mailbox_ids.append(text)
        for value in instance.get("mailbox_recipients") or []:
            text = str(value).strip()
            if text:
                recipients.append(text)
    if mailbox_ids:
        payload["mailbox_ids"] = list(dict.fromkeys(mailbox_ids))
    if recipients and not payload.get("mailbox_recipients"):
        payload["mailbox_recipients"] = list(dict.fromkeys(recipients))
    return payload


def _instance_identifier(instance: dict[str, Any]) -> str:
    for field in ("name", "nickname", "id", "presence_id"):
        value = str(instance.get(field) or "").strip()
        if value:
            return f"{field}:{value}"
    return json.dumps(instance, sort_keys=True, ensure_ascii=False)


def _resolve_instance_inheritance(instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    for instance in instances:
        for field in ("name", "nickname"):
            key = str(instance.get(field) or "").strip()
            if key and key not in by_name:
                by_name[key] = instance

    resolved_cache: dict[str, dict[str, Any]] = {}

    def resolve(instance: dict[str, Any], trail: set[str]) -> dict[str, Any]:
        identity = _instance_identifier(instance)
        if identity in resolved_cache:
            return dict(resolved_cache[identity])
        parent_name = str(instance.get("inherit-from") or instance.get("inherits_from") or "").strip()
        if not parent_name:
            resolved_cache[identity] = dict(instance)
            return dict(instance)
        if parent_name in trail:
            resolved_cache[identity] = dict(instance)
            return dict(instance)
        parent = by_name.get(parent_name)
        if parent is None:
            resolved_cache[identity] = dict(instance)
            return dict(instance)
        merged = {**resolve(parent, trail | {parent_name}), **instance}
        resolved_cache[identity] = merged
        return dict(merged)

    return [resolve(instance, set()) for instance in instances]


def _merge_connector_payload(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key in {"instances", "mailbox_ids", "mailbox_recipients"}:
            continue
        if value is None:
            continue
        if isinstance(value, str):
            if not value.strip() and str(merged.get(key) or "").strip():
                continue
            merged[key] = value.strip()
        else:
            merged[key] = value

    token_envs = [
        *(str(value).strip() for value in _as_list(existing.get("token_env_fallbacks")) if str(value).strip()),
        str(existing.get("token_env") or "").strip(),
        str(incoming.get("token_env") or "").strip(),
        *(str(value).strip() for value in _as_list(incoming.get("token_env_fallbacks")) if str(value).strip()),
    ]
    token_envs = [value for value in dict.fromkeys(token_envs) if value]
    if token_envs:
        preferred_token_env = (
            str(incoming.get("token_env") or "").strip()
            or str(merged.get("token_env") or "").strip()
            or token_envs[0]
        )
        merged["token_env"] = preferred_token_env
        merged["token_env_fallbacks"] = token_envs

    merged["mailbox_ids"] = list(dict.fromkeys([
        *(str(value).strip() for value in _as_list(existing.get("mailbox_ids")) if str(value).strip()),
        *(str(value).strip() for value in _as_list(incoming.get("mailbox_ids")) if str(value).strip()),
    ]))
    merged["mailbox_recipients"] = list(dict.fromkeys([
        *(str(value).strip() for value in _as_list(existing.get("mailbox_recipients")) if str(value).strip()),
        *(str(value).strip() for value in _as_list(incoming.get("mailbox_recipients")) if str(value).strip()),
    ]))

    ordered_keys: list[str] = []
    by_key: dict[str, dict[str, Any]] = {}
    for instance in [*(_as_list(existing.get("instances"))), *(_as_list(incoming.get("instances")))]:
        if not isinstance(instance, dict):
            continue
        key = _instance_identifier(instance)
        if key not in by_key:
            ordered_keys.append(key)
            by_key[key] = dict(instance)
        else:
            by_key[key] = {**by_key[key], **instance}
    if ordered_keys:
        merged["instances"] = _resolve_instance_inheritance([by_key[key] for key in ordered_keys])
    return merged


def _looks_like_agent(record: dict[str, Any]) -> bool:
    agent_id = str(record.get("id") or "").strip()
    payload = _as_dict(record.get("payload"))
    return bool(agent_id and (
        str(payload.get("kind") or "").strip().lower() == "agent"
        or bool(payload.get("agent_kind"))
        or bool(payload.get("agent_roles"))
        or bool(record.get("agent_kind"))
        or bool(record.get("agent_roles"))
    ))


def _looks_like_connector(record: dict[str, Any]) -> bool:
    if "entries" in record and "ordinal" in record:
        return False
    return bool(
        record.get("adapter")
        or str(record.get("kind") or "").strip().lower() == "connector"
        or record.get("base_url")
        or record.get("server")
        or record.get("token_env")
        or record.get("instances")
    )


def _endpoint_host(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        return (urlsplit(text).hostname or "").strip().lower()
    host = text.split("/", 1)[0].strip().lower()
    if ":" in host:
        host = host.split(":", 1)[0]
    return host


def _derive_connector_id(record: dict[str, Any]) -> str:
    explicit = str(record.get("id") or "").strip()
    if explicit:
        return explicit
    adapter = str(record.get("adapter") or "").strip().lower()
    if not adapter:
        return ""
    host = _endpoint_host(
        str(
            record.get("base_url")
            or record.get("endpoint")
            or record.get("server")
            or record.get("instance")
            or "",
        )
    )
    if host:
        return f"{adapter}-{_clean_name(host)}"
    return f"{adapter}-default"


def _connector_signature(record: dict[str, Any]) -> tuple[str, str, str]:
    adapter = str(record.get("adapter") or "").strip().lower()
    host = _endpoint_host(
        str(
            record.get("base_url")
            or record.get("endpoint")
            or record.get("server")
            or record.get("instance")
            or "",
        )
    )
    discriminator = ""
    if adapter == "irc":
        discriminator = str(record.get("nickname") or "").strip().casefold()
    elif adapter == "mattermost":
        discriminator = str(record.get("token_env") or "").strip()
    return adapter, host, discriminator


def _looks_like_mailbox(record: dict[str, Any]) -> bool:
    mailbox_id = str(record.get("id") or "").strip()
    if not mailbox_id:
        return False
    if "entries" in record and "ordinal" in record:
        return False
    payload = _as_dict(record.get("payload"))
    return bool(
        record.get("mailbox_type")
        or str(record.get("kind") or "").strip().lower() == "mailbox"
        or str(payload.get("kind") or "").strip().lower() == "mailbox"
        or payload.get("subscribers") is not None
        or payload.get("aliases") is not None
    )


def _ensure_message_placeholders(
    mailbox_root: Path,
    *,
    mailbox_ids: set[str],
    agent_mailboxes: set[str],
) -> int:
    messages_dir = mailbox_root / "messages"
    messages_dir.mkdir(parents=True, exist_ok=True)
    created = 0
    for mailbox_id in sorted({*mailbox_ids, *agent_mailboxes}):
        target = messages_dir / f"{_clean_name(mailbox_id)}.json"
        if target.exists():
            continue
        target.write_text("[]\n", encoding="utf-8", newline="\n")
        created += 1
    return created


def _ensure_resolver_placeholders(mailbox_root: Path, connector_ids: set[str]) -> int:
    resolver_dir = mailbox_root / "resolver"
    resolver_dir.mkdir(parents=True, exist_ok=True)
    created = 0
    for connector_id in sorted(connector_ids):
        path = resolver_dir / f"resolver_{_clean_name(connector_id)}.json"
        if path.exists():
            continue
        path.write_text(
            json.dumps(
                {"id": connector_id, "kind": "resolver_cache", "entries": []},
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        created += 1
    return created


def bootstrap_mailbox_state(
    mailbox_root: Path,
    *,
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """Populate runtime registries from any recognized JSON config records."""
    root = Path(mailbox_root).expanduser().resolve()
    configured = (
        Path(os.environ.get(BOOTSTRAP_CONFIG_DIR_ENV, "")).expanduser().resolve()
        if os.environ.get(BOOTSTRAP_CONFIG_DIR_ENV, "").strip()
        else None
    )
    config_root = (config_dir or configured or (root / "config")).expanduser().resolve()

    root.mkdir(parents=True, exist_ok=True)
    (root / "runtime").mkdir(parents=True, exist_ok=True)
    (root / "messages").mkdir(parents=True, exist_ok=True)
    (root / "resolver").mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "config_root": str(config_root),
        "scanned_files": 0,
        "agents_upserted": 0,
        "connectors_upserted": 0,
        "mailboxes_upserted": 0,
        "mailbox_files_created": 0,
        "message_files_created": 0,
        "resolver_files_created": 0,
        "missing_files": [],
    }
    if not config_root.exists():
        report["missing_files"] = [str(config_root)]
        return report

    upserted_agents: set[str] = set()
    upserted_connectors: set[str] = set()
    upserted_mailboxes: set[str] = set()
    connector_ids: set[str] = set()
    mailbox_ids: set[str] = set()
    agent_mailboxes: set[str] = set()

    def ensure_mailbox_record(
        mailbox_id: str,
        *,
        mailbox_type: str,
        declared_by: str,
    ) -> None:
        mailbox_id = str(mailbox_id).strip()
        if not mailbox_id:
            return
        existing = mailbox_store.get_entry(root, mailbox_store.MAILBOXES_REGISTRY, mailbox_id)
        if existing is None:
            mailbox_store.put_entry(
                root,
                mailbox_store.MAILBOXES_REGISTRY,
                mailbox_id,
                {
                    "kind": "mailbox",
                    "subscribers": [],
                    "mailbox_type": mailbox_type,
                    "declared_by": declared_by,
                },
            )
        upserted_mailboxes.add(mailbox_id)
        mailbox_ids.add(mailbox_id)
        mailbox_path = mailbox_store.mailbox_path(root, mailbox_id)
        if not mailbox_path.exists():
            mailbox_store.save_mailbox(root, mailbox_store.load_mailbox(root, mailbox_id))
            report["mailbox_files_created"] += 1

    def upsert_agent(record: dict[str, Any], *, source: str) -> str:
        if not _looks_like_agent(record):
            return ""
        agent_id = str(record.get("id") or "").strip()
        if not agent_id:
            raise ValueError(f"{source}: agent entry missing id")
        payload = _agent_payload(record)
        payload["agent_id"] = str(payload.get("agent_id") or agent_id).strip() or agent_id
        mailbox_store.put_entry(root, mailbox_store.AGENTS_REGISTRY, agent_id, payload)
        upserted_agents.add(agent_id)
        mailbox_name = str(payload.get("mailbox") or "").strip()
        if mailbox_name:
            agent_mailboxes.add(mailbox_name)
            ensure_mailbox_record(
                mailbox_name,
                mailbox_type=("system_builtin_mailboxes"
                              if str(payload.get("agent_kind") or "") == "role_agent"
                              else "system_mailboxes"),
                declared_by=payload["agent_id"],
            )
        for field in ("writes_to_mailboxes", "required_mailboxes"):
            for value in _as_list(payload.get(field)):
                declared_mailbox = str(value).strip()
                if not declared_mailbox:
                    continue
                ensure_mailbox_record(
                    declared_mailbox,
                    mailbox_type=("system_builtin_mailboxes"
                                  if str(payload.get("agent_kind") or "") == "role_agent"
                                  else "system_mailboxes"),
                    declared_by=payload["agent_id"],
                )
        return payload["agent_id"]

    def upsert_connector(
        record: dict[str, Any],
        *,
        source: str,
        declared_by_agent: str = "",
    ) -> bool:
        if not _looks_like_connector(record):
            return False
        connector_id = _derive_connector_id(record)
        if not connector_id:
            raise ValueError(f"{source}: connector entry needs adapter and endpoint/server or id")
        payload = _expand_connector_payload(record)
        existing = mailbox_store.get_entry(root, mailbox_store.CONNECTORS_REGISTRY, connector_id)
        existing_payload = _as_dict((existing or {}).get("payload"))
        merged_payload = _merge_connector_payload(existing_payload, payload)
        if declared_by_agent and not str(merged_payload.get("bridge_agent") or "").strip():
            merged_payload["bridge_agent"] = declared_by_agent
        merged_payload["id"] = connector_id
        mailbox_store.put_entry(root, mailbox_store.CONNECTORS_REGISTRY, connector_id, merged_payload)
        explicit_id = str(record.get("id") or "").strip()
        if explicit_id:
            derived_payload = dict(merged_payload)
            derived_payload.pop("id", None)
            legacy_id = _derive_connector_id(derived_payload)
            if legacy_id and legacy_id != connector_id:
                legacy_entry = mailbox_store.get_entry(root, mailbox_store.CONNECTORS_REGISTRY, legacy_id)
                legacy_payload = _as_dict((legacy_entry or {}).get("payload"))
                if legacy_payload and _connector_signature(legacy_payload) == _connector_signature(merged_payload):
                    mailbox_store.delete_entry(root, mailbox_store.CONNECTORS_REGISTRY, legacy_id)
        upserted_connectors.add(connector_id)
        connector_ids.add(connector_id)
        adapter = str(merged_payload.get("adapter") or "").strip().lower()
        instance = endpoint_instance(adapter, merged_payload) if adapter else ""
        for mailbox_id in _as_list(merged_payload.get("mailbox_ids")):
            declared_mailbox = str(mailbox_id).strip()
            if not declared_mailbox or declared_mailbox.startswith("$"):
                continue
            ensure_mailbox_record(
                declared_mailbox,
                mailbox_type="system_mailboxes",
                declared_by=connector_id,
            )
            if adapter and instance:
                canonical_mailbox = mailbox_resource_id(adapter, instance, declared_mailbox)
                if canonical_mailbox and canonical_mailbox != declared_mailbox:
                    ensure_mailbox_record(
                        canonical_mailbox,
                        mailbox_type="system_mailboxes",
                        declared_by=f"{connector_id}:canonical",
                    )
        return True

    def upsert_mailbox(record: dict[str, Any], *, source: str) -> bool:
        if not _looks_like_mailbox(record):
            return False
        mailbox_id = str(record.get("id") or "").strip()
        if not mailbox_id:
            raise ValueError(f"{source}: mailbox entry missing id")
        payload = _as_dict(record.get("payload"))
        payload.setdefault("kind", "mailbox")
        payload.setdefault("subscribers", [])
        if record.get("mailbox_type") and "mailbox_type" not in payload:
            payload["mailbox_type"] = str(record.get("mailbox_type"))
        if record.get("description") and "description" not in payload:
            payload["description"] = str(record["description"])
        mailbox_store.put_entry(root, mailbox_store.MAILBOXES_REGISTRY, mailbox_id, payload)
        upserted_mailboxes.add(mailbox_id)
        mailbox_ids.add(mailbox_id)
        ensure_mailbox_record(
            mailbox_id,
            mailbox_type=str(payload.get("mailbox_type") or record.get("mailbox_type") or "system_mailboxes"),
            declared_by=source,
        )
        return True

    def process_candidate(record: dict[str, Any], *, source: str) -> None:
        declared_agent = upsert_agent(record, source=source)
        if declared_agent:
            payload = _as_dict(record.get("payload"))
            declared_adapter = str(payload.get("adapter") or record.get("adapter") or "").strip().lower()
            declared_endpoint = str(payload.get("endpoint") or "").strip()
            declared_server = str(payload.get("server") or "").strip()
            declared_token_env = str(payload.get("token_env") or "").strip()

            def with_agent_defaults(raw: dict[str, Any]) -> dict[str, Any]:
                seeded = dict(raw)
                if declared_adapter and not str(seeded.get("adapter") or "").strip():
                    seeded["adapter"] = declared_adapter
                has_endpoint = any(str(seeded.get(key) or "").strip() for key in (
                    "base_url", "endpoint", "server", "instance",
                ))
                if not has_endpoint:
                    if declared_server and declared_adapter == "irc":
                        seeded["server"] = declared_server
                    elif declared_endpoint:
                        if "://" in declared_endpoint and declared_adapter in {
                            "mattermost", "discord", "matrix", "slack", "discourse",
                        }:
                            seeded["base_url"] = declared_endpoint
                        else:
                            seeded["endpoint"] = declared_endpoint
                if declared_token_env and not str(seeded.get("token_env") or "").strip():
                    seeded["token_env"] = declared_token_env
                return seeded

            nested_connector = record.get("connector")
            if isinstance(nested_connector, dict):
                upsert_connector(
                    with_agent_defaults(nested_connector),
                    source=f"{source}:connector",
                    declared_by_agent=declared_agent,
                )
            for index, item in enumerate(_as_list(record.get("connectors"))):
                if isinstance(item, dict):
                    upsert_connector(
                        with_agent_defaults(item),
                        source=f"{source}:connectors[{index}]",
                        declared_by_agent=declared_agent,
                    )
            return
        if upsert_connector(record, source=source):
            return
        upsert_mailbox(record, source=source)

    def process_agent_group(items: list[Any], *, source_path: Path, group: str) -> None:
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"{source_path.name}:{group}[{index}] must be an object")
            loaded = item
            if item.get("file"):
                target = _resolve_path(str(item["file"]), base_dir=source_path.parent)
                loaded = _as_dict(_read_json(target))
                loaded = {
                    **loaded,
                    **({"id": item.get("id")} if not loaded.get("id") and item.get("id") else {}),
                    **({"description": item.get("description")}
                       if item.get("description") and not loaded.get("description") else {}),
                }
            process_candidate(loaded, source=f"{source_path.name}:{group}[{index}]")

    for path in sorted(config_root.glob("*.json")):
        report["scanned_files"] += 1
        document = _read_json(path)
        if isinstance(document, dict):
            if "system_agents" in document or "relay_agents" in document:
                process_agent_group(
                    _as_list(document.get("system_agents")),
                    source_path=path,
                    group="system_agents",
                )
                process_agent_group(
                    _as_list(document.get("relay_agents")),
                    source_path=path,
                    group="relay_agents",
                )
            if isinstance(document.get("mailboxes"), list):
                for index, item in enumerate(document["mailboxes"]):
                    if isinstance(item, dict):
                        process_candidate(item, source=f"{path.name}:mailboxes[{index}]")
            if isinstance(document.get("agents"), list):
                for index, item in enumerate(document["agents"]):
                    if isinstance(item, dict):
                        process_candidate(item, source=f"{path.name}:agents[{index}]")
            if isinstance(document.get("entries"), dict):
                for key, item in document["entries"].items():
                    if isinstance(item, dict):
                        process_candidate(item, source=f"{path.name}:entries[{key}]")
            if isinstance(document.get("connectors"), list):
                for index, item in enumerate(document["connectors"]):
                    if isinstance(item, dict):
                        process_candidate(item, source=f"{path.name}:connectors[{index}]")
            process_candidate(document, source=path.name)
        elif isinstance(document, list):
            for index, item in enumerate(document):
                if isinstance(item, dict):
                    process_candidate(item, source=f"{path.name}[{index}]")

    report["agents_upserted"] = len(upserted_agents)
    report["connectors_upserted"] = len(upserted_connectors)
    report["mailboxes_upserted"] = len(upserted_mailboxes)
    report["message_files_created"] = _ensure_message_placeholders(
        root,
        mailbox_ids=mailbox_ids,
        agent_mailboxes=agent_mailboxes,
    )
    report["resolver_files_created"] = _ensure_resolver_placeholders(root, connector_ids)
    return report


__all__ = ["BOOTSTRAP_CONFIG_DIR_ENV", "bootstrap_mailbox_state"]
