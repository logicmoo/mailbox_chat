from pathlib import Path

from mailbox_channel_relay_bridging_proxy import agent_mailbox
from mailbox_channel_relay_bridging_proxy.channel_relay import ChannelRelay, RELAY_RECIPIENT


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
    assert agent_mailbox.receive("local-agent", root=tmp_path)[0]["text"] == "hello"
    assert session.posts[0]["message"] == "done"
