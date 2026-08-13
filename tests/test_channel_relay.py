from pathlib import Path

from mailbox_channels import agent_mailbox
from mailbox_channels.channel_relay import ChannelRelay, RELAY_RECIPIENT
from mailbox_channels.subscriptions import set_subscription


class FailingAdapter:
    def __init__(self) -> None:
        self.status = {"enabled": True, "connected": False, "lastError": None}
        self.listeners = [{
            "id": "discord-main", "direction": "bidirectional", "channel_ids": ["ops"],
            "bridge_agent": "discord-agent", "mailbox_recipients": ["worker"],
        }]

    def cycle(self, _mailbox) -> None:
        raise ConnectionError("service unavailable")


class Response:
    def __init__(self, payload):
        self.payload = payload
        self.content = b""

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class Session:
    def __init__(self):
        self.headers = {}
        self.posts = []
        self.direct_requests = []
        self.empty_channel = False

    def get(self, url, **_kwargs):
        if url.endswith("/users/me"):
            return Response({"id": "bot"})
        if url.endswith("/users/bot/channels"):
            return Response([])
        if self.empty_channel:
            return Response({"order": [], "posts": {}})
        post = {"id": "p1", "user_id": "human", "message": "hello", "create_at": 2, "file_ids": []}
        return Response({"order": ["p1"], "posts": {"p1": post}})

    def post(self, url, **kwargs):
        if url.endswith("/channels/direct"):
            self.direct_requests.append(kwargs["json"])
            return Response({"id": "direct-channel"})
        self.posts.append(kwargs["json"])
        return Response({"id": "sent"})


def test_mattermost_adapter_uses_shared_envelope(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(agent_mailbox.MAILBOX_ENV, str(tmp_path))
    monkeypatch.setenv("MM_URL", "https://mattermost.example")
    monkeypatch.setenv("MM_BOT_TOKEN", "token")
    monkeypatch.setenv("MM_CHANNEL_ID", "channel")
    monkeypatch.setenv("MATTERMOST_RELAY_RECIPIENTS", "local-agent")
    session = Session()
    relay = ChannelRelay(session=session)
    relay.configure()
    relay._connect()
    relay._latest_create_at["channel"] = 0
    agent_mailbox.send(RELAY_RECIPIENT, "done", channel_type="mattermost", channel_id="channel", root=tmp_path)
    relay.cycle()
    received = agent_mailbox.receive("local-agent", root=tmp_path)
    assert next(message for message in received if message["type"] == "mattermost_message")["text"] == "hello"
    assert next(message for message in received if message["type"] == "chat_server_status")["from"] == (
        "local-mattermost-server"
    )
    assert session.posts[0]["message"] == "done"


def test_agent_channel_post_fans_out_to_other_subscribers_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(agent_mailbox.MAILBOX_ENV, str(tmp_path))
    monkeypatch.setenv("MM_URL", "https://mattermost.example")
    monkeypatch.setenv("MM_BOT_TOKEN", "token")
    monkeypatch.setenv("MM_CHANNEL_ID", "channel")
    monkeypatch.setenv(
        "MATTERMOST_RELAY_RECIPIENTS",
        "symbolic-workbench-codex,omegaclaw-core-codex,omegaclaw-min",
    )
    listener = {
        "id": "mattermost-main", "direction": "bidirectional", "channel_ids": ["channel"],
        "bridge_agent": "", "mailbox_recipients": [
            "symbolic-workbench-codex", "omegaclaw-core-codex", "omegaclaw-min",
        ],
    }
    monkeypatch.setattr(
        "mailbox_channels.adapters.mattermost_adapter.listeners_for",
        lambda adapter, **_kwargs: [listener] if adapter == "mattermost" else [],
    )
    monkeypatch.setattr(
        "mailbox_channels.channel_relay.listeners_for",
        lambda adapter, **_kwargs: [listener] if adapter == "mattermost" else [],
    )
    session = Session()
    relay = ChannelRelay(session=session)
    relay.configure()
    relay._connect()
    relay._latest_create_at["channel"] = 3  # no human inbound post in this cycle
    for recipient in ("symbolic-workbench-codex", "omegaclaw-core-codex", "omegaclaw-min"):
        agent_mailbox.receive(recipient, root=tmp_path)  # discard adapter startup status
    request = agent_mailbox.send(
        RELAY_RECIPIENT,
        "Workbench validation completed",
        sender="symbolic-workbench-codex",
        channel_type="mattermost",
        channel_id="channel",
        root=tmp_path,
    )

    session.empty_channel = True
    relay.cycle()

    assert session.posts == [{"channel_id": "channel", "message": request["text"], "file_ids": []}]
    assert agent_mailbox.receive("symbolic-workbench-codex", root=tmp_path) == []
    for recipient in ("omegaclaw-core-codex", "omegaclaw-min"):
        copies = agent_mailbox.receive(recipient, root=tmp_path)
        channel_copy = next(item for item in copies if item["type"] == "mattermost_message")
        assert channel_copy["from"] == "symbolic-workbench-codex"
        assert channel_copy["to"] == recipient
        assert channel_copy["source_id"] == "sent"
        assert channel_copy["relayed_from"] == "symbolic-workbench-codex"

    relay._latest_create_at["channel"] = 0
    relay._bot_user_id = "human"  # the polled copy represents the relay's own platform post
    session.empty_channel = False
    relay._poll_inbound("https://mattermost.example")
    assert agent_mailbox.receive("omegaclaw-core-codex", root=tmp_path) == []


def test_adapter_state_is_always_published_to_server_events(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(agent_mailbox.MAILBOX_ENV, str(tmp_path))
    config = tmp_path / "config"
    config.mkdir()
    relays = config / "relays.json"
    relays.write_text('{"version":1,"listeners":[],"subscriptions":[]}', encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))
    set_subscription("local/0/server_events", "symbolic-workbench-codex", enabled=True)
    relay = ChannelRelay.__new__(ChannelRelay)
    relay._adapter_event_states = {}
    relay._publish_adapter_event(
        "discord", FailingAdapter(), "connection_failed", "discord connection failed",
        diagnostic={"error_type": "ConnectionError", "error_message": "offline"},
    )

    event = agent_mailbox.receive("symbolic-workbench-codex", root=tmp_path)[0]
    assert event["from"] == "local-discord-server"
    assert event["type"] == "chat_server_status"
    assert event["connection_state"] == "connection_failed"
    assert event["diagnostic"]["error_message"] == "offline"
    assert agent_mailbox.receive("local/0/server_events", root=tmp_path) == []


def test_mattermost_person_endpoint_resolves_dm_without_subscriber_fanout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(agent_mailbox.MAILBOX_ENV, str(tmp_path))
    monkeypatch.setenv("MM_URL", "https://chat.snt")
    monkeypatch.setenv("MM_BOT_TOKEN", "token")
    monkeypatch.setenv("MM_CHANNEL_ID", "public-channel")
    monkeypatch.setenv("MATTERMOST_RELAY_RECIPIENTS", "omegaclaw-core-codex")
    session = Session()
    session.empty_channel = True
    relay = ChannelRelay(session=session)
    relay.configure()
    relay._connect()
    agent_mailbox.receive("omegaclaw-core-codex", root=tmp_path)
    agent_mailbox.send(
        RELAY_RECIPIENT, "private", sender="symbolic-workbench-codex",
        channel_type="mattermost", channel_id="person-1", root=tmp_path,
        extra_fields={"endpoint_address": "mm/chat.snt/person-1"},
    )

    relay.cycle()

    assert session.direct_requests == [["bot", "person-1"]]
    assert session.posts == [{"channel_id": "direct-channel", "message": "private", "file_ids": []}]
    assert agent_mailbox.receive("omegaclaw-core-codex", root=tmp_path) == []


def test_adapter_failure_names_service_for_supervisor_retry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(agent_mailbox.MAILBOX_ENV, str(tmp_path))
    relay = ChannelRelay.__new__(ChannelRelay)
    relay.verbose = 1
    relay._adapter_event_states = {}
    adapter = FailingAdapter()
    try:
        relay._cycle_adapter("discord", adapter, object())
    except RuntimeError as error:
        assert str(error) == "discord connection/poll failed: service unavailable"
    else:
        raise AssertionError("adapter error was not propagated to the retry supervisor")
    assert adapter.status["connected"] is False
    assert adapter.status["lastError"] == "service unavailable"
    event = agent_mailbox.receive("worker", root=tmp_path)[0]
    assert event["from"] == "local-discord-server"
    assert event["type"] == "chat_server_status"
    assert event["channel_type"] == "discord"
    assert event["connection_state"] == "connection_failed"
    assert event["local_chat_server"] is True
    assert event["service_context"] == {
        "adapter": "discord",
        "listener_ids": ["discord-main"],
        "channel_ids": ["ops"],
        "directions": ["bidirectional"],
        "enabled": True,
        "connected": False,
        "retry_policy": {"strategy": "exponential", "initial_seconds": 1, "maximum_seconds": 30},
    }
    assert event["diagnostic"] == {
        "error_type": "ConnectionError",
        "error_message": "service unavailable",
        "operation": "connect_or_poll",
        "recoverable": True,
        "will_retry": True,
        "enabled": True,
    }


def test_adapter_diagnostics_redact_environment_secrets(monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "do-not-publish-this")
    error = RuntimeError("request rejected for do-not-publish-this")
    assert ChannelRelay._safe_error(error) == "request rejected for <redacted>"


def test_verbose_logging_coalesces_repeated_messages(monkeypatch, capsys) -> None:
    relay = ChannelRelay.__new__(ChannelRelay)
    relay.verbose = 2
    relay._last_log_message = ""
    relay._last_log_repeats = 0
    relay._repeat_summary_open = False
    relay.status = {}
    monkeypatch.setattr("mailbox_channels.channel_relay.sys.stderr.isatty", lambda: True)

    relay._log("mattermost adapter poll completed", level=2)
    relay._log("mattermost adapter poll completed", level=2)
    relay._log("mattermost adapter poll completed", level=2)
    relay._log("discord adapter connected")

    output = capsys.readouterr().err
    assert output.count("mattermost adapter poll completed") == 1
    assert "\r\x1b[2K[relay] last message repeated 1 times" in output
    assert "\r\x1b[2K[relay] last message repeated 2 times" in output
    assert output.endswith("\n[relay] discord adapter connected\n")
    assert relay.status["lastVerboseMessage"] == "discord adapter connected"
    assert relay.status["lastVerboseMessageRepeatCount"] == 0
    assert isinstance(relay.status["lastVerboseMessageAt"], float)


def test_status_tracks_repeated_verbose_message_count(monkeypatch) -> None:
    relay = ChannelRelay.__new__(ChannelRelay)
    relay.verbose = 2
    relay.status = {}
    relay._last_log_message = ""
    relay._last_log_repeats = 0
    relay._repeat_summary_open = False
    monkeypatch.setattr("mailbox_channels.channel_relay.sys.stderr.isatty", lambda: False)

    for _ in range(21):
        relay._log("mattermost adapter poll completed", level=2)

    assert relay.status["lastVerboseMessage"] == "mattermost adapter poll completed"
    assert relay.status["lastVerboseMessageRepeatCount"] == 20
