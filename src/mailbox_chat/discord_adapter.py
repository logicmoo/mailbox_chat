"""Discord REST adapter backed exclusively by the durable mailbox layer."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

from .delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from .endpoint_address import subscription_recipients
from .listener_registry import listeners_for
from .channel_routes import dispatch_routes


DISCORD_API = "https://discord.com/api/v10"


class DiscordAdapter:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.listeners: list[dict[str, Any]] = []
        self.bot_ids: dict[str, str] = {}
        self.latest_ids: dict[tuple[str, str], str] = {}
        self.status: dict[str, Any] = {
            "enabled": False, "connected": False, "lastError": None, "channels": [],
        }

    @staticmethod
    def _token(listener: dict[str, Any]) -> str:
        token_env = str(listener.get("token_env") or "DISCORD_BOT_TOKEN")
        return os.environ.get(token_env, "").strip()

    def _headers(self, listener: dict[str, Any]) -> dict[str, str]:
        token = self._token(listener)
        if not token:
            raise ValueError(f"Discord listener {listener['id']} is missing {listener.get('token_env') or 'DISCORD_BOT_TOKEN'}")
        return {"Authorization": f"Bot {token}"}

    def configure(self) -> bool:
        self.listeners = listeners_for("discord")
        missing = [item["id"] for item in self.listeners if not self._token(item)]
        self.status.update({
            "enabled": bool(self.listeners) and not missing,
            "connected": False,
            "channels": list(dict.fromkeys(
                channel for item in self.listeners for channel in item.get("channel_ids", [])
            )),
            "lastError": f"Missing Discord tokens for: {', '.join(missing)}" if missing else None,
        })
        return bool(self.status["enabled"])

    def connect(self) -> None:
        for listener in self.listeners:
            response = self.session.get(f"{DISCORD_API}/users/@me", headers=self._headers(listener), timeout=15)
            response.raise_for_status()
            self.bot_ids[listener["id"]] = str(response.json()["id"])
            for channel_id in listener.get("channel_ids", []):
                latest = self.session.get(
                    f"{DISCORD_API}/channels/{channel_id}/messages",
                    headers=self._headers(listener), params={"limit": 1}, timeout=15,
                )
                latest.raise_for_status()
                messages = latest.json()
                self.latest_ids[(listener["id"], channel_id)] = str(messages[0]["id"]) if messages else "0"
        self.status.update({"connected": True, "lastError": None})

    def close(self) -> None:
        self.bot_ids.clear()
        self.status["connected"] = False

    def cycle(self, mailbox: Any) -> None:
        if not self.status["enabled"]:
            return
        if not self.status["connected"]:
            self.connect()
        for listener in self.listeners:
            if listener["direction"] not in {"inbound", "bidirectional"}:
                continue
            for channel_id in listener.get("channel_ids", []):
                self._poll_channel(mailbox, listener, channel_id)
        self.status.update({"connected": True, "lastError": None, "lastCycleAt": time.time()})

    def _poll_channel(self, mailbox: Any, listener: dict[str, Any], channel_id: str) -> None:
        response = self.session.get(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers=self._headers(listener),
            params={"limit": 100, "after": self.latest_ids.get((listener["id"], channel_id), "0")},
            timeout=15,
        )
        response.raise_for_status()
        for message in reversed(response.json()):
            source_id = str(message["id"])
            self.latest_ids[(listener["id"], channel_id)] = max(
                source_id, self.latest_ids.get((listener["id"], channel_id), "0"), key=int,
            )
            author = message.get("author") or {}
            if str(author.get("id") or "") == self.bot_ids.get(listener["id"]):
                continue
            origin = with_origin(
                {"author": str(author.get("username") or author.get("id") or ""),
                 "author_id": str(author.get("id") or ""),
                 "discord_guild_id": str(message.get("guild_id") or "")},
                adapter="discord", listener_id=listener["id"], source_id=source_id,
                channel_id=channel_id, presence_id=str(listener.get("presence_id") or ""),
            )
            claimed = DeliveryLedger(mailbox.mailbox_dir()).claim(origin, endpoint_id(
                "discord", listener_id=listener["id"], channel_id=channel_id,
                presence_id=str(listener.get("presence_id") or ""),
            ))
            if not claimed:
                continue
            recipients = list(dict.fromkeys([
                *([listener["bridge_agent"]] if listener.get("bridge_agent") else []),
                *listener.get("mailbox_recipients", []),
                *subscription_recipients("discord", listener, channel_id),
            ]))
            for recipient in recipients:
                mailbox.send(
                    recipient, str(message.get("content") or ""),
                    sender=f"discord:{author.get('username') or author.get('id') or 'user'}",
                    message_type="discord_message", channel_id=channel_id,
                    channel_type="discord", source_id=source_id,
                    extra_fields=origin,
                )
            dispatch_routes(mailbox, listener_id=listener["id"], channel_id=channel_id,
                            message={**origin, "text": str(message.get("content") or ""),
                                     "source_id": source_id})

    def send_message(self, message: dict[str, Any]) -> None:
        listener_id = str(message.get("listener_id") or "")
        listener = next((item for item in self.listeners if item["id"] == listener_id), None)
        if listener is None:
            eligible = [item for item in self.listeners if item["direction"] in {"outbound", "bidirectional"}]
            listener = eligible[0] if len(eligible) == 1 else None
        if listener is None:
            raise ValueError("Discord outbound message requires an unambiguous listener_id")
        channel_id = str(message.get("channel_id") or "").strip()
        if not channel_id:
            raise ValueError("Discord outbound message requires channel_id")
        attachments = list(message.get("attachments") or [])
        if attachments:
            opened = []
            try:
                files = {}
                for index, record in enumerate(attachments):
                    path = Path(str(record.get("path") or "")).expanduser().resolve(strict=True)
                    stream = path.open("rb")
                    opened.append(stream)
                    files[f"files[{index}]"] = (path.name, stream, record.get("mime_type") or "application/octet-stream")
                response = self.session.post(
                    f"{DISCORD_API}/channels/{channel_id}/messages", headers=self._headers(listener),
                    data={"payload_json": json.dumps({"content": str(message.get("text") or "")})},
                    files=files, timeout=60,
                )
            finally:
                for stream in opened:
                    stream.close()
        else:
            response = self.session.post(
                f"{DISCORD_API}/channels/{channel_id}/messages", headers=self._headers(listener),
                json={"content": str(message.get("text") or "")}, timeout=15,
            )
        response.raise_for_status()
