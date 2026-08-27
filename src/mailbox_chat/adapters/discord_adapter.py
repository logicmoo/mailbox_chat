"""Discord REST adapter backed exclusively by the durable mailbox layer."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

from ..delivery_ledger import DeliveryLedger, endpoint_id, with_origin
from ..endpoint_address import endpoint_instance, subscription_recipients
from ..connector_registry import connectors_for
from ..identifier_directory import IdentifierDirectory


DISCORD_API = "https://discord.com/api/v10"
DEFAULT_USER_REFRESH_SECONDS = 300
DEFAULT_CHANNEL_DISCOVERY_SECONDS = 600
DISCOVERABLE_CHANNEL_TYPES = {0, 5}
DEFAULT_CHANNEL_DISCOVERY_GUILD_LIMIT = 1
DEFAULT_CHANNEL_DISCOVERY_CHANNEL_LIMIT = 64
DEFAULT_CHANNEL_DISCOVERY_TIMEOUT_SECONDS = 8.0
DEFAULT_MAILBOX_POLL_BATCH = 12


class DiscordAdapter:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.connectors: list[dict[str, Any]] = []
        self.bot_ids: dict[str, str] = {}
        self.latest_ids: dict[tuple[str, str], str] = {}
        self.user_refresh_due: dict[tuple[str, str], float] = {}
        self.runtime_mailboxes: dict[str, list[str]] = {}
        self.channel_discovery_due: dict[str, float] = {}
        self.poll_offsets: dict[str, int] = {}
        self.status: dict[str, Any] = {
            "enabled": False, "connected": False, "lastError": None, "mailboxes": [],
        }

    @staticmethod
    def _token(connector: dict[str, Any]) -> str:
        token_env = str(connector.get("token_env") or "DISCORD_BOT_TOKEN")
        return os.environ.get(token_env, "").strip()

    def _headers(self, connector: dict[str, Any]) -> dict[str, str]:
        token = self._token(connector)
        if not token:
            raise ValueError(f"Discord connector {connector['id']} is missing {connector.get('token_env') or 'DISCORD_BOT_TOKEN'}")
        return {"Authorization": f"Bot {token}"}

    def configure(self) -> bool:
        self.connectors = connectors_for("discord")
        self.user_refresh_due.clear()
        self.runtime_mailboxes.clear()
        self.channel_discovery_due.clear()
        self.poll_offsets.clear()
        for connector in self.connectors:
            connector_id = str(connector.get("id") or "").strip()
            if not connector_id:
                continue
            self.runtime_mailboxes[connector_id] = [
                str(mailbox_id).strip()
                for mailbox_id in connector.get("mailbox_ids", [])
                if str(mailbox_id).strip()
            ]
            self.channel_discovery_due[connector_id] = 0.0
        missing = [item["id"] for item in self.connectors if not self._token(item)]
        self.status.update({
            "enabled": bool(self.connectors) and not missing,
            "connected": False,
            "mailboxes": list(dict.fromkeys(
                mailbox
                for connector_id in self.runtime_mailboxes
                for mailbox in self.runtime_mailboxes.get(connector_id, [])
            )),
            "lastError": f"Missing Discord tokens for: {', '.join(missing)}" if missing else None,
        })
        return bool(self.status["enabled"])

    def connect(self) -> None:
        for connector in self.connectors:
            connector_id = str(connector.get("id") or "").strip()
            if not connector_id:
                continue
            response = self.session.get(f"{DISCORD_API}/users/@me", headers=self._headers(connector), timeout=15)
            response.raise_for_status()
            self.bot_ids[connector_id] = str(response.json()["id"])
            for mailbox_id in self.runtime_mailboxes.get(connector_id, []):
                self.latest_ids.setdefault((connector_id, mailbox_id), "0")
                self.user_refresh_due[(connector_id, mailbox_id)] = 0.0
            self.channel_discovery_due[connector_id] = 0.0
        self.status["mailboxes"] = list(dict.fromkeys(
            mailbox for mailboxes in self.runtime_mailboxes.values() for mailbox in mailboxes
        ))
        self.status.update({"connected": True, "lastError": None})

    def close(self) -> None:
        self.bot_ids.clear()
        self.status["connected"] = False

    def cycle(self, mailbox: Any) -> None:
        if not self.status["enabled"]:
            return
        if not self.status["connected"]:
            self.connect()
        for connector in self.connectors:
            if connector["direction"] not in {"inbound", "bidirectional"}:
                continue
            connector_mailboxes = self._mailboxes_for_connector(connector)
            for mailbox_id in self._mailbox_poll_batch(connector, connector_mailboxes):
                self._poll_mailbox(mailbox, connector, mailbox_id)
        self.status["mailboxes"] = list(dict.fromkeys(
            mailbox_id for mailboxes in self.runtime_mailboxes.values() for mailbox_id in mailboxes
        ))
        self.status.update({"connected": True, "lastError": None, "lastCycleAt": time.time()})

    def _poll_mailbox(self, mailbox: Any, connector: dict[str, Any], mailbox_id: str) -> None:
        directory = IdentifierDirectory(mailbox.mailbox_dir())
        system = f"discord/{endpoint_instance('discord', connector)}"
        connector_id = str(connector.get("id") or "").strip()
        key = (connector_id, mailbox_id)
        now = time.time()
        if now >= self.user_refresh_due.get(key, 0.0):
            self._refresh_mailbox_users(directory, system, connector, mailbox_id)
            self.user_refresh_due[key] = now + self._refresh_interval_seconds(connector)
        response = self.session.get(
            f"{DISCORD_API}/channels/{mailbox_id}/messages",
            headers=self._headers(connector),
            params={"limit": 100, "after": self.latest_ids.get((connector_id, mailbox_id), "0")},
            timeout=15,
        )
        response.raise_for_status()
        for message in reversed(response.json()):
            source_id = str(message["id"])
            self.latest_ids[(connector_id, mailbox_id)] = max(
                source_id, self.latest_ids.get((connector_id, mailbox_id), "0"), key=int,
            )
            author, author_id, author_name, guild_id = self._remember_message_entities(
                directory, system, mailbox_id, message,
            )
            if str(author.get("id") or "") == self.bot_ids.get(connector_id):
                continue
            origin = with_origin(
                {"author": author_name,
                 "author_id": author_id,
                 "discord_guild_id": guild_id},
                adapter="discord", connector_id=connector["id"], source_id=source_id,
                mailbox_id=mailbox_id, presence_id=str(connector.get("presence_id") or ""),
            )
            claimed = DeliveryLedger(mailbox.mailbox_dir()).claim(origin, endpoint_id(
                "discord", connector_id=connector["id"], mailbox_id=mailbox_id,
                presence_id=str(connector.get("presence_id") or ""),
            ))
            if not claimed:
                continue
            recipients = list(dict.fromkeys([
                *([connector["bridge_agent"]] if connector.get("bridge_agent") else []),
                *connector.get("mailbox_recipients", []),
                *subscription_recipients("discord", connector, mailbox_id),
            ]))
            for recipient in recipients:
                mailbox.send(
                    recipient, str(message.get("content") or ""),
                    sender=f"discord:{author.get('username') or author.get('id') or 'user'}",
                    message_type="discord_message", mailbox_id=mailbox_id,
                    mailbox_type="discord", source_id=source_id,
                    extra_fields=origin,
                )

    @staticmethod
    def _refresh_interval_seconds(connector: dict[str, Any]) -> float:
        configured = connector.get("user_refresh_seconds")
        if configured is None:
            return float(DEFAULT_USER_REFRESH_SECONDS)
        try:
            parsed = float(configured)
        except (TypeError, ValueError):
            return float(DEFAULT_USER_REFRESH_SECONDS)
        return parsed if parsed > 0 else float(DEFAULT_USER_REFRESH_SECONDS)

    @staticmethod
    def _channel_discovery_interval_seconds(connector: dict[str, Any]) -> float:
        configured = connector.get("channel_discovery_seconds")
        if configured is None:
            return float(DEFAULT_CHANNEL_DISCOVERY_SECONDS)
        try:
            parsed = float(configured)
        except (TypeError, ValueError):
            return float(DEFAULT_CHANNEL_DISCOVERY_SECONDS)
        return parsed if parsed > 0 else float(DEFAULT_CHANNEL_DISCOVERY_SECONDS)

    @staticmethod
    def _auto_discover_channels_enabled(connector: dict[str, Any]) -> bool:
        raw = connector.get("auto_discover_channels", True)
        return str(raw).strip().lower() not in {"0", "false", "no"}

    @staticmethod
    def _channel_discovery_guild_limit(connector: dict[str, Any]) -> int:
        raw = connector.get("channel_discovery_guild_limit", DEFAULT_CHANNEL_DISCOVERY_GUILD_LIMIT)
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_CHANNEL_DISCOVERY_GUILD_LIMIT
        return max(1, parsed)

    @staticmethod
    def _channel_discovery_channel_limit(connector: dict[str, Any]) -> int:
        raw = connector.get("channel_discovery_channel_limit", DEFAULT_CHANNEL_DISCOVERY_CHANNEL_LIMIT)
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_CHANNEL_DISCOVERY_CHANNEL_LIMIT
        return max(1, parsed)

    @staticmethod
    def _channel_discovery_timeout_seconds(connector: dict[str, Any]) -> float:
        raw = connector.get("channel_discovery_timeout_seconds", DEFAULT_CHANNEL_DISCOVERY_TIMEOUT_SECONDS)
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            return DEFAULT_CHANNEL_DISCOVERY_TIMEOUT_SECONDS
        return parsed if parsed > 0 else DEFAULT_CHANNEL_DISCOVERY_TIMEOUT_SECONDS

    def _mailboxes_for_connector(self, connector: dict[str, Any], *, force_refresh: bool = False) -> list[str]:
        connector_id = str(connector.get("id") or "").strip()
        configured = [
            str(mailbox_id).strip() for mailbox_id in connector.get("mailbox_ids", [])
            if str(mailbox_id).strip()
        ]
        if not connector_id:
            return configured
        self.runtime_mailboxes.setdefault(connector_id, list(configured))
        self.channel_discovery_due.setdefault(connector_id, 0.0)
        if not self._auto_discover_channels_enabled(connector):
            merged = list(dict.fromkeys(configured))
            self.runtime_mailboxes[connector_id] = merged
            return merged
        now = time.time()
        if force_refresh or now >= self.channel_discovery_due.get(connector_id, 0.0):
            discovered = self._discover_channel_mailboxes(connector)
            merged = list(dict.fromkeys([*configured, *discovered]))
            self.runtime_mailboxes[connector_id] = merged
            self.channel_discovery_due[connector_id] = (
                now + self._channel_discovery_interval_seconds(connector)
            )
        return self.runtime_mailboxes.get(connector_id, list(configured))

    @staticmethod
    def _mailbox_poll_batch_size(connector: dict[str, Any]) -> int:
        raw = connector.get("mailbox_poll_batch", DEFAULT_MAILBOX_POLL_BATCH)
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_MAILBOX_POLL_BATCH
        return max(1, parsed)

    def _mailbox_poll_batch(self, connector: dict[str, Any], mailbox_ids: list[str]) -> list[str]:
        if not mailbox_ids:
            return []
        connector_id = str(connector.get("id") or "").strip()
        if not connector_id:
            return list(mailbox_ids)
        batch_size = self._mailbox_poll_batch_size(connector)
        if batch_size >= len(mailbox_ids):
            return list(mailbox_ids)
        start = self.poll_offsets.get(connector_id, 0) % len(mailbox_ids)
        ordered = mailbox_ids[start:] + mailbox_ids[:start]
        self.poll_offsets[connector_id] = (start + batch_size) % len(mailbox_ids)
        return ordered[:batch_size]

    def _discover_channel_mailboxes(self, connector: dict[str, Any]) -> list[str]:
        headers = self._headers(connector)
        results: list[str] = []
        timeout = self._channel_discovery_timeout_seconds(connector)
        guild_limit = self._channel_discovery_guild_limit(connector)
        channel_limit = self._channel_discovery_channel_limit(connector)
        try:
            guilds_response = self.session.get(
                f"{DISCORD_API}/users/@me/guilds",
                headers=headers,
                params={"limit": 200},
                timeout=timeout,
            )
            guilds_response.raise_for_status()
            guilds = guilds_response.json()
        except Exception:
            return results
        if not isinstance(guilds, list):
            return results
        for guild in guilds[:guild_limit]:
            if not isinstance(guild, dict):
                continue
            guild_id = str(guild.get("id") or "").strip()
            if not guild_id:
                continue
            try:
                channels_response = self.session.get(
                    f"{DISCORD_API}/guilds/{guild_id}/channels",
                    headers=headers,
                    timeout=timeout,
                )
                channels_response.raise_for_status()
                channels = channels_response.json()
            except Exception:
                continue
            if not isinstance(channels, list):
                continue
            for channel in channels:
                if not isinstance(channel, dict):
                    continue
                channel_id = str(channel.get("id") or "").strip()
                if not channel_id:
                    continue
                channel_type = channel.get("type")
                if isinstance(channel_type, (int, float)):
                    if int(channel_type) not in DISCOVERABLE_CHANNEL_TYPES:
                        continue
                results.append(channel_id)
                if len(results) >= channel_limit:
                    return list(dict.fromkeys(results))
        return list(dict.fromkeys(results))

    def _refresh_mailbox_users(
        self,
        directory: IdentifierDirectory,
        system: str,
        connector: dict[str, Any],
        mailbox_id: str,
    ) -> None:
        response = self.session.get(
            f"{DISCORD_API}/channels/{mailbox_id}/messages",
            headers=self._headers(connector),
            params={"limit": 100},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Discord message listing response was not an array")
        for message in payload:
            if isinstance(message, dict):
                self._remember_message_entities(directory, system, mailbox_id, message)

    @staticmethod
    def _remember_message_entities(
        directory: IdentifierDirectory,
        system: str,
        mailbox_id: str,
        message: dict[str, Any],
    ) -> tuple[dict[str, Any], str, str, str]:
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        author_id = str(author.get("id") or "")
        author_name = str(author.get("username") or author_id or "")
        guild_id = str(message.get("guild_id") or "")
        if author_id:
            directory.remember(
                author_id,
                author_name,
                system=system,
                kind="user",
                metadata={
                    "mailbox": mailbox_id,
                    "mailbox_id": mailbox_id,
                    "author": author,
                },
            )
        if guild_id:
            directory.remember(
                guild_id,
                guild_id,
                system=system,
                kind="guild",
                metadata={"guild_id": guild_id},
            )
        directory.remember(
            mailbox_id,
            mailbox_id,
            system=system,
            kind="mailbox",
            metadata={"mailbox_id": mailbox_id, "guild_id": guild_id},
        )
        return author, author_id, author_name, guild_id

    def send_message(self, message: dict[str, Any]) -> None:
        connector_id = str(message.get("connector_id") or "")
        connector = next((item for item in self.connectors if item["id"] == connector_id), None)
        if connector is None:
            eligible = [item for item in self.connectors if item["direction"] in {"outbound", "bidirectional"}]
            connector = eligible[0] if len(eligible) == 1 else None
        if connector is None:
            raise ValueError("Discord outbound message requires an unambiguous connector_id")
        mailbox_id = str(message.get("mailbox_id") or "").strip()
        if not mailbox_id:
            raise ValueError("Discord outbound message requires mailbox_id")
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
                    f"{DISCORD_API}/channels/{mailbox_id}/messages", headers=self._headers(connector),
                    data={"payload_json": json.dumps({"content": str(message.get("text") or "")})},
                    files=files, timeout=60,
                )
            finally:
                for stream in opened:
                    stream.close()
        else:
            response = self.session.post(
                f"{DISCORD_API}/channels/{mailbox_id}/messages", headers=self._headers(connector),
                json={"content": str(message.get("text") or "")}, timeout=15,
            )
        response.raise_for_status()
