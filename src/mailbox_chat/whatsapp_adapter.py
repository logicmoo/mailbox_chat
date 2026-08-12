"""WhatsApp Business Cloud API webhook and outbound mailbox adapter."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests

from .channel_routes import dispatch_routes
from .delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from .identifier_directory import IdentifierDirectory
from .listener_registry import listeners_for


GRAPH_API = "https://graph.facebook.com/v23.0"


class WhatsAppAdapter:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.listeners: list[dict[str, Any]] = []
        self.status = {"enabled": False, "connected": False, "lastError": None, "phoneNumbers": []}

    @staticmethod
    def _token(listener: dict[str, Any]) -> str:
        return os.environ.get(str(listener.get("token_env") or "WHATSAPP_ACCESS_TOKEN"), "").strip()

    def configure(self) -> bool:
        self.listeners = listeners_for("whatsapp")
        missing = [item["id"] for item in self.listeners if not self._token(item)]
        self.status.update({"enabled": bool(self.listeners) and not missing, "connected": False,
                            "phoneNumbers": [str(item.get("phone_number_id") or "") for item in self.listeners],
                            "lastError": f"Missing WhatsApp tokens for: {', '.join(missing)}" if missing else None})
        return bool(self.status["enabled"])

    def close(self) -> None:
        self.status["connected"] = False

    def cycle(self, _mailbox: Any) -> None:
        if self.status["enabled"]:
            self.status.update({"connected": True, "lastError": None, "lastCycleAt": time.time()})

    def _listener_for_phone(self, phone_id: str) -> dict[str, Any] | None:
        matches = [item for item in self.listeners if str(item.get("phone_number_id") or "") == phone_id]
        return matches[0] if len(matches) == 1 else None

    def handle_webhook(self, payload: dict[str, Any], mailbox: Any) -> None:
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                phone_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
                listener = self._listener_for_phone(phone_id)
                if listener is None or listener["direction"] not in {"inbound", "bidirectional"}:
                    continue
                names = {str(contact.get("wa_id") or ""): str((contact.get("profile") or {}).get("name") or "")
                         for contact in value.get("contacts") or []}
                for message in value.get("messages") or []:
                    sender_id = str(message.get("from") or "")
                    source_id = str(message.get("id") or "")
                    allowed = set(str(item) for item in listener.get("channel_ids", []))
                    if not sender_id or not source_id or (allowed and sender_id not in allowed):
                        continue
                    author = names.get(sender_id) or sender_id
                    if names.get(sender_id):
                        IdentifierDirectory(mailbox.mailbox_dir()).remember(
                            sender_id, author, system="whatsapp", kind="user")
                    message_type = str(message.get("type") or "")
                    text = str((message.get("text") or {}).get("body") or "")
                    origin = with_origin({"author": author, "author_id": sender_id,
                                          "whatsapp_message_type": message_type},
                                         adapter="whatsapp", listener_id=listener["id"],
                                         source_id=source_id, channel_id=sender_id)
                    if not DeliveryLedger(mailbox.mailbox_dir()).claim(origin, endpoint_id(
                        "whatsapp", listener_id=listener["id"], channel_id=sender_id,
                    )):
                        continue
                    recipients = list(dict.fromkeys([listener.get("bridge_agent"),
                                                      *listener.get("mailbox_recipients", [])]))
                    for recipient in filter(None, recipients):
                        mailbox.send(recipient, text, sender=f"whatsapp:{author}",
                                     message_type="whatsapp_message", channel_id=sender_id,
                                     channel_type="whatsapp", source_id=source_id,
                                     extra_fields={**origin, "whatsapp_payload": message.get(message_type) or {}})
                    dispatch_routes(mailbox, listener_id=listener["id"], channel_id=sender_id,
                                    message={**origin, "text": text, "source_id": source_id})

    def _listener_for(self, message: dict[str, Any]) -> dict[str, Any]:
        listener_id = str(message.get("listener_id") or "")
        listener = next((item for item in self.listeners if item["id"] == listener_id), None)
        if listener is None:
            eligible = [item for item in self.listeners if item["direction"] in {"outbound", "bidirectional"}]
            listener = eligible[0] if len(eligible) == 1 else None
        if listener is None:
            raise ValueError("WhatsApp outbound message requires an unambiguous listener_id")
        return listener

    def _headers(self, listener: dict[str, Any]) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token(listener)}"}

    def send_message(self, message: dict[str, Any]) -> None:
        listener = self._listener_for(message)
        recipient = str(message.get("channel_id") or "").strip()
        phone_id = str(listener.get("phone_number_id") or "").strip()
        if not recipient or not phone_id:
            raise ValueError("WhatsApp outbound message requires channel_id and phone_number_id")
        endpoint = f"{GRAPH_API}/{phone_id}/messages"
        text = str(message.get("text") or "")
        if text:
            response = self.session.post(endpoint, headers=self._headers(listener), json={
                "messaging_product": "whatsapp", "recipient_type": "individual", "to": recipient,
                "type": "text", "text": {"body": text},
            }, timeout=30)
            response.raise_for_status()
        for record in message.get("attachments") or []:
            path = Path(str(record.get("path") or "")).expanduser().resolve(strict=True)
            with path.open("rb") as stream:
                upload = self.session.post(f"{GRAPH_API}/{phone_id}/media", headers=self._headers(listener),
                                           data={"messaging_product": "whatsapp"},
                                           files={"file": (path.name, stream, record.get("mime_type") or
                                                           "application/octet-stream")}, timeout=60)
            upload.raise_for_status()
            media_id = str(upload.json()["id"])
            response = self.session.post(endpoint, headers=self._headers(listener), json={
                "messaging_product": "whatsapp", "to": recipient, "type": "document",
                "document": {"id": media_id, "filename": path.name},
            }, timeout=30)
            response.raise_for_status()
