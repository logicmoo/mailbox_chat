"""Transport-neutral channel relay with pluggable communication adapters."""

from __future__ import annotations

from typing import Any

from .mattermost_adapter import MattermostRelay, RELAY_PORT
from .irc_adapter import IrcAdapter
from .discord_adapter import DiscordAdapter
from .matrix_adapter import MatrixAdapter
from .slack_adapter import SlackAdapter
from .telegram_adapter import TelegramAdapter
from .facebook_messenger_adapter import FacebookMessengerAdapter
from .whatsapp_adapter import WhatsAppAdapter
from .viber_adapter import ViberAdapter
from .delivery_ledger import DeliveryLedger, endpoint_id, origin_id


RELAY_RECIPIENT = "channel-relay"
SUPPORTED_CHANNEL_TYPES = ("mattermost", "irc", "discord", "matrix", "slack", "telegram",
                           "whatsapp", "facebook_messenger", "viber")
PLANNED_CHANNEL_TYPES = ("line", "discourse")
ADAPTER_CAPABILITIES = {
    "mattermost": {"presence": "single", "threads": True, "attachments": True},
    "irc": {"presence": "single", "threads": False, "attachments": False},
    "discord": {"presence": "single", "threads": True, "attachments": True},
    "matrix": {"presence": "single", "threads": True, "attachments": True},
    "discourse": {"presence": "single", "threads": True, "attachments": True,
                  "interaction": "forum", "implemented": False},
    "slack": {"presence": "multiple", "threads": True, "attachments": True},
    "telegram": {"presence": "single", "threads": True, "attachments": True},
    "whatsapp": {"presence": "single", "threads": False, "attachments": True,
                 "interaction": "business"},
    "facebook_messenger": {"presence": "single", "threads": False, "attachments": True,
                           "interaction": "page"},
    "viber": {"presence": "single", "threads": False, "attachments": True,
              "interaction": "bot"},
}


class ChannelRelay(MattermostRelay):
    """Coordinate installed adapters around one durable outbound mailbox."""

    def __init__(self, *args: Any, irc_adapter: IrcAdapter | None = None,
                 discord_adapter: DiscordAdapter | None = None,
                 matrix_adapter: MatrixAdapter | None = None,
                 slack_adapter: SlackAdapter | None = None,
                 telegram_adapter: TelegramAdapter | None = None,
                 viber_adapter: ViberAdapter | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.irc = irc_adapter or IrcAdapter()
        self.discord = discord_adapter or DiscordAdapter()
        self.matrix = matrix_adapter or MatrixAdapter()
        self.slack = slack_adapter or SlackAdapter()
        self.telegram = telegram_adapter or TelegramAdapter()
        self.whatsapp = WhatsAppAdapter()
        self.facebook_messenger = FacebookMessengerAdapter()
        self.viber = viber_adapter or ViberAdapter()
        self.mattermost_enabled = False
        self.delivery_ledger = DeliveryLedger(self._mailbox().mailbox_dir())

    def configure(self) -> bool:
        self.mattermost_enabled = super().configure()
        irc_enabled = self.irc.configure()
        discord_enabled = self.discord.configure()
        matrix_enabled = self.matrix.configure()
        slack_enabled = self.slack.configure()
        telegram_enabled = self.telegram.configure()
        whatsapp_enabled = self.whatsapp.configure()
        facebook_enabled = self.facebook_messenger.configure()
        viber_enabled = self.viber.configure()
        self.status["enabled"] = (self.mattermost_enabled or irc_enabled or discord_enabled
                                  or matrix_enabled or slack_enabled or telegram_enabled
                                  or whatsapp_enabled or facebook_enabled or viber_enabled)
        self.status["adapters"] = {"mattermost": dict(self.status), "irc": dict(self.irc.status),
                                   "discord": dict(self.discord.status), "matrix": dict(self.matrix.status),
                                   "slack": dict(self.slack.status), "telegram": dict(self.telegram.status),
                                   "whatsapp": dict(self.whatsapp.status),
                                   "facebook_messenger": dict(self.facebook_messenger.status),
                                   "viber": dict(self.viber.status)}
        return bool(self.status["enabled"])

    def stop(self) -> None:
        self.irc.close()
        self.discord.close()
        self.matrix.close()
        self.slack.close()
        self.telegram.close()
        self.whatsapp.close()
        self.facebook_messenger.close()
        self.viber.close()
        super().stop()

    def reset_after_failure(self) -> None:
        """Discard transient connections while keeping the daemon alive."""
        self.irc.close()
        self.discord.close()
        self.matrix.close()
        self.slack.close()
        self.telegram.close()
        self.whatsapp.close()
        self.facebook_messenger.close()
        self.viber.close()
        self._bot_user_id = ""
        self._latest_create_at.clear()
        self._next_dm_refresh = 0.0
        self.status["connected"] = False

    def cycle(self) -> None:
        mailbox = self._mailbox()
        if self.mattermost_enabled:
            if not self._bot_user_id:
                self._connect()
            base_url = __import__("os").environ["MM_URL"].rstrip("/")
            self._refresh_direct_channels(base_url)
            self._poll_inbound(base_url)
        self.irc.cycle(mailbox)
        self.discord.cycle(mailbox)
        self.matrix.cycle(mailbox)
        self.slack.cycle(mailbox)
        self.telegram.cycle(mailbox)
        self.whatsapp.cycle(mailbox)
        self.facebook_messenger.cycle(mailbox)
        self.viber.cycle(mailbox)
        self._dispatch_outbound()
        self.status.update({
            "connected": bool((self.mattermost_enabled and self._bot_user_id) or self.irc.status["connected"]
                              or self.discord.status["connected"] or self.matrix.status["connected"]
                              or self.slack.status["connected"] or self.telegram.status["connected"]
                              or self.whatsapp.status["connected"]
                              or self.facebook_messenger.status["connected"]
                              or self.viber.status["connected"]),
            "lastCycleAt": __import__("time").time(),
            "lastError": None,
            "adapters": {
                "mattermost": {"enabled": self.mattermost_enabled, "connected": bool(self._bot_user_id)},
                "irc": dict(self.irc.status),
                "discord": dict(self.discord.status),
                "matrix": dict(self.matrix.status),
                "slack": dict(self.slack.status),
                "telegram": dict(self.telegram.status),
                "whatsapp": dict(self.whatsapp.status),
                "facebook_messenger": dict(self.facebook_messenger.status),
                "viber": dict(self.viber.status),
            },
        })

    def _dispatch_outbound(self) -> None:
        mailbox = self._mailbox()
        for message in mailbox.receive(RELAY_RECIPIENT):
            channel_type = str(message.get("channel_type") or "mattermost").lower()
            destination = endpoint_id(
                channel_type,
                listener_id=str(message.get("listener_id") or ""),
                channel_id=str(message.get("channel_id") or ""),
                presence_id=str(message.get("presence_id") or ""),
            )
            if not self.delivery_ledger.claim(message, destination):
                mailbox.send(
                    str(message.get("from") or "local-agent"),
                    f"Suppressed duplicate relay to {destination}",
                    sender=RELAY_RECIPIENT,
                    message_type="channel_delivery_suppressed",
                    extra_fields={
                        "origin_id": origin_id(message),
                        "request_id": message.get("id"),
                        "destination_endpoint": destination,
                    },
                )
                continue
            try:
                if channel_type == "mattermost" and self.mattermost_enabled:
                    self._send_mattermost_message(__import__("os").environ["MM_URL"].rstrip("/"), message)
                elif channel_type == "irc" and self.irc.status["enabled"]:
                    self.irc.send_message(message)
                elif channel_type == "discord" and self.discord.status["enabled"]:
                    self.discord.send_message(message)
                elif channel_type == "matrix" and self.matrix.status["enabled"]:
                    self.matrix.send_message(message)
                elif channel_type == "slack" and self.slack.status["enabled"]:
                    self.slack.send_message(message)
                elif channel_type == "telegram" and self.telegram.status["enabled"]:
                    self.telegram.send_message(message)
                elif channel_type == "whatsapp" and self.whatsapp.status["enabled"]:
                    self.whatsapp.send_message(message)
                elif channel_type == "facebook_messenger" and self.facebook_messenger.status["enabled"]:
                    self.facebook_messenger.send_message(message)
                elif channel_type == "viber" and self.viber.status["enabled"]:
                    self.viber.send_message(message)
                else:
                    raise RuntimeError(f"Channel adapter is not enabled: {channel_type}")
            except Exception as error:
                self.delivery_ledger.release(message, destination)
                mailbox.send(
                    str(message.get("from") or "local-agent"),
                    str(error),
                    sender=RELAY_RECIPIENT,
                    message_type="channel_delivery_failed",
                    extra_fields={
                        "channel_type": channel_type,
                        "request_id": message.get("id"),
                        "workflow_run_id": message.get("workflow_run_id"),
                    },
                )

    @staticmethod
    def _mailbox():
        from . import agent_mailbox

        return agent_mailbox

    def _send_mattermost_message(self, base_url: str, message: dict[str, Any]) -> None:
        channel_id = str(message.get("channel_id") or __import__("os").environ["MM_CHANNEL_ID"])
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


__all__ = [
    "ADAPTER_CAPABILITIES", "ChannelRelay", "PLANNED_CHANNEL_TYPES", "RELAY_PORT", "RELAY_RECIPIENT",
    "SUPPORTED_CHANNEL_TYPES",
]
