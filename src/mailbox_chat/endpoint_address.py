"""Parse stable external chat endpoint addresses."""

from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import quote, unquote
from urllib.parse import urlsplit
from typing import Any


ADAPTER_ALIASES = {
    "local": "local",
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
RESOURCE_ID_PART = re.compile(r"[^a-zA-Z0-9_-]+")
# IRC mailbox prefixes are significant: #prolog and ##prolog are DIFFERENT mailboxs.
# The slug would otherwise strip both to "prolog". Encode the leading sigil run so distinct
# prefixes yield distinct ids. The common single "#" mailbox stays clean (no token);
# other/repeated sigils get a compact token prefix.
IRC_SIGIL_TOKENS = {"#": "h", "&": "s", "+": "p", "!": "x"}


def _encode_irc_identifier(identifier: str) -> str:
    text = str(identifier)
    index = 0
    while index < len(text) and text[index] in IRC_SIGIL_TOKENS:
        index += 1
    sigils, name = text[:index], text[index:]
    if not sigils:
        return text
    token = "".join(IRC_SIGIL_TOKENS[char] for char in sigils)
    return name if token == "h" else f"{token}-{name}"


def mailbox_resource_id(adapter: str, instance: str, identifier: str, *, scope: str = "") -> str:
    """Build a stable non-address mailbox resource ID.

    If the instance host's leading label duplicates the adapter slug or canonical
    name (e.g. ``irc.quakenet.org`` for ``irc``, or ``mm.snet`` for ``mm``), that
    label is popped so the id reads ``irc-quakenet-org-…`` / ``mm-snet-…`` instead
    of ``irc-irc-…`` / ``mm-mm-…``. A non-duplicating host (``chat.singularitynet.io``)
    is kept verbatim (``mm-chat-singularitynet-io-…``). IRC mailbox sigils are encoded so
    ``#prolog`` (=> ``…-prolog``) and ``##prolog`` (=> ``…-hh-prolog``) never collide.
    """
    canonical = ADAPTER_ALIASES.get(adapter, adapter)
    adapter_name = CANONICAL_TYPES.get(canonical, adapter)
    instance_part = str(instance).strip()
    lowered = instance_part.lower()
    for label in (adapter_name, canonical):
        if label and lowered.startswith(f"{label}."):
            instance_part = instance_part[len(label) + 1:]
            break
    if canonical == "irc":
        identifier = _encode_irc_identifier(identifier)
    parts = [adapter_name, instance_part, scope, identifier]
    return "-".join(
        RESOURCE_ID_PART.sub("-", str(part).strip()).strip("-").lower()
        for part in parts if str(part).strip()
    )


@dataclass(frozen=True)
class EndpointAddress:
    adapter: str
    instance: str
    identifier: str

    @property
    def canonical(self) -> str:
        alias = CANONICAL_TYPES[self.adapter]
        return f"{alias}/{self.instance}/{quote(self.identifier, safe='!:@.-_~')}"

    @property
    def inferred_type(self) -> str:
        if self.adapter == "local":
            return "mailbox"
        if self.adapter == "irc":
            if self.identifier == "status":
                return "status"
            return "mailbox" if self.identifier.startswith(("#", "&", "+", "!")) else "user"
        if self.adapter == "mattermost":
            # Mattermost has no id-level sigil, but its markdown addresses users as @name and
            # mailboxs as ~name. Honor that: @name => user (DM), ~name or bare => mailbox.
            return "user" if self.identifier.startswith("@") else "mailbox"
        return "unknown"

    def describe(self, *, resource_type: str = "", properties: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "address": self.canonical,
            "platform": CANONICAL_TYPES[self.adapter],
            "adapter": self.adapter,
            "instance": self.instance,
            "id": self.identifier,
            "type": resource_type or self.inferred_type,
            "properties": dict(properties or {}),
        }


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


def endpoint_instance(adapter: str, connector: dict[str, Any]) -> str:
    """Derive a stable configured instance name, preferring an explicit value."""
    explicit = str(connector.get("instance") or "").strip()
    if explicit:
        return explicit.lower()
    candidates = {
        "local": "0",
        "discord": connector.get("id"),
        "slack": connector.get("workspace_id") or connector.get("id"),
        "matrix": connector.get("homeserver"),
        "mattermost": connector.get("base_url"),
        "irc": connector.get("server"),
        "telegram": connector.get("id"),
        "whatsapp": connector.get("phone_number_id"),
        "whatsapp_personal": connector.get("id") or "local",
        "facebook_messenger": connector.get("page_id"),
        "viber": connector.get("id"),
        "line": connector.get("id"),
        "discourse": connector.get("base_url"),
    }
    value = str(candidates.get(adapter) or connector.get("id") or adapter).strip()
    if "://" in value:
        value = urlsplit(value).hostname or value
    return value.lower()


def subscription_recipients(adapter: str, connector: dict[str, Any], identifier: str) -> list[str]:
    from .subscriptions import subscribers

    return subscribers(mailbox_resource_id(
        adapter, endpoint_instance(adapter, connector), str(identifier),
    ))
