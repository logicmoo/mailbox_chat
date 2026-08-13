"""Facebook Messenger webhook and Graph Send API mailbox adapter."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests

from ..channel_routes import dispatch_routes
from ..delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from ..endpoint_address import subscription_recipients
from ..identifier_directory import IdentifierDirectory
from ..listener_registry import listeners_for


GRAPH_API = "https://graph.facebook.com/v23.0"


class FacebookMessengerAdapter:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.listeners: list[dict[str, Any]] = []
        self.status = {"enabled": False, "connected": False, "lastError": None, "pages": []}

    @staticmethod
    def _token(listener: dict[str, Any]) -> str:
        return os.environ.get(str(listener.get("token_env") or "FACEBOOK_PAGE_ACCESS_TOKEN"), "").strip()

    def configure(self) -> bool:
        self.listeners = listeners_for("facebook_messenger")
        missing = []
        for item in self.listeners:
            required = []
            if not self._token(item):
                required.append(str(item.get("token_env") or "FACEBOOK_PAGE_ACCESS_TOKEN"))
            if not str(item.get("page_id") or "").strip():
                required.append("page_id")
            if item["direction"] in {"inbound", "bidirectional"}:
                if not os.environ.get("FACEBOOK_VERIFY_TOKEN", "").strip():
                    required.append("FACEBOOK_VERIFY_TOKEN")
                if not os.environ.get("FACEBOOK_APP_SECRET", "").strip():
                    required.append("FACEBOOK_APP_SECRET")
            if required:
                missing.append(f"{item['id']} ({', '.join(required)})")
        self.status.update({"enabled": bool(self.listeners) and not missing, "connected": False,
                            "pages": [str(item.get("page_id") or "") for item in self.listeners],
                            "lastError": f"Missing Facebook configuration: {', '.join(missing)}"
                            if missing else None})
        return bool(self.status["enabled"])

    def close(self) -> None:
        self.status["connected"] = False

    def cycle(self, _mailbox: Any) -> None:
        if self.status["enabled"]:
            self.status.update({"connected": True, "lastError": None, "lastCycleAt": time.time()})

    def _listener_for_page(self, page_id: str) -> dict[str, Any] | None:
        matches = [item for item in self.listeners if str(item.get("page_id") or "") == page_id]
        return matches[0] if len(matches) == 1 else None

    def _resolve_sender(self, mailbox: Any, listener: dict[str, Any], sender_id: str) -> str:
        directory = IdentifierDirectory(mailbox.mailbox_dir())
        known = directory.find(system="facebook_messenger", identifier=sender_id, limit=1)
        if known:
            return str(known[0]["text"])
        request = directory.request_resolution("facebook_messenger", sender_id, resolver="Graph user profile")
        if not request["should_request"]:
            return sender_id
        try:
            response = self.session.get(f"{GRAPH_API}/{sender_id}", params={
                "fields": "name,first_name,last_name", "access_token": self._token(listener),
            }, timeout=15)
            response.raise_for_status()
            profile = response.json()
            name = str(profile.get("name") or " ".join(filter(None, [profile.get("first_name"),
                                                                      profile.get("last_name")]))).strip()
            directory.finish_resolution("facebook_messenger", sender_id,
                                        resolver="Graph user profile", text=name, kind="user")
            return name or sender_id
        except Exception as error:
            directory.finish_resolution("facebook_messenger", sender_id,
                                        resolver="Graph user profile", error=str(error))
            return sender_id

    def handle_webhook(self, payload: dict[str, Any], mailbox: Any) -> None:
        for entry in payload.get("entry") or []:
            page_id = str(entry.get("id") or "")
            listener = self._listener_for_page(page_id)
            if listener is None or listener["direction"] not in {"inbound", "bidirectional"}:
                continue
            for event in entry.get("messaging") or []:
                message = event.get("message") or {}
                source_id = str(message.get("mid") or "")
                sender_id = str((event.get("sender") or {}).get("id") or "")
                if not source_id or not sender_id or message.get("is_echo"):
                    continue
                allowed = set(str(item) for item in listener.get("channel_ids", []))
                if allowed and sender_id not in allowed:
                    continue
                author = self._resolve_sender(mailbox, listener, sender_id)
                origin = with_origin({"author": author, "author_id": sender_id},
                                     adapter="facebook_messenger", listener_id=listener["id"],
                                     source_id=source_id, channel_id=sender_id)
                if not DeliveryLedger(mailbox.mailbox_dir()).claim(origin, endpoint_id(
                    "facebook_messenger", listener_id=listener["id"], channel_id=sender_id,
                )):
                    continue
                text = str(message.get("text") or "")
                recipients = list(dict.fromkeys([listener.get("bridge_agent"),
                                                  *listener.get("mailbox_recipients", []),
                                                  *subscription_recipients(
                                                      "facebook_messenger", listener, sender_id,
                                                  )]))
                for recipient in filter(None, recipients):
                    mailbox.send(recipient, text, sender=f"facebook:{author}",
                                 message_type="facebook_messenger_message", channel_id=sender_id,
                                 channel_type="facebook_messenger", source_id=source_id,
                                 extra_fields={**origin, "facebook_attachments": message.get("attachments") or []})
                dispatch_routes(mailbox, listener_id=listener["id"], channel_id=sender_id,
                                message={**origin, "text": text, "source_id": source_id})

    def _listener_for(self, message: dict[str, Any]) -> dict[str, Any]:
        listener_id = str(message.get("listener_id") or "")
        listener = next((item for item in self.listeners if item["id"] == listener_id), None)
        if listener is None:
            eligible = [item for item in self.listeners if item["direction"] in {"outbound", "bidirectional"}]
            listener = eligible[0] if len(eligible) == 1 else None
        if listener is None:
            raise ValueError("Facebook Messenger outbound message requires an unambiguous listener_id")
        return listener

    def send_message(self, message: dict[str, Any]) -> None:
        listener = self._listener_for(message)
        recipient = str(message.get("channel_id") or "").strip()
        if not recipient:
            raise ValueError("Facebook Messenger outbound message requires channel_id (PSID)")
        headers = {"Authorization": f"Bearer {self._token(listener)}"}
        text = str(message.get("text") or "")
        if text:
            response = self.session.post(f"{GRAPH_API}/me/messages", headers=headers,
                                         json={"recipient": {"id": recipient},
                                               "messaging_type": "RESPONSE", "message": {"text": text}},
                                         timeout=30)
            response.raise_for_status()
        for record in message.get("attachments") or []:
            path = Path(str(record.get("path") or "")).expanduser().resolve(strict=True)
            with path.open("rb") as stream:
                response = self.session.post(
                    f"{GRAPH_API}/me/messages", headers=headers,
                    data={"recipient": f'{{"id":"{recipient}"}}', "messaging_type": "RESPONSE",
                          "message": '{"attachment":{"type":"file","payload":{"is_reusable":false}}}'},
                    files={"filedata": (path.name, stream, record.get("mime_type") or "application/octet-stream")},
                    timeout=60,
                )
            response.raise_for_status()
