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

from .attachment_gateway import attachment_url
from .attachment_storage import write_bytes
from .channel_routes import dispatch_routes
from .delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from .endpoint_address import subscription_recipients
from .identifier_directory import IdentifierDirectory
from .listener_registry import listeners_for


LINE_API = "https://api.line.me/v2/bot"
LINE_DATA_API = "https://api-data.line.me/v2/bot"


class LineAdapter:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.listeners: list[dict[str, Any]] = []
        self.status = {"enabled": False, "connected": False, "lastError": None, "channels": []}

    @staticmethod
    def _token(listener: dict[str, Any]) -> str:
        return os.environ.get(str(listener.get("token_env") or "LINE_CHANNEL_ACCESS_TOKEN"), "").strip()

    @staticmethod
    def _secret(listener: dict[str, Any]) -> str:
        return os.environ.get(str(listener.get("secret_env") or "LINE_CHANNEL_SECRET"), "").strip()

    def _headers(self, listener: dict[str, Any]) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token(listener)}"}

    def configure(self) -> bool:
        self.listeners = listeners_for("line")
        missing = [item["id"] for item in self.listeners if not self._token(item) or not self._secret(item)]
        self.status.update({"enabled": bool(self.listeners) and not missing, "connected": False,
                            "channels": [item["id"] for item in self.listeners],
                            "lastError": f"Missing LINE credentials for: {', '.join(missing)}" if missing else None})
        return bool(self.status["enabled"])

    def close(self) -> None:
        self.status["connected"] = False

    def cycle(self, _mailbox: Any) -> None:
        if self.status["enabled"]:
            self.status.update({"connected": True, "lastError": None, "lastCycleAt": time.time()})

    def authenticate_webhook(self, body: bytes, signature: str) -> dict[str, Any] | None:
        for listener in self.listeners:
            secret = self._secret(listener)
            if not secret:
                continue
            expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
            if signature and hmac.compare_digest(signature, expected):
                return listener
        return None

    @staticmethod
    def _conversation(source: dict[str, Any]) -> tuple[str, str]:
        kind = str(source.get("type") or "user")
        key = {"group": "groupId", "room": "roomId", "user": "userId"}.get(kind, "userId")
        return kind, str(source.get(key) or "")

    def _profile(self, listener: dict[str, Any], source: dict[str, Any], user_id: str) -> str:
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
            response = self.session.get(url, headers=self._headers(listener), timeout=15)
            response.raise_for_status()
            return str(response.json().get("displayName") or user_id)
        except Exception:
            return user_id

    def _conversation_name(self, listener: dict[str, Any], kind: str, conversation: str) -> str:
        if kind != "group":
            return conversation
        try:
            response = self.session.get(f"{LINE_API}/group/{conversation}/summary",
                                        headers=self._headers(listener), timeout=15)
            response.raise_for_status()
            return str(response.json().get("groupName") or conversation)
        except Exception:
            return conversation

    def _download(self, mailbox: Any, listener: dict[str, Any], message: dict[str, Any]) -> list[dict[str, Any]]:
        if message.get("type") not in {"image", "video", "audio", "file"}:
            return []
        message_id = str(message.get("id") or "")
        response = self.session.get(f"{LINE_DATA_API}/message/{message_id}/content",
                                    headers=self._headers(listener), timeout=60)
        response.raise_for_status()
        name = Path(str(message.get("fileName") or f"{message_id}.{message.get('type')}" )).name
        target = mailbox.mailbox_dir() / "attachments" / message_id / name
        write_bytes(mailbox.mailbox_dir(), target, response.content)
        return [{"path": str(target), "name": name,
                 "mime_type": response.headers.get("Content-Type") or mimetypes.guess_type(name)[0]
                 or "application/octet-stream", "size": len(response.content),
                 "sha256": hashlib.sha256(response.content).hexdigest()}]

    def handle_webhook(self, payload: dict[str, Any], mailbox: Any,
                       listener: dict[str, Any]) -> None:
        if listener["direction"] not in {"inbound", "bidirectional"}:
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
            allowed = set(str(item) for item in listener.get("channel_ids", []))
            if conversation not in allowed and not (kind == "user" and listener.get("include_direct_messages")):
                continue
            name = self._profile(listener, source, user_id)
            directory = IdentifierDirectory(mailbox.mailbox_dir())
            if user_id:
                directory.remember(user_id, name, system="line", kind="user")
            directory.remember(conversation, self._conversation_name(listener, kind, conversation),
                               system="line", kind=kind)
            message = event.get("message") or {}
            text = str(message.get("text") or f"[{message.get('type') or 'message'}]")
            attachments = self._download(mailbox, listener, message)
            origin = with_origin({"author": name, "author_id": user_id}, adapter="line",
                                 listener_id=listener["id"], source_id=source_id,
                                 channel_id=conversation)
            if not DeliveryLedger(mailbox.mailbox_dir()).claim(origin, endpoint_id(
                "line", listener_id=listener["id"], channel_id=conversation,
            )):
                continue
            recipients = list(dict.fromkeys([listener.get("bridge_agent"),
                                              *listener.get("mailbox_recipients", []),
                                              *subscription_recipients("line", listener, conversation)]))
            for recipient in filter(None, recipients):
                mailbox.send(recipient, text, sender=f"line:{name}", message_type="line_message",
                             channel_id=conversation, channel_type="line", source_id=source_id,
                             thread_id=str(message.get("quotedMessageId") or "") or None,
                             extra_fields={**origin, "attachments": attachments, "line_source_type": kind,
                                           "line_reply_token": str(event.get("replyToken") or "")})
            dispatch_routes(mailbox, listener_id=listener["id"], channel_id=conversation,
                            message={**origin, "text": text, "source_id": source_id,
                                     "attachments": attachments})

    def _listener_for(self, message: dict[str, Any]) -> dict[str, Any]:
        listener_id = str(message.get("listener_id") or "")
        listener = next((item for item in self.listeners if item["id"] == listener_id), None)
        if listener is None:
            eligible = [item for item in self.listeners if item["direction"] in {"outbound", "bidirectional"}]
            listener = eligible[0] if len(eligible) == 1 else None
        if listener is None:
            raise ValueError("LINE outbound message requires an unambiguous listener_id")
        return listener

    def send_message(self, message: dict[str, Any]) -> None:
        listener = self._listener_for(message)
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
            response = self.session.post(f"{LINE_API}/message/{endpoint}", headers=self._headers(listener),
                                         json=payload, timeout=30)
            response.raise_for_status()
