"""LINE Messaging API adapter for users, groups, and multi-person rooms."""

from __future__ import annotations

import base64
import hashlib
import hmac
import mimetypes
import os
import time
from pathlib import Path
from typing import Any

import requests

from ..attachment_gateway import attachment_url
from ..attachment_storage import write_bytes
from ..delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from ..endpoint_address import subscription_recipients
from ..identifier_directory import IdentifierDirectory
from ..connector_registry import connectors_for


LINE_API = "https://api.line.me/v2/bot"
LINE_DATA_API = "https://api-data.line.me/v2/bot"


class LineAdapter:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.connectors: list[dict[str, Any]] = []
        self.status = {"enabled": False, "connected": False, "lastError": None, "channels": []}

    @staticmethod
    def _token(connector: dict[str, Any]) -> str:
        return os.environ.get(str(connector.get("token_env") or "LINE_CHANNEL_ACCESS_TOKEN"), "").strip()

    @staticmethod
    def _secret(connector: dict[str, Any]) -> str:
        return os.environ.get(str(connector.get("secret_env") or "LINE_CHANNEL_SECRET"), "").strip()

    def _headers(self, connector: dict[str, Any]) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token(connector)}"}

    def configure(self) -> bool:
        self.connectors = connectors_for("line")
        missing = [item["id"] for item in self.connectors if not self._token(item) or not self._secret(item)]
        self.status.update({"enabled": bool(self.connectors) and not missing, "connected": False,
                            "channels": [item["id"] for item in self.connectors],
                            "lastError": f"Missing LINE credentials for: {', '.join(missing)}" if missing else None})
        return bool(self.status["enabled"])

    def close(self) -> None:
        self.status["connected"] = False

    def cycle(self, _mailbox: Any) -> None:
        if self.status["enabled"]:
            self.status.update({"connected": True, "lastError": None, "lastCycleAt": time.time()})

    def authenticate_webhook(self, body: bytes, signature: str) -> dict[str, Any] | None:
        for connector in self.connectors:
            secret = self._secret(connector)
            if not secret:
                continue
            expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
            if signature and hmac.compare_digest(signature, expected):
                return connector
        return None

    @staticmethod
    def _conversation(source: dict[str, Any]) -> tuple[str, str]:
        kind = str(source.get("type") or "user")
        key = {"group": "groupId", "room": "roomId", "user": "userId"}.get(kind, "userId")
        return kind, str(source.get(key) or "")

    def _profile(self, connector: dict[str, Any], source: dict[str, Any], user_id: str) -> str:
        if not user_id:
            return "unknown"
        kind, conversation = self._conversation(source)
        if kind == "group":
            url = f"{LINE_API}/group/{conversation}/member/{user_id}"
        elif kind == "room":
            url = f"{LINE_API}/room/{conversation}/member/{user_id}"
        else:
            url = f"{LINE_API}/profile/{user_id}"
        try:
            response = self.session.get(url, headers=self._headers(connector), timeout=15)
            response.raise_for_status()
            return str(response.json().get("displayName") or user_id)
        except Exception:
            return user_id

    def _conversation_name(self, connector: dict[str, Any], kind: str, conversation: str) -> str:
        if kind != "group":
            return conversation
        try:
            response = self.session.get(f"{LINE_API}/group/{conversation}/summary",
                                        headers=self._headers(connector), timeout=15)
            response.raise_for_status()
            return str(response.json().get("groupName") or conversation)
        except Exception:
            return conversation

    def _download(self, mailbox: Any, connector: dict[str, Any], message: dict[str, Any]) -> list[dict[str, Any]]:
        if message.get("type") not in {"image", "video", "audio", "file"}:
            return []
        message_id = str(message.get("id") or "")
        response = self.session.get(f"{LINE_DATA_API}/message/{message_id}/content",
                                    headers=self._headers(connector), timeout=60)
        response.raise_for_status()
        name = Path(str(message.get("fileName") or f"{message_id}.{message.get('type')}" )).name
        target = mailbox.mailbox_dir() / "attachments" / message_id / name
        write_bytes(mailbox.mailbox_dir(), target, response.content)
        return [{"path": str(target), "name": name,
                 "mime_type": response.headers.get("Content-Type") or mimetypes.guess_type(name)[0]
                 or "application/octet-stream", "size": len(response.content),
                 "sha256": hashlib.sha256(response.content).hexdigest()}]

    def handle_webhook(self, payload: dict[str, Any], mailbox: Any,
                       connector: dict[str, Any]) -> None:
        if connector["direction"] not in {"inbound", "bidirectional"}:
            return
        for event in payload.get("events") or []:
            if event.get("type") != "message":
                continue
            source = event.get("source") or {}
            kind, conversation = self._conversation(source)
            source_id = str(event.get("webhookEventId") or (event.get("message") or {}).get("id") or "")
            user_id = str(source.get("userId") or "")
            if not conversation or not source_id:
                continue
            allowed = set(str(item) for item in connector.get("channel_ids", []))
            if conversation not in allowed and not (kind == "user" and connector.get("include_direct_messages")):
                continue
            name = self._profile(connector, source, user_id)
            directory = IdentifierDirectory(mailbox.mailbox_dir())
            if user_id:
                directory.remember(user_id, name, system="line", kind="user")
            directory.remember(conversation, self._conversation_name(connector, kind, conversation),
                               system="line", kind=kind)
            message = event.get("message") or {}
            text = str(message.get("text") or f"[{message.get('type') or 'message'}]")
            attachments = self._download(mailbox, connector, message)
            origin = with_origin({"author": name, "author_id": user_id}, adapter="line",
                                 connector_id=connector["id"], source_id=source_id,
                                 channel_id=conversation)
            if not DeliveryLedger(mailbox.mailbox_dir()).claim(origin, endpoint_id(
                "line", connector_id=connector["id"], channel_id=conversation,
            )):
                continue
            recipients = list(dict.fromkeys([connector.get("bridge_agent"),
                                              *connector.get("mailbox_recipients", []),
                                              *subscription_recipients("line", connector, conversation)]))
            for recipient in filter(None, recipients):
                mailbox.send(recipient, text, sender=f"line:{name}", message_type="line_message",
                             channel_id=conversation, channel_type="line", source_id=source_id,
                             thread_id=str(message.get("quotedMessageId") or "") or None,
                             extra_fields={**origin, "attachments": attachments, "line_source_type": kind,
                                           "line_reply_token": str(event.get("replyToken") or "")})

    def _connector_for(self, message: dict[str, Any]) -> dict[str, Any]:
        connector_id = str(message.get("connector_id") or "")
        connector = next((item for item in self.connectors if item["id"] == connector_id), None)
        if connector is None:
            eligible = [item for item in self.connectors if item["direction"] in {"outbound", "bidirectional"}]
            connector = eligible[0] if len(eligible) == 1 else None
        if connector is None:
            raise ValueError("LINE outbound message requires an unambiguous connector_id")
        return connector

    def send_message(self, message: dict[str, Any]) -> None:
        connector = self._connector_for(message)
        destination = str(message.get("channel_id") or "").strip()
        if not destination:
            raise ValueError("LINE outbound message requires channel_id (user, group, or room ID)")
        messages: list[dict[str, Any]] = []
        text = str(message.get("text") or "")
        messages.extend({"type": "text", "text": text[start:start + 5000]}
                        for start in range(0, len(text), 5000))
        for record in message.get("attachments") or []:
            url = attachment_url(record)
            if str(record.get("mime_type") or "").startswith("image/"):
                messages.append({"type": "image", "originalContentUrl": url, "previewImageUrl": url})
            else:
                messages.append({"type": "text", "text": f"Attachment: {url}"})
        reply_token = str(message.get("line_reply_token") or "")
        for start in range(0, len(messages), 5):
            batch = messages[start:start + 5]
            if start == 0 and reply_token:
                endpoint = "reply"
                payload = {"replyToken": reply_token, "messages": batch}
            else:
                endpoint = "push"
                payload = {"to": destination, "messages": batch}
            response = self.session.post(f"{LINE_API}/message/{endpoint}", headers=self._headers(connector),
                                         json=payload, timeout=30)
            response.raise_for_status()
