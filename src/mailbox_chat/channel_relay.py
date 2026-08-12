"""Transport-neutral channel relay with pluggable communication adapters."""

from __future__ import annotations

import sys
import os
import time
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
from .line_adapter import LineAdapter
from .discourse_adapter import DiscourseAdapter
from .whatsapp_personal_adapter import WhatsAppPersonalAdapter
from .delivery_ledger import DeliveryLedger, endpoint_id, origin_id
from .listener_registry import listeners_for


RELAY_RECIPIENT = "channel-relay"
SUPPORTED_CHANNEL_TYPES = ("mattermost", "irc", "discord", "matrix", "slack", "telegram",
                           "whatsapp", "whatsapp_personal", "facebook_messenger", "viber", "line",
                           "discourse")
PLANNED_CHANNEL_TYPES: tuple[str, ...] = ()
ADAPTER_CAPABILITIES = {
    "mattermost": {"presence": "single", "threads": True, "attachments": True},
    "irc": {"presence": "single", "threads": False, "attachments": False},
    "discord": {"presence": "single", "threads": True, "attachments": True},
    "matrix": {"presence": "single", "threads": True, "attachments": True},
    "discourse": {"presence": "single", "threads": True, "attachments": True,
                  "interaction": "forum"},
    "slack": {"presence": "multiple", "threads": True, "attachments": True},
    "telegram": {"presence": "single", "threads": True, "attachments": True},
    "whatsapp": {"presence": "single", "threads": False, "attachments": True,
                 "interaction": "business"},
    "whatsapp_personal": {"presence": "single", "threads": True, "attachments": True,
                          "interaction": "unofficial-web-companion", "official": False},
    "facebook_messenger": {"presence": "single", "threads": False, "attachments": True,
                           "interaction": "page"},
    "viber": {"presence": "single", "threads": False, "attachments": True,
              "interaction": "bot"},
    "line": {"presence": "single", "threads": True, "attachments": True,
             "interaction": "user-group-room"},
}


class ChannelRelay(MattermostRelay):
    """Coordinate installed adapters around one durable outbound mailbox."""

    def __init__(self, *args: Any, irc_adapter: IrcAdapter | None = None,
                 discord_adapter: DiscordAdapter | None = None,
                 matrix_adapter: MatrixAdapter | None = None,
                 slack_adapter: SlackAdapter | None = None,
                 telegram_adapter: TelegramAdapter | None = None,
                 viber_adapter: ViberAdapter | None = None, verbose: int = 0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.verbose = verbose
        self.irc = irc_adapter or IrcAdapter()
        self.discord = discord_adapter or DiscordAdapter()
        self.matrix = matrix_adapter or MatrixAdapter()
        self.slack = slack_adapter or SlackAdapter()
        self.telegram = telegram_adapter or TelegramAdapter()
        self.whatsapp = WhatsAppAdapter()
        self.facebook_messenger = FacebookMessengerAdapter()
        self.viber = viber_adapter or ViberAdapter()
        self.line = LineAdapter()
        self.discourse = DiscourseAdapter()
        self.whatsapp_personal = WhatsAppPersonalAdapter()
        self.mattermost_enabled = False
        self.delivery_ledger = DeliveryLedger(self._mailbox().mailbox_dir())
        self._adapter_event_states: dict[str, str] = {}
        self._last_log_message = ""
        self._last_log_repeats = 0
        self._repeat_summary_open = False

    def _log(self, message: str, *, level: int = 1) -> None:
        if self.verbose < level:
            return
        if message == self._last_log_message:
            self._last_log_repeats += 1
            self.status.update({
                "lastVerboseMessage": message,
                "lastVerboseMessageRepeatCount": self._last_log_repeats,
                "lastVerboseMessageAt": time.time(),
            })
            summary = f"[relay] last message repeated {self._last_log_repeats} times"
            if sys.stderr.isatty():
                print(f"\r\x1b[2K{summary}", file=sys.stderr, end="", flush=True)
                self._repeat_summary_open = True
            elif self._last_log_repeats == 1 or self._last_log_repeats % 20 == 0:
                print(summary, file=sys.stderr, flush=True)
            return
        if self._repeat_summary_open:
            print(file=sys.stderr, flush=True)
            self._repeat_summary_open = False
        self._last_log_message = message
        self._last_log_repeats = 0
        self.status.update({
            "lastVerboseMessage": message,
            "lastVerboseMessageRepeatCount": 0,
            "lastVerboseMessageAt": time.time(),
        })
        print(f"[relay] {message}", file=sys.stderr, flush=True)

    @staticmethod
    def _safe_error(error: Exception) -> str:
        message = str(error)
        secret_markers = ("TOKEN", "SECRET", "PASSWORD", "API_KEY")
        for key, value in os.environ.items():
            if value and len(value) >= 4 and any(marker in key.upper() for marker in secret_markers):
                message = message.replace(value, "<redacted>")
        return message

    def _adapter_recipients(self, name: str, adapter: Any) -> list[str]:
        listeners = (listeners_for("mattermost") if name == "mattermost"
                     else list(getattr(adapter, "listeners", [])))
        recipients = [
            recipient
            for listener in listeners
            for recipient in [listener.get("bridge_agent"), *listener.get("mailbox_recipients", [])]
            if recipient
        ]
        if name == "mattermost" and not recipients:
            recipients = self._inbound_recipients(__import__("os").environ.get("MM_CHANNEL_ID", ""))
        return list(dict.fromkeys(recipients))

    def _adapter_context(self, name: str, adapter: Any) -> dict[str, Any]:
        listeners = (listeners_for("mattermost") if name == "mattermost"
                     else list(getattr(adapter, "listeners", [])))
        status = getattr(adapter, "status", {})
        return {
            "adapter": name,
            "listener_ids": [str(item.get("id") or "") for item in listeners if item.get("id")],
            "channel_ids": list(dict.fromkeys(
                str(channel_id) for item in listeners for channel_id in item.get("channel_ids", [])
            )),
            "directions": list(dict.fromkeys(
                str(item.get("direction") or "") for item in listeners if item.get("direction")
            )),
            "enabled": bool(status.get("enabled")),
            "connected": bool(status.get("connected")),
            "retry_policy": {"strategy": "exponential", "initial_seconds": 1, "maximum_seconds": 30},
        }

    def _publish_adapter_event(
        self, name: str, adapter: Any, state: str, text: str,
        *, diagnostic: dict[str, Any] | None = None,
    ) -> None:
        if self._adapter_event_states.get(name) == state:
            return
        self._adapter_event_states[name] = state
        mailbox = self._mailbox()
        for recipient in self._adapter_recipients(name, adapter):
            try:
                mailbox.send(
                    recipient,
                    text,
                    sender=f"local-{name}-server",
                    message_type="chat_server_status",
                    channel_type=name,
                    extra_fields={
                        "adapter": name,
                        "connection_state": state,
                        "local_chat_server": True,
                        "service_context": self._adapter_context(name, adapter),
                        **({"diagnostic": diagnostic} if diagnostic else {}),
                    },
                )
            except (OSError, ValueError) as error:
                self._log(f"could not publish {name} status to {recipient}: {error}")

    def _configure_adapter(self, name: str, adapter: Any) -> bool:
        try:
            enabled = bool(adapter.configure())
        except Exception as error:
            raise RuntimeError(f"{name} configuration failed: {error}") from error
        if enabled:
            self._log(f"starting {name} adapter")
            if name not in self._adapter_event_states:
                self._publish_adapter_event(name, adapter, "starting", f"Starting {name} chat server connection")
        elif adapter.status.get("lastError"):
            self._log(f"{name} adapter disabled: {adapter.status['lastError']}")
        return enabled

    def _cycle_adapter(self, name: str, adapter: Any, mailbox: Any) -> None:
        was_connected = bool(adapter.status.get("connected"))
        try:
            adapter.cycle(mailbox)
        except Exception as error:
            safe_error = self._safe_error(error)
            adapter.status.update({"connected": False, "lastError": safe_error})
            self._publish_adapter_event(
                name, adapter, "connection_failed", f"{name} chat server connection failed: {safe_error}",
                diagnostic={
                    "error_type": type(error).__name__, "error_message": safe_error,
                    "operation": "connect_or_poll", "recoverable": True, "will_retry": True,
                    "enabled": bool(adapter.status.get("enabled")),
                },
            )
            raise RuntimeError(f"{name} connection/poll failed: {safe_error}") from error
        if adapter.status.get("connected") and not was_connected:
            self._log(f"{name} adapter connected")
            self._publish_adapter_event(name, adapter, "connected", f"{name} chat server connected")
        elif adapter.status.get("enabled"):
            self._log(f"{name} adapter poll completed", level=2)

    def configure(self) -> bool:
        self.mattermost_enabled = super().configure()
        if self.mattermost_enabled:
            self._log("starting mattermost adapter")
            if "mattermost" not in self._adapter_event_states:
                self._publish_adapter_event(
                    "mattermost", self, "starting", "Starting mattermost chat server connection",
                )
        irc_enabled = self._configure_adapter("irc", self.irc)
        discord_enabled = self._configure_adapter("discord", self.discord)
        matrix_enabled = self._configure_adapter("matrix", self.matrix)
        slack_enabled = self._configure_adapter("slack", self.slack)
        telegram_enabled = self._configure_adapter("telegram", self.telegram)
        whatsapp_enabled = self._configure_adapter("whatsapp", self.whatsapp)
        facebook_enabled = self._configure_adapter("facebook_messenger", self.facebook_messenger)
        viber_enabled = self._configure_adapter("viber", self.viber)
        line_enabled = self._configure_adapter("line", self.line)
        discourse_enabled = self._configure_adapter("discourse", self.discourse)
        whatsapp_personal_enabled = self._configure_adapter("whatsapp_personal", self.whatsapp_personal)
        self.status["enabled"] = (self.mattermost_enabled or irc_enabled or discord_enabled
                                  or matrix_enabled or slack_enabled or telegram_enabled
                                  or whatsapp_enabled or facebook_enabled or viber_enabled or line_enabled
                                  or discourse_enabled or whatsapp_personal_enabled)
        self.status["adapters"] = {"mattermost": dict(self.status), "irc": dict(self.irc.status),
                                   "discord": dict(self.discord.status), "matrix": dict(self.matrix.status),
                                   "slack": dict(self.slack.status), "telegram": dict(self.telegram.status),
                                   "whatsapp": dict(self.whatsapp.status),
                                   "facebook_messenger": dict(self.facebook_messenger.status),
                                   "viber": dict(self.viber.status)}
        self.status["adapters"]["line"] = dict(self.line.status)
        self.status["adapters"]["discourse"] = dict(self.discourse.status)
        self.status["adapters"]["whatsapp_personal"] = dict(self.whatsapp_personal.status)
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
        self.line.close()
        self.discourse.close()
        self.whatsapp_personal.close()
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
        self.line.close()
        self.discourse.close()
        self.whatsapp_personal.close()
        self._bot_user_id = ""
        self._latest_create_at.clear()
        self._next_dm_refresh = 0.0
        self.status["connected"] = False

    def cycle(self) -> None:
        mailbox = self._mailbox()
        if self.mattermost_enabled:
            was_connected = bool(self._bot_user_id)
            if not self._bot_user_id:
                try:
                    self._connect()
                except Exception as error:
                    safe_error = self._safe_error(error)
                    self._publish_adapter_event(
                        "mattermost", self, "connection_failed",
                        f"mattermost chat server connection failed: {safe_error}",
                        diagnostic={
                            "error_type": type(error).__name__, "error_message": safe_error,
                            "operation": "connect", "recoverable": True, "will_retry": True,
                            "enabled": self.mattermost_enabled,
                        },
                    )
                    raise RuntimeError(f"mattermost connection failed: {safe_error}") from error
            if self._bot_user_id and not was_connected:
                self._log("mattermost adapter connected")
                self._publish_adapter_event(
                    "mattermost", self, "connected", "mattermost chat server connected",
                )
            base_url = __import__("os").environ["MM_URL"].rstrip("/")
            try:
                self._refresh_direct_channels(base_url)
                self._poll_inbound(base_url)
            except Exception as error:
                safe_error = self._safe_error(error)
                self._publish_adapter_event(
                    "mattermost", self, "connection_failed",
                    f"mattermost chat server poll failed: {safe_error}",
                    diagnostic={
                        "error_type": type(error).__name__, "error_message": safe_error,
                        "operation": "poll", "recoverable": True, "will_retry": True,
                        "enabled": self.mattermost_enabled,
                    },
                )
                raise RuntimeError(f"mattermost poll failed: {safe_error}") from error
            self._log("mattermost adapter poll completed", level=2)
        self._cycle_adapter("irc", self.irc, mailbox)
        self._cycle_adapter("discord", self.discord, mailbox)
        self._cycle_adapter("matrix", self.matrix, mailbox)
        self._cycle_adapter("slack", self.slack, mailbox)
        self._cycle_adapter("telegram", self.telegram, mailbox)
        self._cycle_adapter("whatsapp", self.whatsapp, mailbox)
        self._cycle_adapter("facebook_messenger", self.facebook_messenger, mailbox)
        self._cycle_adapter("viber", self.viber, mailbox)
        self._cycle_adapter("line", self.line, mailbox)
        self._cycle_adapter("discourse", self.discourse, mailbox)
        self._cycle_adapter("whatsapp_personal", self.whatsapp_personal, mailbox)
        self._dispatch_outbound()
        self.status.update({
            "connected": bool((self.mattermost_enabled and self._bot_user_id) or self.irc.status["connected"]
                              or self.discord.status["connected"] or self.matrix.status["connected"]
                              or self.slack.status["connected"] or self.telegram.status["connected"]
                              or self.whatsapp.status["connected"]
                              or self.facebook_messenger.status["connected"]
                              or self.viber.status["connected"]
                              or self.line.status["connected"]
                              or self.discourse.status["connected"]
                              or self.whatsapp_personal.status["connected"]),
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
                "line": dict(self.line.status),
                "discourse": dict(self.discourse.status),
                "whatsapp_personal": dict(self.whatsapp_personal.status),
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
                elif channel_type == "line" and self.line.status["enabled"]:
                    self.line.send_message(message)
                elif channel_type == "discourse" and self.discourse.status["enabled"]:
                    self.discourse.send_message(message)
                elif channel_type == "whatsapp_personal" and self.whatsapp_personal.status["enabled"]:
                    self.whatsapp_personal.send_message(message)
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
