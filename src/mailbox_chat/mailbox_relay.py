"""Transport-neutral mailbox relay with pluggable communication adapters."""

from __future__ import annotations

import sys
import os
import time
from typing import Any

from .adapters.mattermost_adapter import MattermostRelay, RELAY_PORT
from .adapters.irc_adapter import IrcAdapter
from .adapters.discord_adapter import DiscordAdapter
from .adapters.matrix_adapter import MatrixAdapter
from .adapters.slack_adapter import SlackAdapter
from .adapters.telegram_adapter import TelegramAdapter
from .adapters.facebook_messenger_adapter import FacebookMessengerAdapter
from .adapters.whatsapp_adapter import WhatsAppAdapter
from .adapters.viber_adapter import ViberAdapter
from .adapters.line_adapter import LineAdapter
from .adapters.discourse_adapter import DiscourseAdapter
from .adapters.whatsapp_personal_adapter import WhatsAppPersonalAdapter
from .delivery_ledger import DeliveryLedger, endpoint_id, origin_id
from .connector_registry import connectors_for
from .subscriptions import SERVER_EVENTS_MAILBOX, subscribers
from .endpoint_address import endpoint_instance, parse_endpoint


RELAY_RECIPIENT = "server_outbound_relay_agent_to_mailbox"
SUPPORTED_MAILBOX_TYPES = ("mattermost", "irc", "discord", "matrix", "slack", "telegram",
                           "whatsapp", "whatsapp_personal", "facebook_messenger", "viber", "line",
                           "discourse")
PLANNED_MAILBOX_TYPES: tuple[str, ...] = ()
ADAPTER_CAPABILITIES = {
    # ghost.tier: full = real, unbadged user; display = per-message name+avatar but bot-attributed;
    # none = one fixed identity per account/connection.
    "mattermost": {
        "presence": "single", "threads": True, "attachments": True,
        "ghost": {"tier": "display", "mechanism": "webhook", "permission": "server_setting"},
        "dm_cold_initiate": False,
        "discovery": {"mailboxes": True, "users": True},
        "auth_methods": ["bot_token", "incoming_webhook"],
        "official": True,
        "notes": "webhook override_username/icon gated by EnablePostUsernameOverride/IconOverride "
                 "(admin, default off); always shows a BOT badge; REST bot posts have no per-post "
                 "override; root_id threads",
    },
    "irc": {
        "presence": "single", "threads": False, "attachments": False,
        "ghost": {"tier": "none", "mechanism": "none", "permission": "none"},
        "dm_cold_initiate": True,
        "discovery": {"mailboxes": True, "users": True},
        "auth_methods": ["sasl_plain", "sasl_external", "nickserv", "quakenet_q"],
        "official": True,
        "notes": "one real nick per TCP connection; no threads/native files (URLs/DCC only); "
                 "LIST/NAMES/WHO discovery; QuakeNet=Q auth, no SASL, no msgid (heuristic dedupe); "
                 "Libera=SASL/CertFP, has msgid+server-time+echo-message",
    },
    "discord": {
        "presence": "single", "threads": True, "attachments": True,
        "ghost": {"tier": "display", "mechanism": "webhook", "permission": "manage_webhooks"},
        "dm_cold_initiate": False,
        "discovery": {"mailboxes": True, "users": True},
        "auth_methods": ["bot_token", "webhook_token"],
        "official": True,
        "notes": "webhooks set per-message username+avatar (needs Manage Webhooks) but cannot DM "
                 "and have no presence/status; bot needs a shared guild to DM",
    },
    "matrix": {
        "presence": "multiple", "threads": True, "attachments": True,
        "ghost": {"tier": "full", "mechanism": "appservice", "permission": "homeserver_admin"},
        "dm_cold_initiate": True,
        "discovery": {"mailboxes": True, "users": True},
        "auth_methods": ["access_token", "appservice"],
        "official": True,
        "notes": "application service masquerades as ghost users via ?user_id= within its users "
                 "namespace (homeserver admin installs the AS registration); ghosts are full users; "
                 "m.thread relations; availability-'presence' is CPU-heavy and often disabled",
    },
    "discourse": {
        "presence": "multiple", "threads": True, "attachments": True, "interaction": "forum",
        "ghost": {"tier": "full", "mechanism": "admin_post_as", "permission": "admin_api_key"},
        "dm_cold_initiate": True,
        "discovery": {"mailboxes": True, "users": True},
        "auth_methods": ["api_key"],
        "official": True,
        "notes": "admin API key + Api-Username posts AS any existing user with no badge "
                 "(true attribution); can pre-create accounts; forum topics/posts, not chat threads",
    },
    "slack": {
        "presence": "multiple", "threads": True, "attachments": True,
        "ghost": {"tier": "display", "mechanism": "webhook", "permission": "none"},
        "dm_cold_initiate": False,
        "discovery": {"mailboxes": True, "users": True},
        "auth_methods": ["bot_token", "app_token", "incoming_webhook"],
        "official": True,
        "notes": "incoming webhook or chat:write.customize overrides display name+icon "
                 "(still app/bot attributed, not real impersonation); thread_ts threads; "
                 "1 msg/sec webhook limit",
    },
    "telegram": {
        "presence": "single", "threads": True, "attachments": True,
        "ghost": {"tier": "none", "mechanism": "none", "permission": "none"},
        "dm_cold_initiate": False,
        "discovery": {"mailboxes": False, "users": False},
        "auth_methods": ["bot_token"],
        "official": True,
        "notes": "no sender override (author_signature is cosmetic); message_thread_id forum topics; "
                 "user must /start first; upload 50MB / download 20MB (2GB via local Bot API server)",
    },
    "whatsapp": {
        "presence": "single", "threads": False, "attachments": True, "interaction": "business",
        "ghost": {"tier": "none", "mechanism": "none", "permission": "none"},
        "dm_cold_initiate": False,
        "discovery": {"mailboxes": False, "users": False},
        "auth_methods": ["system_user_token"],
        "official": True,
        "notes": "Cloud API; fixed business phone number (no ghosting); 24h customer-service window, "
                 "template messages required outside it; multiple numbers per WABA; 100MB media",
    },
    "whatsapp_personal": {
        "presence": "single", "threads": True, "attachments": True,
        "interaction": "unofficial-web-companion",
        "ghost": {"tier": "none", "mechanism": "none", "permission": "none"},
        "dm_cold_initiate": True,
        "discovery": {"mailboxes": False, "users": False},
        "auth_methods": ["qr_pairing"],
        "official": False,
        "notes": "unofficial web-companion (Baileys/whatsapp-web.js) — against WhatsApp ToS, ban "
                 "risk; one real account, no ghosting; no messaging window",
    },
    "facebook_messenger": {
        "presence": "multiple", "threads": False, "attachments": True, "interaction": "page",
        "ghost": {"tier": "display", "mechanism": "personas", "permission": "none"},
        "dm_cold_initiate": False,
        "discovery": {"mailboxes": False, "users": False},
        "auth_methods": ["page_token"],
        "official": True,
        "notes": "Personas API sends with a custom persona name+avatar within a Page; 24h window + "
                 "message tags for out-of-window sends",
    },
    "viber": {
        "presence": "single", "threads": False, "attachments": True, "interaction": "bot",
        "ghost": {"tier": "display", "mechanism": "sender_override", "permission": "none"},
        "dm_cold_initiate": False,
        "discovery": {"mailboxes": False, "users": False},
        "auth_methods": ["auth_token"],
        "official": True,
        "notes": "per-message sender.name+sender.avatar override (no extra permission); new bots are "
                 "commercial-only since 2024; user must subscribe first; no subscriber-list API",
    },
    "line": {
        "presence": "single", "threads": False, "attachments": True,
        "interaction": "user-group-room",
        "ghost": {"tier": "display", "mechanism": "sender_override", "permission": "none"},
        "dm_cold_initiate": False,
        "discovery": {"mailboxes": False, "users": False},
        "auth_methods": ["mailbox_access_token"],
        "official": True,
        "notes": "per-message sender.name (<=20 chars)+iconUrl override; reply token single-use ~1min "
                 "(else push, counts against monthly quota); member-list API needs verified/premium",
    },
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
        self._injected_irc = irc_adapter
        self.irc_adapters: list[IrcAdapter] = []
        self._irc_duplicates_suppressed: list[dict[str, Any]] = []
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
        self.mattermost_adapters: list[MattermostRelay] = []
        self._mattermost_connector_status: dict[str, dict[str, Any]] = {}
        self.delivery_ledger = DeliveryLedger(self._mailbox().mailbox_dir())
        self._adapter_event_states: dict[str, str] = {}
        self._mattermost_channel_lagging: dict[str, bool] = {}
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
        if name == "mattermost":
            connectors = connectors_for("mattermost")
        elif name.startswith("mattermost:"):
            connectors = [dict(getattr(adapter, "connector", {}))]
        else:
            connectors = list(getattr(adapter, "connectors", []))
        recipients = [
            recipient
            for connector in connectors
            for recipient in [connector.get("bridge_agent"), *connector.get("mailbox_recipients", [])]
            if recipient
        ]
        if name.startswith("mattermost") and not recipients:
            default_mailbox = str(getattr(adapter, "default_mailbox", "") or "")
            if default_mailbox:
                recipients = adapter._inbound_recipients(default_mailbox)
        return list(dict.fromkeys(recipients))

    def _adapter_context(self, name: str, adapter: Any) -> dict[str, Any]:
        if name == "mattermost":
            connectors = connectors_for("mattermost")
        elif name.startswith("mattermost:"):
            connectors = [dict(getattr(adapter, "connector", {}))]
        else:
            connectors = list(getattr(adapter, "connectors", []))
        status = getattr(adapter, "status", {})
        return {
            "adapter": name,
            "connector_ids": [str(item.get("id") or "") for item in connectors if item.get("id")],
            "mailbox_ids": list(dict.fromkeys(
                str(mailbox_id) for item in connectors for mailbox_id in item.get("mailbox_ids", [])
            )),
            "directions": list(dict.fromkeys(
                str(item.get("direction") or "") for item in connectors if item.get("direction")
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
        recipients = list(dict.fromkeys([
            SERVER_EVENTS_MAILBOX,
            *subscribers(SERVER_EVENTS_MAILBOX), *self._adapter_recipients(name, adapter),
        ]))
        for recipient in recipients:
            try:
                mailbox.send(
                    recipient,
                    text,
                    sender=f"local-{name}-server",
                    message_type="chat_server_status",
                    mailbox_type=name,
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

    def _configure_mattermost(self) -> bool:
        raw_connectors = list(connectors_for("mattermost"))
        self._mattermost_connector_status = {}
        token_backed_instances = {
            endpoint_instance("mattermost", connector)
            for connector in raw_connectors
            if str(connector.get("token_env") or "").strip()
        }
        connectors: list[dict[str, Any]] = []
        for connector in raw_connectors:
            connector_id = str(connector.get("id") or "")
            instance = endpoint_instance("mattermost", connector)
            if instance and instance in token_backed_instances and not str(
                connector.get("token_env") or ""
            ).strip():
                self._mattermost_connector_status[connector_id] = {
                    "enabled": False,
                    "connected": False,
                    "lastError": (
                        "Skipped legacy connector because token-backed bot connectors "
                        f"are configured for {instance}"
                    ),
                    "mailboxes": list(connector.get("mailbox_ids") or []),
                }
                self._log(
                    "skipping legacy mattermost connector "
                    f"{connector.get('id') or '<unknown>'} on {instance} "
                    "because token-backed bot connectors are configured",
                )
                continue
            connectors.append(connector)
        adapters: list[MattermostRelay] = []
        if not connectors:
            self.mattermost_enabled = super().configure()
            self.mattermost_adapters = [self] if self.mattermost_enabled else []
            if self.mattermost_enabled:
                self._log("starting mattermost adapter")
                if "mattermost" not in self._adapter_event_states:
                    self._publish_adapter_event(
                        "mattermost", self, "starting",
                        "Starting mattermost chat server connection",
                    )
            return self.mattermost_enabled

        for index, connector in enumerate(connectors):
            adapter = self if index == 0 else MattermostRelay()
            try:
                enabled = bool(
                    MattermostRelay.configure(self, connector)
                    if adapter is self else adapter.configure(connector)
                )
            except Exception as error:
                raise RuntimeError(f"mattermost configuration failed: {error}") from error
            connector_id = str(connector.get("id") or "")
            self._mattermost_connector_status[connector_id] = {
                "enabled": bool(adapter.status.get("enabled")),
                "connected": bool(adapter.status.get("connected") and getattr(adapter, "_bot_user_id", "")),
                "lastError": adapter.status.get("lastError"),
                "mailboxes": list(adapter.status.get("mailboxes") or connector.get("mailbox_ids") or []),
            }
            if not enabled:
                if adapter.status.get("lastError"):
                    self._log(f"mattermost adapter disabled: {adapter.status['lastError']}")
                continue
            adapters.append(adapter)
            name = "mattermost" if index == 0 else f"mattermost:{connector.get('id') or index}"
            self._log(f"starting {name} adapter")
            if name not in self._adapter_event_states:
                self._publish_adapter_event(
                    name, adapter, "starting", f"Starting {name} chat server connection",
                )
        self.mattermost_adapters = adapters
        self.mattermost_enabled = bool(adapters)
        return self.mattermost_enabled

    def _configure_irc(self) -> bool:
        """Build one IRC adapter per configured IRC connector (multi-server)."""
        connectors = list(connectors_for("irc"))
        adapters: list[IrcAdapter] = []
        self._irc_duplicates_suppressed = []
        seen_profiles: dict[tuple[str, str, tuple[str, ...]], str] = {}
        for index, connector in enumerate(connectors):
            instance = endpoint_instance("irc", connector)
            nickname = str(connector.get("nickname") or "").strip().casefold()
            mailbox_ids = tuple(sorted(
                str(mailbox_id).strip()
                for mailbox_id in connector.get("mailbox_ids", [])
                if str(mailbox_id).strip()
            ))
            profile = (instance, nickname, mailbox_ids)
            connector_id = str(connector.get("id") or index)
            if profile in seen_profiles:
                original = seen_profiles[profile]
                self._irc_duplicates_suppressed.append({
                    "suppressed_connector_id": connector_id,
                    "kept_connector_id": original,
                    "instance": instance,
                    "nickname": str(connector.get("nickname") or ""),
                    "mailbox_ids": list(mailbox_ids),
                })
                self._log(
                    f"skipping duplicate IRC connector {connector_id} for "
                    f"{instance} nick {connector.get('nickname') or ''} "
                    f"(already using {original})",
                )
                continue
            seen_profiles[profile] = connector_id
            adapter = (self._injected_irc if index == 0 and self._injected_irc is not None
                       else IrcAdapter())
            try:
                adapter.configure(connector)
            except Exception as error:
                raise RuntimeError(f"irc configuration failed: {error}") from error
            adapters.append(adapter)
            name = f"irc:{adapter.connector_id or connector.get('id') or index}"
            self._log(f"starting {name} adapter")
            if name not in self._adapter_event_states:
                self._publish_adapter_event(
                    name, adapter, "starting", f"Starting {name} chat server connection")
        self.irc_adapters = adapters
        self.irc = adapters[0] if adapters else (self._injected_irc or self.irc)
        return bool(adapters)

    def irc_adapter_for(self, *, connector_id: str = "", instance: str = "") -> IrcAdapter | None:
        """Pick an enabled IRC adapter by connector id or server instance."""
        adapters = [adapter for adapter in self.irc_adapters if adapter.status.get("enabled")]
        if not adapters:
            return None
        if connector_id:
            for adapter in adapters:
                if adapter.connector_id == connector_id:
                    return adapter
        if instance:
            for adapter in adapters:
                if adapter.irc_instance == instance:
                    return adapter
        return adapters[0]

    def _irc_enabled(self) -> bool:
        return any(adapter.status.get("enabled") for adapter in self.irc_adapters)

    def _irc_connected(self) -> bool:
        return any(adapter.status.get("connected") for adapter in self.irc_adapters)

    def _mattermost_connected(self) -> bool:
        return any(adapter.status.get("connected") and bool(getattr(adapter, "_bot_user_id", ""))
                   for adapter in self.mattermost_adapters)

    def _mattermost_status_summary(self) -> dict[str, Any]:
        configured = list(connectors_for("mattermost"))
        active_by_id = {
            str(adapter.connector.get("id") or ""): adapter
            for adapter in self.mattermost_adapters
            if str(adapter.connector.get("id") or "")
        }
        servers: list[dict[str, Any]] = []
        for index, connector in enumerate(configured):
            connector_id = str(connector.get("id") or "")
            instance = endpoint_instance("mattermost", connector)
            adapter = active_by_id.get(connector_id)
            status = self._mattermost_connector_status.get(connector_id, {})
            servers.append({
                "name": "mattermost" if index == 0 else f"mattermost:{connector_id or index}",
                "connector_id": connector_id,
                "instance": instance,
                "enabled": bool(status.get("enabled", bool(adapter))),
                "connected": bool(
                    status.get("connected", False)
                    or (adapter and adapter.status.get("connected") and getattr(adapter, "_bot_user_id", ""))
                ),
                "lastError": status.get("lastError"),
                "mailboxes": list(
                    status.get("mailboxes")
                    or (list(adapter.status.get("mailboxes") or []) if adapter else list(connector.get("mailbox_ids") or []))
                ),
            })
        return {
            "enabled": self.mattermost_enabled,
            "connected": self._mattermost_connected(),
            "servers": servers,
        }

    def _irc_status_summary(self) -> dict[str, Any]:
        return {
            "enabled": self._irc_enabled(),
            "connected": self._irc_connected(),
            "servers": [
                {"connector_id": adapter.connector_id, "instance": adapter.irc_instance,
                 **adapter.status}
                for adapter in self.irc_adapters
            ],
            "duplicates_suppressed": list(self._irc_duplicates_suppressed),
        }

    def _select_irc_adapter(self, message: dict[str, Any]) -> IrcAdapter:
        """Route an outbound message to the IRC server it belongs to.

        Prefer an explicit connector_id; otherwise match the endpoint instance
        (the IRC server host); otherwise fall back to the only/first adapter.
        """
        adapters = [adapter for adapter in self.irc_adapters if adapter.status.get("enabled")]
        if not adapters:
            raise RuntimeError("No enabled IRC connector is configured")
        connector_id = str(message.get("connector_id") or "").strip()
        if connector_id:
            for adapter in adapters:
                if adapter.connector_id == connector_id:
                    return adapter
        address = parse_endpoint(str(message.get("endpoint_address") or ""))
        instance = address.instance if address else ""
        if instance:
            for adapter in adapters:
                if adapter.irc_instance == instance:
                    return adapter
        return adapters[0]

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
        self.mattermost_enabled = self._configure_mattermost()
        irc_enabled = self._configure_irc()
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
        self.status["adapters"] = {"mattermost": self._mattermost_status_summary(),
                                   "irc": self._irc_status_summary(),
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
        for adapter in self.mattermost_adapters:
            if adapter is self:
                continue
            adapter.stop()
        for adapter in self.irc_adapters:
            adapter.close()
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
        for adapter in self.mattermost_adapters:
            if adapter is self:
                continue
            adapter.stop()
            adapter._bot_user_id = ""
            adapter._latest_create_at.clear()
            adapter._next_dm_refresh = 0.0
            adapter.status["connected"] = False
        for adapter in self.irc_adapters:
            adapter.close()
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

    def _cycle_mattermost_adapter(self, name: str, adapter: MattermostRelay) -> None:
        was_connected = bool(adapter._bot_user_id)
        if not adapter._bot_user_id:
            try:
                adapter._connect()
            except Exception as error:
                safe_error = self._safe_error(error)
                self._publish_adapter_event(
                    name, adapter, "connection_failed",
                    f"{name} chat server connection failed: {safe_error}",
                    diagnostic={
                        "error_type": type(error).__name__, "error_message": safe_error,
                        "operation": "connect", "recoverable": True, "will_retry": True,
                        "enabled": bool(adapter.status.get("enabled")),
                    },
                )
                raise RuntimeError(f"{name} connection failed: {safe_error}") from error
        if adapter._bot_user_id and not was_connected:
            self._log(f"{name} adapter connected")
            self._publish_adapter_event(name, adapter, "connected", f"{name} chat server connected")
        try:
            adapter._refresh_direct_mailboxes(adapter.base_url)
            adapter._poll_inbound(adapter.base_url)
        except Exception as error:
            safe_error = self._safe_error(error)
            self._publish_adapter_event(
                name, adapter, "connection_failed",
                f"{name} chat server poll failed: {safe_error}",
                diagnostic={
                    "error_type": type(error).__name__, "error_message": safe_error,
                    "operation": "poll", "recoverable": True, "will_retry": True,
                    "enabled": bool(adapter.status.get("enabled")),
                },
            )
            raise RuntimeError(f"{name} poll failed: {safe_error}") from error
        self._log(f"{name} adapter poll completed", level=2)

    def _publish_mattermost_channel_sync_event(
        self,
        mailbox_id: str,
        *,
        lagging: bool,
        lag_seconds: float,
        ahead_adapter: str,
        behind_adapter: str,
    ) -> None:
        mailbox = self._mailbox()
        recipients = list(dict.fromkeys([
            SERVER_EVENTS_MAILBOX,
            *subscribers(SERVER_EVENTS_MAILBOX),
            *self._inbound_recipients(mailbox_id),
        ]))
        for recipient in recipients:
            mailbox.send(
                recipient,
                (f"Mattermost channel {mailbox_id} listener lag detected: {behind_adapter} is "
                 f"{int(lag_seconds)}s behind {ahead_adapter}"
                 if lagging else
                 f"Mattermost channel {mailbox_id} listener lag recovered"),
                sender="local-mattermost-server",
                message_type="chat_server_status",
                mailbox_id=mailbox_id,
                mailbox_type="mattermost",
                extra_fields={
                    "adapter": "mattermost",
                    "connection_state": ("channel_lagging" if lagging else "channel_synchronized"),
                    "local_chat_server": True,
                    "service_context": self._mattermost_status_summary(),
                    "diagnostic": {
                        "mailbox_id": mailbox_id,
                        "lag_seconds": lag_seconds,
                        "ahead_adapter": ahead_adapter,
                        "behind_adapter": behind_adapter,
                        "threshold_seconds": int(os.environ.get(
                            "MATTERMOST_CHANNEL_LAG_WARNING_SECONDS", "180",
                        )),
                    },
                },
            )

    def _check_mattermost_channel_sync(self) -> None:
        threshold_seconds = int(os.environ.get("MATTERMOST_CHANNEL_LAG_WARNING_SECONDS", "180"))
        if threshold_seconds <= 0:
            return
        by_mailbox: dict[str, list[tuple[str, int]]] = {}
        for index, adapter in enumerate(self.mattermost_adapters):
            name = "mattermost" if index == 0 else f"mattermost:{adapter.connector.get('id') or index}"
            for mailbox_id, created_at in adapter._latest_create_at.items():
                timestamp = int(created_at or 0)
                if mailbox_id and timestamp > 0:
                    by_mailbox.setdefault(mailbox_id, []).append((name, timestamp))
        for mailbox_id, samples in by_mailbox.items():
            if len(samples) < 2:
                self._mattermost_channel_lagging[mailbox_id] = False
                continue
            behind_adapter, min_seen = min(samples, key=lambda item: item[1])
            ahead_adapter, max_seen = max(samples, key=lambda item: item[1])
            lag_seconds = max(0.0, (max_seen - min_seen) / 1000.0)
            lagging = lag_seconds >= float(threshold_seconds)
            previously_lagging = self._mattermost_channel_lagging.get(mailbox_id, False)
            if lagging != previously_lagging:
                self._publish_mattermost_channel_sync_event(
                    mailbox_id,
                    lagging=lagging,
                    lag_seconds=lag_seconds,
                    ahead_adapter=ahead_adapter,
                    behind_adapter=behind_adapter,
                )
            self._mattermost_channel_lagging[mailbox_id] = lagging

    def cycle(self) -> None:
        mailbox = self._mailbox()
        if self.mattermost_enabled:
            for index, adapter in enumerate(self.mattermost_adapters):
                name = "mattermost" if index == 0 else f"mattermost:{adapter.connector.get('id') or index}"
                self._cycle_mattermost_adapter(name, adapter)
            self._check_mattermost_channel_sync()
        for index, adapter in enumerate(self.irc_adapters):
            self._cycle_adapter(f"irc:{adapter.connector_id or index}", adapter, mailbox)
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
            "connected": bool(self._mattermost_connected() or self._irc_connected()
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
                "mattermost": self._mattermost_status_summary(),
                "irc": self._irc_status_summary(),
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
            if message.get("audit_of"):
                # Passive audit copies share the queue mailbox; only direct
                # queue sends are deliveries.
                continue
            mailbox_type = str(message.get("mailbox_type") or "mattermost").lower()
            address = parse_endpoint(str(message.get("endpoint_address") or ""))
            if address and not message.get("connector_id") and mailbox_type != "mattermost":
                matches = [
                    connector for connector in connectors_for(mailbox_type, direction="outbound")
                    if address.instance == "0" or endpoint_instance(mailbox_type, connector) == address.instance
                ]
                if matches and (address.instance == "0" or len(matches) == 1):
                    message["connector_id"] = matches[0]["id"]
            destination = endpoint_id(
                mailbox_type,
                connector_id=str(message.get("connector_id") or ""),
                mailbox_id=str(message.get("mailbox_id") or ""),
                presence_id=str(message.get("presence_id") or ""),
            )
            if not self.delivery_ledger.claim(message, destination):
                mailbox.send(
                    str(message.get("from") or "console-default-agent"),
                    f"Suppressed duplicate relay to {destination}",
                    sender="mailbox-server",
                    message_type="mailbox_delivery_suppressed",
                    extra_fields={
                        "origin_id": origin_id(message),
                        "request_id": message.get("id"),
                        "destination_endpoint": destination,
                    },
                )
                continue
            try:
                if mailbox_type == "mattermost" and self.mattermost_enabled:
                    mattermost_adapter = self._select_mattermost_adapter(message, address)
                    posted = self._send_mattermost_message(mattermost_adapter, message)
                    if not address or address.identifier in set(mattermost_adapter._mailboxes()):
                        self._fanout_mattermost_outbound(mattermost_adapter, message, posted)
                elif mailbox_type == "irc" and self._irc_enabled():
                    self._select_irc_adapter(message).send_message(message)
                elif mailbox_type == "discord" and self.discord.status["enabled"]:
                    self.discord.send_message(message)
                elif mailbox_type == "matrix" and self.matrix.status["enabled"]:
                    self.matrix.send_message(message)
                elif mailbox_type == "slack" and self.slack.status["enabled"]:
                    self.slack.send_message(message)
                elif mailbox_type == "telegram" and self.telegram.status["enabled"]:
                    self.telegram.send_message(message)
                elif mailbox_type == "whatsapp" and self.whatsapp.status["enabled"]:
                    self.whatsapp.send_message(message)
                elif mailbox_type == "facebook_messenger" and self.facebook_messenger.status["enabled"]:
                    self.facebook_messenger.send_message(message)
                elif mailbox_type == "viber" and self.viber.status["enabled"]:
                    self.viber.send_message(message)
                elif mailbox_type == "line" and self.line.status["enabled"]:
                    self.line.send_message(message)
                elif mailbox_type == "discourse" and self.discourse.status["enabled"]:
                    self.discourse.send_message(message)
                elif mailbox_type == "whatsapp_personal" and self.whatsapp_personal.status["enabled"]:
                    self.whatsapp_personal.send_message(message)
                else:
                    raise RuntimeError(f"Channel adapter is not enabled: {mailbox_type}")
            except Exception as error:
                self.delivery_ledger.release(message, destination)
                mailbox.send(
                    str(message.get("from") or "console-default-agent"),
                    str(error),
                    sender="mailbox-server",
                    message_type="mailbox_delivery_failed",
                    extra_fields={
                        "mailbox_type": mailbox_type,
                        "request_id": message.get("id"),
                        "workflow_run_id": message.get("workflow_run_id"),
                    },
                )

    @staticmethod
    def _mailbox():
        from . import agent_mailbox

        return agent_mailbox

    def _select_mattermost_adapter(
        self, message: dict[str, Any], address: Any,
    ) -> MattermostRelay:
        adapters = [adapter for adapter in self.mattermost_adapters if adapter.status.get("enabled")]
        if not adapters:
            raise RuntimeError("No enabled Mattermost connector is configured")
        connector_id = str(message.get("connector_id") or "").strip()
        if connector_id:
            for adapter in adapters:
                if str(adapter.connector.get("id") or "") == connector_id:
                    return adapter
        mailbox_id = str(message.get("mailbox_id") or "").strip()
        if not mailbox_id and address:
            mailbox_id = str(address.identifier or "").strip()
        if mailbox_id:
            for adapter in adapters:
                if mailbox_id in set(adapter._mailboxes()):
                    return adapter
        if address and address.instance and address.instance != "0":
            for adapter in adapters:
                instance = endpoint_instance("mattermost", adapter.connector)
                if instance == address.instance:
                    return adapter
        return adapters[0]

    def _fanout_mattermost_outbound(
        self, adapter: MattermostRelay, message: dict[str, Any], posted: dict[str, Any],
    ) -> None:
        """Deliver a bot-authored mailbox post once to the other mailbox subscribers."""
        mailbox = self._mailbox()
        mailbox_id = str(message.get("mailbox_id") or adapter.default_mailbox)
        sender = str(message.get("from") or "console-default-agent")
        source_id = str(posted.get("id") or message.get("id") or "")
        connectors = [
            item for item in connectors_for("mattermost", direction="inbound")
            if mailbox_id in item.get("mailbox_ids", [])
        ]
        connector = connectors[0] if connectors else dict(adapter.connector)
        connector_ids = [
            str(item.get("id") or "")
            for item in connectors
            if str(item.get("id") or "").strip()
        ]
        if not connector_ids and str(connector.get("id") or "").strip():
            connector_ids = [str(connector.get("id"))]
        origin_fields = {
            "origin": {
                "adapter": "mailbox",
                "connector_id": str(connector.get("id") or "mattermost-default"),
                "connector_ids": connector_ids,
                "source_id": str(message.get("id") or source_id),
                "mailbox_id": mailbox_id,
                "presence_id": str(connector.get("presence_id") or ""),
            },
            "origin_id": origin_id(message),
            "relay_trace": list(dict.fromkeys([
                *list(message.get("relay_trace") or []),
                endpoint_id(
                    "mattermost",
                    connector_id=str(connector.get("id") or "mattermost-default"),
                    mailbox_id=mailbox_id,
                    presence_id=str(connector.get("presence_id") or ""),
                ),
            ])),
            "platform_author": adapter._bot_user_id,
            "relayed_from": sender,
        }
        for recipient in adapter._inbound_recipients(mailbox_id):
            if recipient == sender:
                continue
            mailbox.send(
                recipient,
                str(message.get("text") or ""),
                sender=sender,
                message_type="mattermost_message",
                mailbox_id=mailbox_id,
                mailbox_type="mattermost",
                source_id=source_id,
                thread_id=str(message.get("root_id") or message.get("thread_id") or "") or None,
                attachments=list(message.get("attachments") or []),
                extra_fields=origin_fields,
            )

    def _send_mattermost_message(self, adapter: MattermostRelay, message: dict[str, Any]) -> dict[str, Any]:
        base_url = adapter.base_url
        mailbox_id = str(message.get("mailbox_id") or adapter.default_mailbox)
        address = parse_endpoint(str(message.get("endpoint_address") or ""))
        if address:
            from urllib.parse import urlsplit
            active_instance = (urlsplit(base_url).hostname or "").lower()
            if address.instance not in {"0", active_instance}:
                raise ValueError(
                    f"Mattermost endpoint server {address.instance} does not match {active_instance}"
                )
            configured_mailboxes = set(adapter._mailboxes())
            if address.identifier in configured_mailboxes:
                mailbox_id = address.identifier
            else:
                response = adapter.session.post(
                    f"{base_url}/api/v4/channels/direct",
                    json=[adapter._bot_user_id, address.identifier], timeout=15,
                )
                response.raise_for_status()
                direct = response.json()
                mailbox_id = str(direct.get("id") or "")
                if not mailbox_id:
                    raise RuntimeError(f"Mattermost did not resolve DM recipient {address.identifier}")
        payload: dict[str, Any] = {
            "channel_id": mailbox_id,
            "message": str(message.get("text", "")),
            "file_ids": adapter._upload_attachments(
                base_url, mailbox_id, list(message.get("attachments") or []),
            ),
        }
        root_id = str(message.get("root_id") or message.get("thread_id") or "")
        if root_id:
            payload["root_id"] = root_id
        response = adapter.session.post(f"{base_url}/api/v4/posts", json=payload, timeout=15)
        response.raise_for_status()
        posted = response.json()
        return posted if isinstance(posted, dict) else {}


__all__ = [
    "ADAPTER_CAPABILITIES", "ChannelRelay", "PLANNED_MAILBOX_TYPES", "RELAY_PORT", "RELAY_RECIPIENT",
    "SUPPORTED_MAILBOX_TYPES",
]
