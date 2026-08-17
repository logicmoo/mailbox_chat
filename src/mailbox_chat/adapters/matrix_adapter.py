"""Matrix Client-Server API adapter compatible with Element and other clients."""

from __future__ import annotations

import json
import mimetypes
import os
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from ..delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from ..endpoint_address import subscription_recipients
from ..connector_registry import connectors_for


class MatrixAdapter:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.connectors: list[dict[str, Any]] = []
        self.user_ids: dict[str, str] = {}
        self.next_batch: dict[str, str] = {}
        self.status: dict[str, Any] = {
            "enabled": False, "connected": False, "lastError": None, "rooms": [],
        }

    @staticmethod
    def _value(connector: dict[str, Any], key: str, default_env: str) -> str:
        value = str(connector.get(key) or f"${default_env}")
        return os.environ.get(value[1:], "").strip() if value.startswith("$") else value.strip()

    def _homeserver(self, connector: dict[str, Any]) -> str:
        value = self._value(connector, "homeserver", "MATRIX_HOMESERVER")
        if not value:
            raise ValueError(f"Matrix connector {connector['id']} requires homeserver")
        return value.rstrip("/")

    def _headers(self, connector: dict[str, Any]) -> dict[str, str]:
        token_name = str(connector.get("token_env") or "MATRIX_ACCESS_TOKEN")
        token = os.environ.get(token_name, "").strip()
        if not token:
            raise ValueError(f"Matrix connector {connector['id']} requires an access token")
        return {"Authorization": f"Bearer {token}"}

    def configure(self) -> bool:
        self.connectors = connectors_for("matrix")
        errors: list[str] = []
        for connector in self.connectors:
            try:
                self._homeserver(connector)
                self._headers(connector)
            except ValueError as error:
                errors.append(str(error))
        self.status.update({
            "enabled": bool(self.connectors) and not errors,
            "connected": False,
            "rooms": list(dict.fromkeys(room for item in self.connectors for room in item.get("channel_ids", []))),
            "lastError": "; ".join(errors) or None,
        })
        return bool(self.status["enabled"])

    def connect(self) -> None:
        for connector in self.connectors:
            base = self._homeserver(connector)
            response = self.session.get(
                f"{base}/_matrix/client/v3/account/whoami", headers=self._headers(connector), timeout=15,
            )
            response.raise_for_status()
            self.user_ids[connector["id"]] = str(response.json()["user_id"])
            initial = self.session.get(
                f"{base}/_matrix/client/v3/sync", headers=self._headers(connector),
                params={"timeout": 0, "filter": json.dumps({"room": {"timeline": {"limit": 0}}})},
                timeout=15,
            )
            initial.raise_for_status()
            self.next_batch[connector["id"]] = str(initial.json()["next_batch"])
        self.status.update({"connected": True, "lastError": None})

    def close(self) -> None:
        self.user_ids.clear()
        self.next_batch.clear()
        self.status["connected"] = False

    def cycle(self, mailbox: Any) -> None:
        if not self.status["enabled"]:
            return
        if not self.status["connected"]:
            self.connect()
        for connector in self.connectors:
            if connector["direction"] in {"inbound", "bidirectional"}:
                self._sync_connector(mailbox, connector)
        self.status.update({"connected": True, "lastError": None, "lastCycleAt": time.time()})

    def _sync_connector(self, mailbox: Any, connector: dict[str, Any]) -> None:
        base = self._homeserver(connector)
        response = self.session.get(
            f"{base}/_matrix/client/v3/sync", headers=self._headers(connector),
            params={"since": self.next_batch[connector["id"]], "timeout": 0}, timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        self.next_batch[connector["id"]] = str(payload["next_batch"])
        allowed_rooms = set(connector.get("channel_ids", []))
        for room_id, room in (payload.get("rooms", {}).get("join", {}) or {}).items():
            if allowed_rooms and room_id not in allowed_rooms:
                continue
            for event in room.get("timeline", {}).get("events", []):
                self._handle_event(mailbox, connector, room_id, event)

    def _handle_event(self, mailbox: Any, connector: dict[str, Any], room_id: str,
                      event: dict[str, Any]) -> None:
        if event.get("type") != "m.room.message" or event.get("sender") == self.user_ids.get(connector["id"]):
            return
        content = event.get("content") or {}
        source_id = str(event.get("event_id") or "")
        if not source_id:
            return
        origin = with_origin(
            {"author": str(event.get("sender") or ""), "matrix_msgtype": str(content.get("msgtype") or "")},
            adapter="matrix", connector_id=connector["id"], source_id=source_id,
            channel_id=room_id, presence_id=str(connector.get("presence_id") or ""),
        )
        if not DeliveryLedger(mailbox.mailbox_dir()).claim(origin, endpoint_id(
            "matrix", connector_id=connector["id"], channel_id=room_id,
            presence_id=str(connector.get("presence_id") or ""),
        )):
            return
        relates = content.get("m.relates_to") or {}
        recipients = list(dict.fromkeys([
            *([connector["bridge_agent"]] if connector.get("bridge_agent") else []),
            *connector.get("mailbox_recipients", []),
            *subscription_recipients("matrix", connector, room_id),
        ]))
        for recipient in recipients:
            mailbox.send(
                recipient, str(content.get("body") or ""), sender=f"matrix:{event.get('sender') or 'user'}",
                message_type="matrix_message", channel_id=room_id, channel_type="matrix",
                source_id=source_id,
                thread_id=str(relates.get("event_id") or "") or None,
                extra_fields=origin,
            )

    def _connector_for(self, message: dict[str, Any]) -> dict[str, Any]:
        connector_id = str(message.get("connector_id") or "")
        connector = next((item for item in self.connectors if item["id"] == connector_id), None)
        if connector is None:
            eligible = [item for item in self.connectors if item["direction"] in {"outbound", "bidirectional"}]
            connector = eligible[0] if len(eligible) == 1 else None
        if connector is None:
            raise ValueError("Matrix outbound message requires an unambiguous connector_id")
        return connector

    def _send_event(self, connector: dict[str, Any], room_id: str, event_type: str,
                    content: dict[str, Any]) -> None:
        base = self._homeserver(connector)
        encoded_room = quote(room_id, safe="")
        transaction_id = uuid.uuid4().hex
        response = self.session.put(
            f"{base}/_matrix/client/v3/rooms/{encoded_room}/send/{event_type}/{transaction_id}",
            headers=self._headers(connector), json=content, timeout=30,
        )
        response.raise_for_status()

    def send_message(self, message: dict[str, Any]) -> None:
        connector = self._connector_for(message)
        room_id = str(message.get("channel_id") or "").strip()
        if not room_id:
            raise ValueError("Matrix outbound message requires channel_id (room ID)")
        body = str(message.get("text") or "")
        if body:
            content: dict[str, Any] = {"msgtype": "m.text", "body": body}
            thread_id = str(message.get("thread_id") or message.get("root_id") or "")
            if thread_id:
                content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id,
                                           "is_falling_back": True}
            self._send_event(connector, room_id, "m.room.message", content)
        for record in message.get("attachments") or []:
            path = Path(str(record.get("path") or "")).expanduser().resolve(strict=True)
            mime_type = str(record.get("mime_type") or mimetypes.guess_type(path.name)[0]
                            or "application/octet-stream")
            base = self._homeserver(connector)
            with path.open("rb") as stream:
                upload = self.session.post(
                    f"{base}/_matrix/media/v3/upload", headers={**self._headers(connector),
                    "Content-Type": mime_type}, params={"filename": path.name}, data=stream, timeout=60,
                )
            upload.raise_for_status()
            msgtype = "m.image" if mime_type.startswith("image/") else "m.file"
            self._send_event(connector, room_id, "m.room.message", {
                "msgtype": msgtype, "body": path.name, "url": str(upload.json()["content_uri"]),
                "info": {"mimetype": mime_type, "size": path.stat().st_size},
            })
