"""Rakuten Viber Bot API webhook and outbound mailbox adapter."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from pathlib import Path
from typing import Any

import requests

from ..attachment_gateway import attachment_url
from ..delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from ..endpoint_address import subscription_recipients
from ..identifier_directory import IdentifierDirectory
from ..connector_registry import connectors_for


VIBER_API = "https://chatapi.viber.com/pa"
VIBER_MAX_FILE_BYTES = 50 * 1024 * 1024


class ViberAdapter:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.connectors: list[dict[str, Any]] = []
        self.status = {"enabled": False, "connected": False, "lastError": None, "bots": []}

    @staticmethod
    def _token(connector: dict[str, Any]) -> str:
        return os.environ.get(str(connector.get("token_env") or "VIBER_AUTH_TOKEN"), "").strip()

    def configure(self) -> bool:
        self.connectors = connectors_for("viber")
        missing = [item["id"] for item in self.connectors if not self._token(item)]
        self.status.update({
            "enabled": bool(self.connectors) and not missing,
            "connected": False,
            "bots": [item["id"] for item in self.connectors],
            "lastError": f"Missing Viber tokens for: {', '.join(missing)}" if missing else None,
        })
        return bool(self.status["enabled"])

    def close(self) -> None:
        self.status["connected"] = False

    def cycle(self, _mailbox: Any) -> None:
        if self.status["enabled"]:
            self.status.update({"connected": True, "lastError": None, "lastCycleAt": time.time()})

    def authenticate_webhook(self, body: bytes, signature: str) -> dict[str, Any] | None:
        for connector in self.connectors:
            token = self._token(connector)
            if not token:
                continue
            expected = hmac.new(token.encode("utf-8"), body, hashlib.sha256).hexdigest()
            if signature and hmac.compare_digest(signature, expected):
                return connector
        return None

    def handle_webhook(self, payload: dict[str, Any], mailbox: Any,
                       connector: dict[str, Any]) -> None:
        if payload.get("event") != "message" or connector["direction"] not in {"inbound", "bidirectional"}:
            return
        sender = payload.get("sender") or {}
        sender_id = str(sender.get("id") or "")
        source_id = str(payload.get("message_token") or "")
        if not sender_id or not source_id:
            raise ValueError("Viber message callback requires sender.id and message_token")
        allowed = set(str(item) for item in connector.get("channel_ids", []))
        if sender_id not in allowed and not connector.get("include_direct_messages"):
            return
        name = str(sender.get("name") or sender_id).strip()
        IdentifierDirectory(mailbox.mailbox_dir()).remember(
            sender_id, name, system="viber", kind="user",
            metadata={key: sender[key] for key in ("country", "language", "avatar") if sender.get(key)},
        )
        message = payload.get("message") or {}
        message_type = str(message.get("type") or "message")
        text = str(message.get("text") or "")
        if not text:
            text = f"[{message_type}]" + (f" {message['media']}" if message.get("media") else "")
        origin = with_origin({"author": name, "author_id": sender_id}, adapter="viber",
                             connector_id=connector["id"], source_id=source_id, channel_id=sender_id)
        if not DeliveryLedger(mailbox.mailbox_dir()).claim(origin, endpoint_id(
            "viber", connector_id=connector["id"], channel_id=sender_id,
        )):
            return
        recipients = list(dict.fromkeys([connector.get("bridge_agent"),
                                         *connector.get("mailbox_recipients", []),
                                         *subscription_recipients("viber", connector, sender_id)]))
        for recipient in filter(None, recipients):
            mailbox.send(recipient, text, sender=f"viber:{name}", message_type="viber_message",
                         channel_id=sender_id, channel_type="viber", source_id=source_id,
                         extra_fields={**origin, "viber_message": message})

    def _connector_for(self, message: dict[str, Any]) -> dict[str, Any]:
        connector_id = str(message.get("connector_id") or "")
        connector = next((item for item in self.connectors if item["id"] == connector_id), None)
        if connector is None:
            eligible = [item for item in self.connectors if item["direction"] in {"outbound", "bidirectional"}]
            connector = eligible[0] if len(eligible) == 1 else None
        if connector is None:
            raise ValueError("Viber outbound message requires an unambiguous connector_id")
        return connector

    def _post(self, connector: dict[str, Any], payload: dict[str, Any]) -> None:
        response = self.session.post(f"{VIBER_API}/send_message", headers={
            "X-Viber-Auth-Token": self._token(connector), "Content-Type": "application/json",
        }, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        if int(result.get("status", 0)) != 0:
            raise ValueError(f"Viber send failed: {result.get('status_message') or result.get('status')}")

    def send_message(self, message: dict[str, Any]) -> None:
        connector = self._connector_for(message)
        receiver = str(message.get("channel_id") or "").strip()
        if not receiver:
            raise ValueError("Viber outbound message requires channel_id (Viber user ID)")
        sender = {"name": str(connector.get("bot_name") or "Mailbox Relay")[:28]}
        text = str(message.get("text") or "")
        for start in range(0, len(text), 7000):
            self._post(connector, {"receiver": receiver, "type": "text",
                                  "text": text[start:start + 7000], "sender": sender})
        for record in message.get("attachments") or []:
            path = Path(str(record.get("path") or "")).expanduser().resolve(strict=True)
            size = path.stat().st_size
            if size > VIBER_MAX_FILE_BYTES:
                raise ValueError(f"Viber files may not exceed {VIBER_MAX_FILE_BYTES} bytes")
            self._post(connector, {
                "receiver": receiver, "type": "file", "media": attachment_url(record),
                "size": size, "file_name": path.name[:256], "sender": sender,
            })
