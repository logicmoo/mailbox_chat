"""WhatsApp Business Cloud API webhook and outbound mailbox adapter."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests

from ..delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from ..endpoint_address import subscription_recipients
from ..identifier_directory import IdentifierDirectory
from ..connector_registry import connectors_for


GRAPH_API = "https://graph.facebook.com/v23.0"


class WhatsAppAdapter:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.connectors: list[dict[str, Any]] = []
        self.status = {"enabled": False, "connected": False, "lastError": None, "phoneNumbers": []}

    @staticmethod
    def _token(connector: dict[str, Any]) -> str:
        return os.environ.get(str(connector.get("token_env") or "WHATSAPP_ACCESS_TOKEN"), "").strip()

    def configure(self) -> bool:
        self.connectors = connectors_for("whatsapp")
        missing = []
        for item in self.connectors:
            required = []
            if not self._token(item):
                required.append(str(item.get("token_env") or "WHATSAPP_ACCESS_TOKEN"))
            if not str(item.get("phone_number_id") or "").strip():
                required.append("phone_number_id")
            if item["direction"] in {"inbound", "bidirectional"}:
                if not os.environ.get("WHATSAPP_VERIFY_TOKEN", "").strip():
                    required.append("WHATSAPP_VERIFY_TOKEN")
                if not os.environ.get("WHATSAPP_APP_SECRET", "").strip():
                    required.append("WHATSAPP_APP_SECRET")
            if required:
                missing.append(f"{item['id']} ({', '.join(required)})")
        self.status.update({"enabled": bool(self.connectors) and not missing, "connected": False,
                            "phoneNumbers": [str(item.get("phone_number_id") or "") for item in self.connectors],
                            "lastError": f"Missing WhatsApp configuration: {', '.join(missing)}"
                            if missing else None})
        return bool(self.status["enabled"])

    def close(self) -> None:
        self.status["connected"] = False

    def cycle(self, _mailbox: Any) -> None:
        if self.status["enabled"]:
            self.status.update({"connected": True, "lastError": None, "lastCycleAt": time.time()})

    def _connector_for_phone(self, phone_id: str) -> dict[str, Any] | None:
        matches = [item for item in self.connectors if str(item.get("phone_number_id") or "") == phone_id]
        return matches[0] if len(matches) == 1 else None

    def handle_webhook(self, payload: dict[str, Any], mailbox: Any) -> None:
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                phone_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
                connector = self._connector_for_phone(phone_id)
                if connector is None or connector["direction"] not in {"inbound", "bidirectional"}:
                    continue
                names = {str(contact.get("wa_id") or ""): str((contact.get("profile") or {}).get("name") or "")
                         for contact in value.get("contacts") or []}
                for message in value.get("messages") or []:
                    sender_id = str(message.get("from") or "")
                    source_id = str(message.get("id") or "")
                    group_id = str(message.get("group_id") or (message.get("group") or {}).get("id") or "")
                    conversation_id = group_id or sender_id
                    allowed = set(str(item) for item in connector.get("channel_ids", []))
                    if (not sender_id or not source_id or (group_id and not connector.get("groups_enabled"))
                            or (allowed and conversation_id not in allowed)):
                        continue
                    author = names.get(sender_id) or sender_id
                    if names.get(sender_id):
                        IdentifierDirectory(mailbox.mailbox_dir()).remember(
                            sender_id, author, system="whatsapp", kind="user")
                    message_type = str(message.get("type") or "")
                    text = str((message.get("text") or {}).get("body") or "")
                    origin = with_origin({"author": author, "author_id": sender_id,
                                          "whatsapp_message_type": message_type},
                                         adapter="whatsapp", connector_id=connector["id"],
                                         source_id=source_id, channel_id=conversation_id)
                    if not DeliveryLedger(mailbox.mailbox_dir()).claim(origin, endpoint_id(
                        "whatsapp", connector_id=connector["id"], channel_id=conversation_id,
                    )):
                        continue
                    recipients = list(dict.fromkeys([connector.get("bridge_agent"),
                                                     *connector.get("mailbox_recipients", []),
                                                     *subscription_recipients(
                                                         "whatsapp", connector, conversation_id,
                                                     )]))
                    for recipient in filter(None, recipients):
                        mailbox.send(recipient, text, sender=f"whatsapp:{author}",
                                     message_type="whatsapp_message", channel_id=conversation_id,
                                     channel_type="whatsapp", source_id=source_id,
                                     extra_fields={**origin, "whatsapp_payload": message.get(message_type) or {},
                                                   "whatsapp_group_id": group_id,
                                                   "whatsapp_participant_id": sender_id})

    def _connector_for(self, message: dict[str, Any]) -> dict[str, Any]:
        connector_id = str(message.get("connector_id") or "")
        connector = next((item for item in self.connectors if item["id"] == connector_id), None)
        if connector is None:
            eligible = [item for item in self.connectors if item["direction"] in {"outbound", "bidirectional"}]
            connector = eligible[0] if len(eligible) == 1 else None
        if connector is None:
            raise ValueError("WhatsApp outbound message requires an unambiguous connector_id")
        return connector

    def _headers(self, connector: dict[str, Any]) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token(connector)}"}

    def send_message(self, message: dict[str, Any]) -> None:
        connector = self._connector_for(message)
        recipient = str(message.get("channel_id") or "").strip()
        phone_id = str(connector.get("phone_number_id") or "").strip()
        if not recipient or not phone_id:
            raise ValueError("WhatsApp outbound message requires channel_id and phone_number_id")
        endpoint = f"{GRAPH_API}/{phone_id}/messages"
        is_group = bool(message.get("whatsapp_group") or message.get("whatsapp_group_id"))
        if is_group and not connector.get("groups_enabled"):
            raise ValueError("WhatsApp Business Groups API is not enabled for this connector")
        recipient_type = "group" if is_group else "individual"
        text = str(message.get("text") or "")
        if text:
            response = self.session.post(endpoint, headers=self._headers(connector), json={
                "messaging_product": "whatsapp", "recipient_type": recipient_type, "to": recipient,
                "type": "text", "text": {"body": text},
            }, timeout=30)
            response.raise_for_status()
        for record in message.get("attachments") or []:
            path = Path(str(record.get("path") or "")).expanduser().resolve(strict=True)
            with path.open("rb") as stream:
                upload = self.session.post(f"{GRAPH_API}/{phone_id}/media", headers=self._headers(connector),
                                           data={"messaging_product": "whatsapp"},
                                           files={"file": (path.name, stream, record.get("mime_type") or
                                                           "application/octet-stream")}, timeout=60)
            upload.raise_for_status()
            media_id = str(upload.json()["id"])
            response = self.session.post(endpoint, headers=self._headers(connector), json={
                "messaging_product": "whatsapp", "recipient_type": recipient_type,
                "to": recipient, "type": "document",
                "document": {"id": media_id, "filename": path.name},
            }, timeout=30)
            response.raise_for_status()
