from pathlib import Path

from mailbox_channel_relay_bridging_proxy import agent_mailbox
from mailbox_channel_relay_bridging_proxy.channel_relay import ChannelRelay, RELAY_RECIPIENT


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

    def get(self, url, **_kwargs):
        if url.endswith("/users/me"):
            return Response({"id": "bot"})
        if url.endswith("/users/bot/channels"):
            return Response([])
        post = {"id": "p1", "user_id": "human", "message": "hello", "create_at": 2, "file_ids": []}
        return Response({"order": ["p1"], "posts": {"p1": post}})

    def post(self, url, **kwargs):
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
    monkeypatch.setattr("mailbox_channel_relay_bridging_proxy.channel_relay.sys.stderr.isatty", lambda: True)

    relay._log("mattermost adapter poll completed", level=2)
    relay._log("mattermost adapter poll completed", level=2)
    relay._log("mattermost adapter poll completed", level=2)
    relay._log("discord adapter connected")

    output = capsys.readouterr().err
    assert output.count("mattermost adapter poll completed") == 1
    assert "\r\x1b[2K[relay] last message repeated 1 times" in output
    assert "\r\x1b[2K[relay] last message repeated 2 times" in output
    assert output.endswith("\n[relay] discord adapter connected\n")
