"""Opt-in companion adapter for personal WhatsApp Web chats and groups."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from pathlib import Path
from typing import Any

import requests

from ..attachment_storage import write_bytes
from ..channel_routes import dispatch_routes
from ..delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from ..endpoint_address import subscription_recipients
from ..identifier_directory import IdentifierDirectory
from ..listener_registry import listeners_for


class WhatsAppPersonalAdapter:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.listeners: list[dict[str, Any]] = []
        self.status = {"enabled": False, "connected": False, "lastError": None, "companions": []}

    @staticmethod
    def _secret(listener: dict[str, Any]) -> str:
        return os.environ.get(str(listener.get("webhook_secret_env") or
                                  "WHATSAPP_PERSONAL_WEBHOOK_SECRET"), "").strip()

    @staticmethod
    def _token(listener: dict[str, Any]) -> str:
        return os.environ.get(str(listener.get("companion_token_env") or
                                  "WHATSAPP_PERSONAL_COMPANION_TOKEN"), "").strip()

    @staticmethod
    def _url(listener: dict[str, Any]) -> str:
        return str(listener.get("companion_url") or "http://127.0.0.1:46668").rstrip("/")

    def configure(self) -> bool:
        self.listeners = listeners_for("whatsapp_personal")
        missing = [item["id"] for item in self.listeners if not self._secret(item) or not self._token(item)]
        self.status.update({"enabled": bool(self.listeners) and not missing, "connected": False,
                            "companions": [self._url(item) for item in self.listeners],
                            "lastError": f"Missing WhatsApp Personal credentials for: {', '.join(missing)}"
                            if missing else None})
        return bool(self.status["enabled"])

    def close(self) -> None:
        self.status["connected"] = False

    def cycle(self, _mailbox: Any) -> None:
        if not self.status["enabled"]:
            return
        connected = False
        errors = []
        for listener in self.listeners:
            try:
                response = self.session.get(f"{self._url(listener)}/status",
                                            headers={"Authorization": f"Bearer {self._token(listener)}"}, timeout=5)
                response.raise_for_status()
                connected = connected or bool(response.json().get("ready"))
            except Exception as error:
                errors.append(str(error))
        self.status.update({"connected": connected, "lastError": "; ".join(errors) or None,
                            "lastCycleAt": time.time()})

    def authenticate_webhook(self, body: bytes, signature: str) -> dict[str, Any] | None:
        for listener in self.listeners:
            secret = self._secret(listener)
            expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest() if secret else ""
            if signature and expected and hmac.compare_digest(signature, expected):
                return listener
        return None

    def handle_webhook(self, payload: dict[str, Any], mailbox: Any, listener: dict[str, Any]) -> None:
        if listener["direction"] not in {"inbound", "bidirectional"}:
            return
        chat_id = str(payload.get("chat_id") or "")
        source_id = str(payload.get("message_id") or "")
        author_id = str(payload.get("author_id") or chat_id)
        is_group = bool(payload.get("is_group"))
        allowed = set(str(item) for item in listener.get("channel_ids", []))
        if not chat_id or not source_id or (allowed and chat_id not in allowed):
            return
        if is_group and not listener.get("include_groups", True):
            return
        if not is_group and not listener.get("include_direct_messages"):
            return
        author = str(payload.get("author_name") or author_id)
        chat_name = str(payload.get("chat_name") or chat_id)
        directory = IdentifierDirectory(mailbox.mailbox_dir())
        directory.remember(author_id, author, system="whatsapp_personal", kind="user")
        directory.remember(chat_id, chat_name, system="whatsapp_personal",
                           kind="group" if is_group else "chat")
        attachments = []
        media = payload.get("media") if isinstance(payload.get("media"), dict) else None
        if media and media.get("data"):
            content = base64.b64decode(str(media["data"]), validate=True)
            name = Path(str(media.get("name") or f"{source_id}.bin")).name
            target = mailbox.mailbox_dir() / "attachments" / source_id.replace("/", "_") / name
            write_bytes(mailbox.mailbox_dir(), target, content)
            attachments.append({"path": str(target), "name": name,
                                "mime_type": str(media.get("mime_type") or "application/octet-stream"),
                                "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})
        origin = with_origin({"author": author, "author_id": author_id}, adapter="whatsapp_personal",
                             listener_id=listener["id"], source_id=source_id, channel_id=chat_id)
        if not DeliveryLedger(mailbox.mailbox_dir()).claim(origin, endpoint_id(
            "whatsapp_personal", listener_id=listener["id"], channel_id=chat_id,
        )):
            return
        text = str(payload.get("text") or ("[attachment]" if attachments else ""))
        recipients = list(dict.fromkeys([listener.get("bridge_agent"),
                                         *listener.get("mailbox_recipients", []),
                                         *subscription_recipients(
                                             "whatsapp_personal", listener, chat_id,
                                         )]))
        for recipient in filter(None, recipients):
            mailbox.send(recipient, text, sender=f"whatsapp-personal:{author}",
                         message_type="whatsapp_personal_message", channel_id=chat_id,
                         channel_type="whatsapp_personal", source_id=source_id,
                         extra_fields={**origin, "attachments": attachments, "whatsapp_is_group": is_group})
        dispatch_routes(mailbox, listener_id=listener["id"], channel_id=chat_id,
                        message={**origin, "text": text, "source_id": source_id,
                                 "attachments": attachments})

    def _listener_for(self, message: dict[str, Any]) -> dict[str, Any]:
        listener_id = str(message.get("listener_id") or "")
        listener = next((item for item in self.listeners if item["id"] == listener_id), None)
        if listener is None:
            eligible = [item for item in self.listeners if item["direction"] in {"outbound", "bidirectional"}]
            listener = eligible[0] if len(eligible) == 1 else None
        if listener is None:
            raise ValueError("WhatsApp Personal outbound message requires an unambiguous listener_id")
        return listener

    def send_message(self, message: dict[str, Any]) -> None:
        listener = self._listener_for(message)
        chat_id = str(message.get("channel_id") or "").strip()
        if not chat_id:
            raise ValueError("WhatsApp Personal outbound message requires a chat or group channel_id")
        payload = {"chat_id": chat_id, "text": str(message.get("text") or ""),
                   "attachments": [str(item.get("path") or "") for item in message.get("attachments") or []]}
        response = self.session.post(f"{self._url(listener)}/send",
                                     headers={"Authorization": f"Bearer {self._token(listener)}"},
                                     json=payload, timeout=60)
        response.raise_for_status()
