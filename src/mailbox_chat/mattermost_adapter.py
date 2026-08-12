"""Mattermost transport adapter for Mailbox Channel Relay Bridging Proxy."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from .listener_registry import config_dir, listeners_for
from .delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from .channel_routes import dispatch_routes


LOGGER = logging.getLogger(__name__)
RELAY_RECIPIENT = "channel-relay"
DEFAULT_INBOUND_RECIPIENTS = ("symbolic-workbench", "omegaclaw-core", "omegaclaw-min")
RELAY_PORT = 46667


def _mailbox_module():
    from . import agent_mailbox

    return agent_mailbox


class MattermostRelay:
    def __init__(self, *, session: requests.Session | None = None, sleep: Any = time.sleep) -> None:
        self.session = session or requests.Session()
        self.sleep = sleep
        self.stop_requested = False
        self.status: dict[str, Any] = {
            "enabled": False,
            "running": False,
            "connected": False,
            "lastError": None,
            "lastCycleAt": None,
            "channels": [],
        }
        self._latest_create_at: dict[str, int] = {}
        self._bot_user_id = ""
        self._next_dm_refresh = 0.0

    def configure(self) -> bool:
        load_dotenv(config_dir() / ".env", override=False)
        required = ("MM_URL", "MM_BOT_TOKEN", "MM_CHANNEL_ID")
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        enabled = os.environ.get("WORKBENCH_MATTERMOST_RELAY", "1").strip().lower() not in {"0", "false", "no"}
        self.status["enabled"] = enabled and not missing
        if enabled and missing:
            self.status["lastError"] = f"Missing Mattermost settings: {', '.join(missing)}"
        return bool(self.status["enabled"])

    def stop(self) -> None:
        self.stop_requested = True
        self.status["running"] = False
        self.status["connected"] = False

    def _channels(self) -> list[str]:
        configured_listeners = listeners_for("mattermost", direction="inbound")
        configured_channels = [
            channel_id for listener in configured_listeners for channel_id in listener["channel_ids"]
        ]
        if configured_channels:
            return list(dict.fromkeys(configured_channels))
        primary = os.environ["MM_CHANNEL_ID"].strip()
        configured = os.environ.get("MM_CHANNEL_IDS", primary)
        return list(dict.fromkeys([primary, *(item.strip() for item in configured.split(",") if item.strip())]))

    def _inbound_recipients(self, channel_id: str) -> list[str]:
        configured = [
            recipient
            for listener in listeners_for("mattermost", direction="inbound")
            if channel_id in listener["channel_ids"] or (
                listener.get("include_direct_messages") and channel_id not in self._channels()
            )
            for recipient in [listener.get("bridge_agent"), *listener["mailbox_recipients"]]
            if recipient
        ]
        if configured:
            return list(dict.fromkeys(configured))
        configured = os.environ.get("MATTERMOST_RELAY_RECIPIENTS", ",".join(DEFAULT_INBOUND_RECIPIENTS))
        return list(dict.fromkeys(item.strip() for item in configured.split(",") if item.strip()))

    def _connect(self) -> None:
        base_url = os.environ["MM_URL"].rstrip("/")
        self.session.headers.update({"Authorization": f"Bearer {os.environ['MM_BOT_TOKEN']}"})
        response = self.session.get(f"{base_url}/api/v4/users/me", timeout=15)
        response.raise_for_status()
        self._bot_user_id = str(response.json()["id"])
        channels = self._channels()
        now_ms = int(time.time() * 1000)
        self._latest_create_at = {channel_id: now_ms for channel_id in channels}
        self.status.update({"connected": True, "channels": channels, "lastError": None})

    def _refresh_direct_channels(self, base_url: str) -> None:
        if time.time() < self._next_dm_refresh:
            return
        response = self.session.get(f"{base_url}/api/v4/users/{self._bot_user_id}/channels", timeout=15)
        response.raise_for_status()
        direct = [str(item["id"]) for item in response.json() if item.get("type") == "D" and item.get("id")]
        now_ms = int(time.time() * 1000)
        channels = list(dict.fromkeys([*self._channels(), *direct]))
        for channel_id in channels:
            self._latest_create_at.setdefault(channel_id, now_ms)
        self.status["channels"] = channels
        self._next_dm_refresh = time.time() + 30

    def _download_attachments(self, base_url: str, post: dict[str, Any]) -> list[dict[str, Any]]:
        mailbox = _mailbox_module()
        attachments: list[dict[str, Any]] = []
        for file_id in post.get("file_ids") or []:
            info_response = self.session.get(f"{base_url}/api/v4/files/{file_id}/info", timeout=15)
            info_response.raise_for_status()
            info = info_response.json()
            name = Path(str(info.get("name") or file_id)).name
            target_dir = mailbox.mailbox_dir() / "attachments" / str(post["id"])
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / name
            if not target.exists():
                file_response = self.session.get(f"{base_url}/api/v4/files/{file_id}", timeout=30)
                file_response.raise_for_status()
                target.write_bytes(file_response.content)
            content = target.read_bytes()
            attachments.append({
                "path": str(target),
                "name": name,
                "mime_type": str(info.get("mime_type") or mimetypes.guess_type(name)[0] or "application/octet-stream"),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "mattermost_file_id": str(file_id),
            })
        return attachments

    def _poll_inbound(self, base_url: str) -> None:
        mailbox = _mailbox_module()
        for channel_id in self.status["channels"]:
            response = self.session.get(
                f"{base_url}/api/v4/channels/{channel_id}/posts",
                params={"page": 0, "per_page": 200, "since": self._latest_create_at.get(channel_id, 0)},
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
            posts = payload.get("posts", {})
            for post_id in reversed(payload.get("order", [])):
                post = posts.get(post_id) or {}
                created = int(post.get("create_at", 0))
                self._latest_create_at[channel_id] = max(self._latest_create_at.get(channel_id, 0), created + 1)
                if str(post.get("user_id", "")) == self._bot_user_id:
                    continue
                source_id = str(post.get("id", ""))
                if self._source_seen(mailbox.mailbox_dir() / "messages.jsonl", source_id):
                    continue
                attachments = self._download_attachments(base_url, post)
                matching = next((
                    item for item in listeners_for("mattermost", direction="inbound")
                    if channel_id in item["channel_ids"]
                ), {})
                origin_fields = with_origin(
                    {"author": str(post.get("user_id", "")), "attachments": attachments},
                    adapter="mattermost",
                    listener_id=str(matching.get("id") or "mattermost-default"),
                    source_id=source_id,
                    channel_id=channel_id,
                    presence_id=str(matching.get("presence_id") or ""),
                )
                DeliveryLedger(mailbox.mailbox_dir()).claim(
                    origin_fields,
                    endpoint_id(
                        "mattermost",
                        listener_id=str(matching.get("id") or "mattermost-default"),
                        channel_id=channel_id,
                        presence_id=str(matching.get("presence_id") or ""),
                    ),
                )
                for recipient in self._inbound_recipients(channel_id):
                    mailbox.send(
                        recipient,
                        str(post.get("message", "")),
                        sender="mattermost",
                        message_type="mattermost_message",
                        channel_id=channel_id,
                        channel_type="mattermost",
                        source_id=source_id,
                        thread_id=str(post.get("root_id", "") or "") or None,
                        extra_fields=origin_fields,
                    )
                dispatch_routes(mailbox,
                                listener_id=str(matching.get("id") or "mattermost-default"),
                                channel_id=channel_id,
                                message={**origin_fields, "text": str(post.get("message", "")),
                                         "source_id": source_id,
                                         "thread_id": str(post.get("root_id", "") or "") or None})

    @staticmethod
    def _source_seen(messages_path: Path, source_id: str) -> bool:
        if not source_id or not messages_path.exists():
            return False
        import json

        with messages_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    if record.get("source_id") == source_id and record.get("to") in DEFAULT_INBOUND_RECIPIENTS:
                        return True
                except json.JSONDecodeError:
                    continue
        return False

    def _upload_attachments(self, base_url: str, channel_id: str, records: list[dict[str, Any]]) -> list[str]:
        file_ids: list[str] = []
        for record in records:
            path = Path(str(record.get("path", ""))).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(f"Attachment does not exist: {path}")
            with path.open("rb") as stream:
                response = self.session.post(
                    f"{base_url}/api/v4/files",
                    data={"channel_id": channel_id},
                    files={"files": (path.name, stream, record.get("mime_type") or "application/octet-stream")},
                    timeout=60,
                )
            response.raise_for_status()
            file_ids.extend(str(item["id"]) for item in response.json().get("file_infos", []))
        return file_ids

    def _send_outbound(self, base_url: str) -> None:
        mailbox = _mailbox_module()
        for message in mailbox.receive(RELAY_RECIPIENT):
            channel_id = str(message.get("channel_id") or os.environ["MM_CHANNEL_ID"])
            payload: dict[str, Any] = {
                "channel_id": channel_id,
                "message": str(message.get("text", "")),
                "file_ids": self._upload_attachments(base_url, channel_id, list(message.get("attachments") or [])),
            }
            root_id = str(message.get("root_id") or message.get("thread_id") or "")
            if root_id:
                payload["root_id"] = root_id
            response = self.session.post(f"{base_url}/api/v4/posts", json=payload, timeout=15)
            response.raise_for_status()

    def cycle(self) -> None:
        if not self._bot_user_id:
            self._connect()
        base_url = os.environ["MM_URL"].rstrip("/")
        self._refresh_direct_channels(base_url)
        self._poll_inbound(base_url)
        self._send_outbound(base_url)
        self.status.update({"connected": True, "lastCycleAt": time.time(), "lastError": None})

    def run_forever(self) -> None:
        if not self.configure():
            raise RuntimeError(str(self.status["lastError"] or "Mattermost relay is disabled"))
        self.stop_requested = False
        self.status["running"] = True
        while not self.stop_requested:
            try:
                self.cycle()
            except Exception as error:  # keep transport alive across transient network failures
                self.status.update({"connected": False, "lastError": str(error)})
                LOGGER.exception("Mattermost relay cycle failed")
            self.sleep(1)
        self.status["running"] = False
