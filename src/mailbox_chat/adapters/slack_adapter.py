"""Slack Web API adapter using mailbox-backed delivery and deduplication."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import time
from pathlib import Path
from typing import Any

import requests

from ..attachment_storage import write_bytes
from ..delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from ..endpoint_address import subscription_recipients
from ..listener_registry import listeners_for
from ..channel_routes import dispatch_routes


SLACK_API = "https://slack.com/api"


class SlackAdapter:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.listeners: list[dict[str, Any]] = []
        self.bot_user_ids: dict[str, str] = {}
        self.latest_ts: dict[tuple[str, str], str] = {}
        self.status: dict[str, Any] = {
            "enabled": False, "connected": False, "lastError": None, "channels": [],
        }

    @staticmethod
    def _token(listener: dict[str, Any]) -> str:
        name = str(listener.get("token_env") or "SLACK_BOT_TOKEN")
        return os.environ.get(name, "").strip()

    def _headers(self, listener: dict[str, Any]) -> dict[str, str]:
        token = self._token(listener)
        if not token:
            raise ValueError(f"Slack listener {listener['id']} is missing {listener.get('token_env') or 'SLACK_BOT_TOKEN'}")
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _payload(response: requests.Response) -> dict[str, Any]:
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Slack API error: {payload.get('error') or 'unknown_error'}")
        return payload

    def configure(self) -> bool:
        self.listeners = listeners_for("slack")
        missing = [item["id"] for item in self.listeners if not self._token(item)]
        self.status.update({
            "enabled": bool(self.listeners) and not missing,
            "connected": False,
            "channels": list(dict.fromkeys(
                channel for item in self.listeners for channel in item.get("channel_ids", [])
            )),
            "lastError": f"Missing Slack tokens for: {', '.join(missing)}" if missing else None,
        })
        return bool(self.status["enabled"])

    def connect(self) -> None:
        for listener in self.listeners:
            auth = self._payload(self.session.post(
                f"{SLACK_API}/auth.test", headers=self._headers(listener), timeout=15,
            ))
            self.bot_user_ids[listener["id"]] = str(auth.get("user_id") or "")
            for channel_id in listener.get("channel_ids", []):
                history = self._payload(self.session.get(
                    f"{SLACK_API}/conversations.history", headers=self._headers(listener),
                    params={"channel": channel_id, "limit": 1}, timeout=15,
                ))
                messages = history.get("messages") or []
                self.latest_ts[(listener["id"], channel_id)] = str(messages[0]["ts"]) if messages else "0"
        self.status.update({"connected": True, "lastError": None})

    def close(self) -> None:
        self.bot_user_ids.clear()
        self.latest_ts.clear()
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
        payload = self._payload(self.session.get(
            f"{SLACK_API}/conversations.history", headers=self._headers(listener),
            params={"channel": channel_id, "oldest": self.latest_ts.get((listener["id"], channel_id), "0"),
                    "inclusive": "false", "limit": 100}, timeout=15,
        ))
        for message in reversed(payload.get("messages") or []):
            source_id = str(message.get("ts") or "")
            if not source_id:
                continue
            self.latest_ts[(listener["id"], channel_id)] = max(
                source_id, self.latest_ts.get((listener["id"], channel_id), "0"), key=float,
            )
            if message.get("user") == self.bot_user_ids.get(listener["id"]) or message.get("subtype") == "bot_message":
                continue
            attachments = self._download_files(mailbox, listener, source_id, message.get("files") or [])
            origin = with_origin(
                {"author": str(message.get("user") or message.get("username") or ""),
                 "attachments": attachments},
                adapter="slack", listener_id=listener["id"], source_id=source_id,
                channel_id=channel_id, presence_id=str(listener.get("presence_id") or ""),
            )
            if not DeliveryLedger(mailbox.mailbox_dir()).claim(origin, endpoint_id(
                "slack", listener_id=listener["id"], channel_id=channel_id,
                presence_id=str(listener.get("presence_id") or ""),
            )):
                continue
            recipients = list(dict.fromkeys([
                *([listener["bridge_agent"]] if listener.get("bridge_agent") else []),
                *listener.get("mailbox_recipients", []),
                *subscription_recipients("slack", listener, channel_id),
            ]))
            for recipient in recipients:
                mailbox.send(
                    recipient, str(message.get("text") or ""),
                    sender=f"slack:{message.get('user') or message.get('username') or 'user'}",
                    message_type="slack_message", channel_id=channel_id, channel_type="slack",
                    source_id=source_id, thread_id=str(message.get("thread_ts") or "") or None,
                    extra_fields=origin,
                )
            dispatch_routes(mailbox, listener_id=listener["id"], channel_id=channel_id,
                            message={**origin, "text": str(message.get("text") or ""),
                                     "source_id": source_id,
                                     "thread_id": str(message.get("thread_ts") or "") or None})

    def _download_files(self, mailbox: Any, listener: dict[str, Any], source_id: str,
                        files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for item in files:
            url = str(item.get("url_private_download") or item.get("url_private") or "")
            if not url:
                continue
            name = Path(str(item.get("name") or item.get("id") or "attachment")).name
            response = self.session.get(url, headers=self._headers(listener), timeout=60)
            response.raise_for_status()
            target_dir = mailbox.mailbox_dir() / "attachments" / source_id.replace(".", "-")
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / name
            write_bytes(mailbox.mailbox_dir(), target, response.content)
            records.append({
                "path": str(target), "name": name,
                "mime_type": str(item.get("mimetype") or mimetypes.guess_type(name)[0]
                                 or "application/octet-stream"),
                "size": len(response.content), "sha256": hashlib.sha256(response.content).hexdigest(),
                "slack_file_id": str(item.get("id") or ""),
            })
        return records

    def _listener_for(self, message: dict[str, Any]) -> dict[str, Any]:
        listener_id = str(message.get("listener_id") or "")
        listener = next((item for item in self.listeners if item["id"] == listener_id), None)
        if listener is None:
            eligible = [item for item in self.listeners if item["direction"] in {"outbound", "bidirectional"}]
            listener = eligible[0] if len(eligible) == 1 else None
        if listener is None:
            raise ValueError("Slack outbound message requires an unambiguous listener_id")
        return listener

    def send_message(self, message: dict[str, Any]) -> None:
        listener = self._listener_for(message)
        channel_id = str(message.get("channel_id") or "").strip()
        if not channel_id:
            raise ValueError("Slack outbound message requires channel_id")
        thread_ts = str(message.get("thread_id") or message.get("root_id") or "")
        text = str(message.get("text") or "")
        if text:
            payload: dict[str, Any] = {"channel": channel_id, "text": text}
            if thread_ts:
                payload["thread_ts"] = thread_ts
            self._payload(self.session.post(
                f"{SLACK_API}/chat.postMessage", headers=self._headers(listener), json=payload, timeout=15,
            ))
        for record in message.get("attachments") or []:
            self._upload_file(listener, channel_id, thread_ts, record)

    def _upload_file(self, listener: dict[str, Any], channel_id: str, thread_ts: str,
                     record: dict[str, Any]) -> None:
        path = Path(str(record.get("path") or "")).expanduser().resolve(strict=True)
        allocation = self._payload(self.session.get(
            f"{SLACK_API}/files.getUploadURLExternal", headers=self._headers(listener),
            params={"filename": path.name, "length": path.stat().st_size}, timeout=15,
        ))
        with path.open("rb") as stream:
            upload = self.session.post(str(allocation["upload_url"]), files={"file": (path.name, stream)}, timeout=60)
        upload.raise_for_status()
        complete: dict[str, Any] = {
            "files": [{"id": allocation["file_id"], "title": path.name}], "channel_id": channel_id,
        }
        if thread_ts:
            complete["thread_ts"] = thread_ts
        self._payload(self.session.post(
            f"{SLACK_API}/files.completeUploadExternal", headers=self._headers(listener),
            json=complete, timeout=15,
        ))
