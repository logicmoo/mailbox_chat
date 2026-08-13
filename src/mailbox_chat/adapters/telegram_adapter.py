"""Telegram Bot API adapter backed exclusively by the durable mailbox."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

from ..channel_routes import dispatch_routes
from ..delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from ..endpoint_address import subscription_recipients
from ..listener_registry import listeners_for
from ..identifier_directory import IdentifierDirectory


TELEGRAM_API = "https://api.telegram.org"


class TelegramAdapter:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.listeners: list[dict[str, Any]] = []
        self.bot_ids: dict[str, str] = {}
        self.offsets: dict[str, int] = {}
        self.status: dict[str, Any] = {
            "enabled": False, "connected": False, "lastError": None, "chats": [],
        }

    @staticmethod
    def _token(listener: dict[str, Any]) -> str:
        token_env = str(listener.get("token_env") or "TELEGRAM_BOT_TOKEN")
        return os.environ.get(token_env, "").strip()

    def _url(self, listener: dict[str, Any], method: str) -> str:
        token = self._token(listener)
        if not token:
            name = listener.get("token_env") or "TELEGRAM_BOT_TOKEN"
            raise ValueError(f"Telegram listener {listener['id']} is missing {name}")
        return f"{TELEGRAM_API}/bot{token}/{method}"

    def configure(self) -> bool:
        self.listeners = listeners_for("telegram")
        missing = [item["id"] for item in self.listeners if not self._token(item)]
        self.status.update({
            "enabled": bool(self.listeners) and not missing,
            "connected": False,
            "chats": list(dict.fromkeys(
                chat for item in self.listeners for chat in item.get("channel_ids", [])
            )),
            "lastError": f"Missing Telegram tokens for: {', '.join(missing)}" if missing else None,
        })
        return bool(self.status["enabled"])

    @staticmethod
    def _offset_path(mailbox: Any, listener_id: str) -> Path:
        return mailbox.mailbox_dir() / "runtime" / "telegram" / f"{listener_id}.offset"

    def _save_offset(self, mailbox: Any, listener_id: str) -> None:
        path = self._offset_path(mailbox, listener_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(str(self.offsets[listener_id]), encoding="ascii")
        temporary.replace(path)

    def connect(self, mailbox: Any) -> None:
        directory = IdentifierDirectory(mailbox.mailbox_dir())
        for listener in self.listeners:
            response = self.session.get(self._url(listener, "getMe"), timeout=15)
            response.raise_for_status()
            result = response.json().get("result") or {}
            self.bot_ids[listener["id"]] = str(result.get("id") or "")
            bot_name = str(result.get("username") or result.get("first_name") or "").strip()
            if self.bot_ids[listener["id"]] and bot_name:
                directory.remember(self.bot_ids[listener["id"]], bot_name, system="telegram", kind="user")
            for chat_id in listener.get("channel_ids", []):
                request = directory.request_resolution("telegram", str(chat_id), resolver="getChat")
                if request["should_request"]:
                    try:
                        chat_response = self.session.get(
                            self._url(listener, "getChat"), params={"chat_id": chat_id}, timeout=15,
                        )
                        chat_response.raise_for_status()
                        chat = chat_response.json().get("result") or {}
                        chat_name = str(chat.get("title") or chat.get("username") or " ".join(filter(None, [
                            chat.get("first_name"), chat.get("last_name"),
                        ]))).strip()
                        directory.finish_resolution(
                            "telegram", str(chat.get("id") or chat_id), resolver="getChat",
                            text=chat_name, kind="chat",
                        )
                    except Exception as error:
                        directory.finish_resolution(
                            "telegram", str(chat_id), resolver="getChat", error=str(error),
                        )
                        raise
            offset_path = self._offset_path(mailbox, listener["id"])
            if offset_path.is_file():
                self.offsets[listener["id"]] = int(offset_path.read_text(encoding="ascii").strip() or "0")
            else:
                initial = self.session.get(
                    self._url(listener, "getUpdates"), params={"timeout": 0, "limit": 100}, timeout=15,
                )
                initial.raise_for_status()
                updates = initial.json().get("result") or []
                self.offsets[listener["id"]] = max(
                    (int(update.get("update_id", -1)) + 1 for update in updates), default=0,
                )
                self._save_offset(mailbox, listener["id"])
        self.status.update({"connected": True, "lastError": None})

    def close(self) -> None:
        self.bot_ids.clear()
        self.status["connected"] = False

    def cycle(self, mailbox: Any) -> None:
        if not self.status["enabled"]:
            return
        if not self.status["connected"]:
            self.connect(mailbox)
        for listener in self.listeners:
            if listener["direction"] in {"inbound", "bidirectional"}:
                self._poll_listener(mailbox, listener)
        self.status.update({"connected": True, "lastError": None, "lastCycleAt": time.time()})

    def _poll_listener(self, mailbox: Any, listener: dict[str, Any]) -> None:
        response = self.session.get(
            self._url(listener, "getUpdates"),
            params={"offset": self.offsets.get(listener["id"], 0), "timeout": 0, "limit": 100,
                    "allowed_updates": json.dumps(["message", "channel_post"])},
            timeout=15,
        )
        response.raise_for_status()
        for update in response.json().get("result") or []:
            update_id = int(update["update_id"])
            self.offsets[listener["id"]] = max(self.offsets.get(listener["id"], 0), update_id + 1)
            message = update.get("message") or update.get("channel_post") or {}
            if message:
                self._handle_message(mailbox, listener, update_id, message)
            self._save_offset(mailbox, listener["id"])

    def _handle_message(self, mailbox: Any, listener: dict[str, Any], update_id: int,
                        message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        channel_id = str(chat.get("id") or "")
        allowed = set(str(item) for item in listener.get("channel_ids", []))
        is_private = str(chat.get("type") or "") == "private"
        if allowed and channel_id not in allowed:
            if not (is_private and listener.get("include_direct_messages")):
                return
        author = message.get("from") or message.get("sender_chat") or {}
        if str(author.get("id") or "") == self.bot_ids.get(listener["id"]):
            return
        source_id = str(message.get("message_id") or update_id)
        thread_id = str(message.get("message_thread_id") or "") or None
        display_name = str(author.get("username") or " ".join(filter(None, [
            author.get("first_name"), author.get("last_name"),
        ])) or author.get("title") or author.get("id") or "user")
        text = str(message.get("text") or message.get("caption") or "")
        directory = IdentifierDirectory(mailbox.mailbox_dir())
        if author.get("id") and display_name:
            directory.remember(str(author["id"]), display_name, system="telegram", kind="user")
        chat_name = str(chat.get("title") or chat.get("username") or "").strip()
        if channel_id and chat_name:
            directory.remember(channel_id, chat_name, system="telegram", kind="chat")
        origin = with_origin(
            {"author": display_name, "author_id": str(author.get("id") or ""),
             "telegram_update_id": str(update_id), "telegram_chat_type": str(chat.get("type") or "")},
            adapter="telegram", listener_id=listener["id"], source_id=source_id,
            channel_id=channel_id, presence_id=str(listener.get("presence_id") or ""),
        )
        if not DeliveryLedger(mailbox.mailbox_dir()).claim(origin, endpoint_id(
            "telegram", listener_id=listener["id"], channel_id=channel_id,
            presence_id=str(listener.get("presence_id") or ""),
        )):
            return
        recipients = list(dict.fromkeys([
            *([listener["bridge_agent"]] if listener.get("bridge_agent") else []),
            *listener.get("mailbox_recipients", []),
            *subscription_recipients("telegram", listener, channel_id),
        ]))
        for recipient in recipients:
            mailbox.send(
                recipient, text, sender=f"telegram:{display_name}", message_type="telegram_message",
                channel_id=channel_id, channel_type="telegram", source_id=source_id,
                thread_id=thread_id, extra_fields=origin,
            )
        dispatch_routes(mailbox, listener_id=listener["id"], channel_id=channel_id,
                        message={**origin, "text": text, "source_id": source_id,
                                 "thread_id": thread_id})

    def _listener_for(self, message: dict[str, Any]) -> dict[str, Any]:
        listener_id = str(message.get("listener_id") or "")
        listener = next((item for item in self.listeners if item["id"] == listener_id), None)
        if listener is None:
            eligible = [item for item in self.listeners if item["direction"] in {"outbound", "bidirectional"}]
            listener = eligible[0] if len(eligible) == 1 else None
        if listener is None:
            raise ValueError("Telegram outbound message requires an unambiguous listener_id")
        return listener

    @staticmethod
    def _thread_payload(message: dict[str, Any]) -> dict[str, int]:
        thread_id = str(message.get("thread_id") or message.get("root_id") or "").strip()
        if not thread_id:
            return {}
        try:
            return {"message_thread_id": int(thread_id)}
        except ValueError as error:
            raise ValueError("Telegram thread_id must be a numeric forum topic ID") from error

    def send_message(self, message: dict[str, Any]) -> None:
        listener = self._listener_for(message)
        channel_id = str(message.get("channel_id") or "").strip()
        if not channel_id:
            raise ValueError("Telegram outbound message requires channel_id (chat ID)")
        thread = self._thread_payload(message)
        text = str(message.get("text") or "")
        for start in range(0, len(text), 4096):
            response = self.session.post(
                self._url(listener, "sendMessage"),
                json={"chat_id": channel_id, "text": text[start:start + 4096], **thread}, timeout=30,
            )
            response.raise_for_status()
        for record in message.get("attachments") or []:
            path = Path(str(record.get("path") or "")).expanduser().resolve(strict=True)
            with path.open("rb") as stream:
                response = self.session.post(
                    self._url(listener, "sendDocument"), data={"chat_id": channel_id, **thread},
                    files={"document": (path.name, stream, record.get("mime_type") or "application/octet-stream")},
                    timeout=60,
                )
            response.raise_for_status()
