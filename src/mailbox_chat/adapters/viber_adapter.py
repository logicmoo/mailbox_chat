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
from ..channel_routes import dispatch_routes
from ..delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from ..endpoint_address import subscription_recipients
from ..identifier_directory import IdentifierDirectory
from ..listener_registry import listeners_for


VIBER_API = "https://chatapi.viber.com/pa"
VIBER_MAX_FILE_BYTES = 50 * 1024 * 1024


class ViberAdapter:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.listeners: list[dict[str, Any]] = []
        self.status = {"enabled": False, "connected": False, "lastError": None, "bots": []}

    @staticmethod
    def _token(listener: dict[str, Any]) -> str:
        return os.environ.get(str(listener.get("token_env") or "VIBER_AUTH_TOKEN"), "").strip()

    def configure(self) -> bool:
        self.listeners = listeners_for("viber")
        missing = [item["id"] for item in self.listeners if not self._token(item)]
        self.status.update({
            "enabled": bool(self.listeners) and not missing,
            "connected": False,
            "bots": [item["id"] for item in self.listeners],
            "lastError": f"Missing Viber tokens for: {', '.join(missing)}" if missing else None,
        })
        return bool(self.status["enabled"])

    def close(self) -> None:
        self.status["connected"] = False

    def cycle(self, _mailbox: Any) -> None:
        if self.status["enabled"]:
            self.status.update({"connected": True, "lastError": None, "lastCycleAt": time.time()})

    def authenticate_webhook(self, body: bytes, signature: str) -> dict[str, Any] | None:
        for listener in self.listeners:
            token = self._token(listener)
            if not token:
                continue
            expected = hmac.new(token.encode("utf-8"), body, hashlib.sha256).hexdigest()
            if signature and hmac.compare_digest(signature, expected):
                return listener
        return None

    def handle_webhook(self, payload: dict[str, Any], mailbox: Any,
                       listener: dict[str, Any]) -> None:
        if payload.get("event") != "message" or listener["direction"] not in {"inbound", "bidirectional"}:
            return
        sender = payload.get("sender") or {}
        sender_id = str(sender.get("id") or "")
        source_id = str(payload.get("message_token") or "")
        if not sender_id or not source_id:
            raise ValueError("Viber message callback requires sender.id and message_token")
        allowed = set(str(item) for item in listener.get("channel_ids", []))
        if sender_id not in allowed and not listener.get("include_direct_messages"):
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
                             listener_id=listener["id"], source_id=source_id, channel_id=sender_id)
        if not DeliveryLedger(mailbox.mailbox_dir()).claim(origin, endpoint_id(
            "viber", listener_id=listener["id"], channel_id=sender_id,
        )):
            return
        recipients = list(dict.fromkeys([listener.get("bridge_agent"),
                                         *listener.get("mailbox_recipients", []),
                                         *subscription_recipients("viber", listener, sender_id)]))
        for recipient in filter(None, recipients):
            mailbox.send(recipient, text, sender=f"viber:{name}", message_type="viber_message",
                         channel_id=sender_id, channel_type="viber", source_id=source_id,
                         extra_fields={**origin, "viber_message": message})
        dispatch_routes(mailbox, listener_id=listener["id"], channel_id=sender_id,
                        message={**origin, "text": text, "source_id": source_id})

    def _listener_for(self, message: dict[str, Any]) -> dict[str, Any]:
        listener_id = str(message.get("listener_id") or "")
        listener = next((item for item in self.listeners if item["id"] == listener_id), None)
        if listener is None:
            eligible = [item for item in self.listeners if item["direction"] in {"outbound", "bidirectional"}]
            listener = eligible[0] if len(eligible) == 1 else None
        if listener is None:
            raise ValueError("Viber outbound message requires an unambiguous listener_id")
        return listener

    def _post(self, listener: dict[str, Any], payload: dict[str, Any]) -> None:
        response = self.session.post(f"{VIBER_API}/send_message", headers={
            "X-Viber-Auth-Token": self._token(listener), "Content-Type": "application/json",
        }, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        if int(result.get("status", 0)) != 0:
            raise ValueError(f"Viber send failed: {result.get('status_message') or result.get('status')}")

    def send_message(self, message: dict[str, Any]) -> None:
        listener = self._listener_for(message)
        receiver = str(message.get("channel_id") or "").strip()
        if not receiver:
            raise ValueError("Viber outbound message requires channel_id (Viber user ID)")
        sender = {"name": str(listener.get("bot_name") or "Mailbox Relay")[:28]}
        text = str(message.get("text") or "")
        for start in range(0, len(text), 7000):
            self._post(listener, {"receiver": receiver, "type": "text",
                                  "text": text[start:start + 7000], "sender": sender})
        for record in message.get("attachments") or []:
            path = Path(str(record.get("path") or "")).expanduser().resolve(strict=True)
            size = path.stat().st_size
            if size > VIBER_MAX_FILE_BYTES:
                raise ValueError(f"Viber files may not exceed {VIBER_MAX_FILE_BYTES} bytes")
            self._post(listener, {
                "receiver": receiver, "type": "file", "media": attachment_url(record),
                "size": size, "file_name": path.name[:256], "sender": sender,
            })
