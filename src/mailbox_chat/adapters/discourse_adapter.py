"""Discourse webhook and REST posting adapter."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

import requests

from ..attachment_gateway import attachment_url
from ..channel_routes import dispatch_routes
from ..delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from ..endpoint_address import subscription_recipients
from ..identifier_directory import IdentifierDirectory
from ..listener_registry import listeners_for


class DiscourseAdapter:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.listeners: list[dict[str, Any]] = []
        self.status = {"enabled": False, "connected": False, "lastError": None, "sites": []}

    @staticmethod
    def _environment(listener: dict[str, Any], field: str, default: str) -> str:
        return os.environ.get(str(listener.get(field) or default), "").strip()

    def _api_key(self, listener: dict[str, Any]) -> str:
        return self._environment(listener, "api_key_env", "DISCOURSE_API_KEY")

    def _webhook_secret(self, listener: dict[str, Any]) -> str:
        return self._environment(listener, "webhook_secret_env", "DISCOURSE_WEBHOOK_SECRET")

    @staticmethod
    def _base_url(listener: dict[str, Any]) -> str:
        value = str(listener.get("base_url") or "")
        if value.startswith("$"):
            value = os.environ.get(value[1:], "")
        return value.rstrip("/")

    def _headers(self, listener: dict[str, Any]) -> dict[str, str]:
        return {"Api-Key": self._api_key(listener),
                "Api-Username": str(listener.get("api_username") or "system"),
                "Accept": "application/json"}

    def configure(self) -> bool:
        self.listeners = listeners_for("discourse")
        missing = [item["id"] for item in self.listeners
                   if not self._base_url(item) or not self._api_key(item) or not self._webhook_secret(item)]
        self.status.update({"enabled": bool(self.listeners) and not missing, "connected": False,
                            "sites": [self._base_url(item) for item in self.listeners],
                            "lastError": f"Missing Discourse configuration for: {', '.join(missing)}"
                            if missing else None})
        return bool(self.status["enabled"])

    def close(self) -> None:
        self.status["connected"] = False

    def cycle(self, _mailbox: Any) -> None:
        if self.status["enabled"]:
            self.status.update({"connected": True, "lastError": None, "lastCycleAt": time.time()})

    def authenticate_webhook(self, body: bytes, signature: str) -> dict[str, Any] | None:
        for listener in self.listeners:
            secret = self._webhook_secret(listener)
            if not secret:
                continue
            expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            if signature and hmac.compare_digest(signature, expected):
                return listener
        return None

    def handle_webhook(self, payload: dict[str, Any], mailbox: Any, listener: dict[str, Any],
                       *, event_id: str = "", event_name: str = "") -> None:
        if listener["direction"] not in {"inbound", "bidirectional"}:
            return
        post = payload.get("post") or payload.get("topic") or payload
        if not isinstance(post, dict):
            raise ValueError("Discourse webhook payload has no post or topic object")
        topic_id = str(post.get("topic_id") or post.get("id") or "")
        post_id = str(post.get("id") or event_id)
        category_id = str(post.get("category_id") or "")
        allowed_topics = set(str(item) for item in listener.get("channel_ids", []))
        allowed_categories = set(str(item) for item in listener.get("category_ids", []))
        if allowed_topics and topic_id not in allowed_topics:
            return
        if allowed_categories and category_id and category_id not in allowed_categories:
            return
        if not topic_id or not post_id:
            return
        username = str(post.get("username") or post.get("name") or "unknown")
        IdentifierDirectory(mailbox.mailbox_dir()).remember(username, username, system="discourse", kind="user")
        title = str(post.get("topic_title") or post.get("title") or f"Topic {topic_id}")
        IdentifierDirectory(mailbox.mailbox_dir()).remember(topic_id, title, system="discourse", kind="topic")
        text = str(post.get("raw") or post.get("excerpt") or post.get("cooked") or "")
        source_id = event_id or post_id
        origin = with_origin({"author": username, "author_id": str(post.get("user_id") or username)},
                             adapter="discourse", listener_id=listener["id"], source_id=source_id,
                             channel_id=topic_id)
        if not DeliveryLedger(mailbox.mailbox_dir()).claim(origin, endpoint_id(
            "discourse", listener_id=listener["id"], channel_id=topic_id,
        )):
            return
        recipients = list(dict.fromkeys([listener.get("bridge_agent"),
                                         *listener.get("mailbox_recipients", []),
                                         *subscription_recipients("discourse", listener, topic_id)]))
        for recipient in filter(None, recipients):
            mailbox.send(recipient, text, sender=f"discourse:{username}",
                         message_type="discourse_post", channel_id=topic_id,
                         channel_type="discourse", source_id=source_id,
                         thread_id=str(post.get("reply_to_post_number") or "") or None,
                         extra_fields={**origin, "discourse_post_id": post_id,
                                       "discourse_post_number": post.get("post_number"),
                                       "discourse_event": event_name, "topic_title": title})
        dispatch_routes(mailbox, listener_id=listener["id"], channel_id=topic_id,
                        message={**origin, "text": text, "source_id": source_id})

    def _listener_for(self, message: dict[str, Any]) -> dict[str, Any]:
        listener_id = str(message.get("listener_id") or "")
        listener = next((item for item in self.listeners if item["id"] == listener_id), None)
        if listener is None:
            eligible = [item for item in self.listeners if item["direction"] in {"outbound", "bidirectional"}]
            listener = eligible[0] if len(eligible) == 1 else None
        if listener is None:
            raise ValueError("Discourse outbound message requires an unambiguous listener_id")
        return listener

    def send_message(self, message: dict[str, Any]) -> None:
        listener = self._listener_for(message)
        topic_id = str(message.get("channel_id") or "").strip()
        raw = str(message.get("text") or "")
        links = [f"[{record.get('name') or 'Attachment'}]({attachment_url(record)})"
                 for record in message.get("attachments") or []]
        if links:
            raw = "\n\n".join(filter(None, [raw, *links]))
        payload: dict[str, Any] = {"raw": raw}
        if topic_id:
            payload["topic_id"] = int(topic_id)
            reply = message.get("reply_to_post_number") or message.get("thread_id")
            if reply:
                payload["reply_to_post_number"] = int(reply)
        else:
            title = str(message.get("topic_title") or "").strip()
            if not title:
                raise ValueError("New Discourse topics require topic_title")
            payload["title"] = title
            category = message.get("category_id") or listener.get("default_category_id")
            if category:
                payload["category"] = int(category)
        response = self.session.post(f"{self._base_url(listener)}/posts.json",
                                     headers=self._headers(listener), json=payload, timeout=30)
        response.raise_for_status()
