"""Parse stable external chat endpoint addresses."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote
from urllib.parse import urlsplit
from typing import Any


ADAPTER_ALIASES = {
    "mm": "mattermost",
    "discord": "discord",
    "slack": "slack",
    "matrix": "matrix",
    "irc": "irc",
    "telegram": "telegram",
    "wab": "whatsapp",
    "wa": "whatsapp_personal",
    "facebook": "facebook_messenger",
    "viber": "viber",
    "line": "line",
    "discourse": "discourse",
}
CANONICAL_TYPES = {value: key for key, value in ADAPTER_ALIASES.items()}
ADAPTER_ALIASES.update({
    "whatsapp-business": "whatsapp",
    "whatsapp-personal": "whatsapp_personal",
})


@dataclass(frozen=True)
class EndpointAddress:
    adapter: str
    instance: str
    identifier: str

    @property
    def canonical(self) -> str:
        alias = CANONICAL_TYPES[self.adapter]
        return f"{alias}/{self.instance}/{quote(self.identifier, safe='!:@.-_~')}"


def parse_endpoint(value: str) -> EndpointAddress | None:
    parts = value.strip().split("/", 2)
    if len(parts) != 3:
        return None
    adapter, instance, identifier = (item.strip() for item in parts)
    if not adapter or not instance or not identifier:
        raise ValueError("endpoint addresses require ADAPTER/INSTANCE/IDENTIFIER")
    adapter = ADAPTER_ALIASES.get(adapter.lower(), adapter.lower())
    if adapter not in CANONICAL_TYPES:
        raise ValueError(f"unsupported endpoint address adapter: {adapter}")
    return EndpointAddress(adapter, instance.lower(), unquote(identifier))


def endpoint_instance(adapter: str, listener: dict[str, Any]) -> str:
    """Derive a stable configured instance name, preferring an explicit value."""
    explicit = str(listener.get("instance") or "").strip()
    if explicit:
        return explicit.lower()
    candidates = {
        "discord": listener.get("id"),
        "slack": listener.get("workspace_id") or listener.get("id"),
        "matrix": listener.get("homeserver"),
        "irc": listener.get("server"),
        "telegram": listener.get("id"),
        "whatsapp": listener.get("phone_number_id"),
        "whatsapp_personal": listener.get("id") or "local",
        "facebook_messenger": listener.get("page_id"),
        "viber": listener.get("id"),
        "line": listener.get("id"),
        "discourse": listener.get("base_url"),
    }
    value = str(candidates.get(adapter) or listener.get("id") or adapter).strip()
    if "://" in value:
        value = urlsplit(value).hostname or value
    return value.lower()


def subscription_recipients(adapter: str, listener: dict[str, Any], identifier: str) -> list[str]:
    from .local_channels import subscribers

    specific = EndpointAddress(adapter, endpoint_instance(adapter, listener), str(identifier)).canonical
    default = EndpointAddress(adapter, "0", str(identifier)).canonical
    return list(dict.fromkeys([*subscribers(specific), *subscribers(default)]))
