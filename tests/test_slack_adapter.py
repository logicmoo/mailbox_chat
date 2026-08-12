from pathlib import Path

from mailbox_channel_relay_bridging_proxy.slack_adapter import SlackAdapter


class Response:
    content = b""
    def __init__(self, payload): self.payload = payload
    def json(self): return self.payload
    def raise_for_status(self): return None


class Session:
    def __init__(self): self.history = 0; self.posts = []
    def get(self, url, **kwargs):
        if url.endswith("/conversations.history"):
            self.history += 1
            return Response({"ok": True, "messages": [] if self.history == 1 else [
                {"ts": "2.0", "text": "hello", "user": "U1"}
            ]})
        return Response({"ok": True})
    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("/auth.test"): return Response({"ok": True, "user_id": "BOT"})
        return Response({"ok": True, "ts": "3.0"})


class Mailbox:
    def __init__(self, root: Path): self.root = root; self.sent = []
    def mailbox_dir(self): return self.root
    def send(self, recipient, text, **kwargs): self.sent.append((recipient, text, kwargs))


def test_slack_history_and_send(monkeypatch, tmp_path: Path) -> None:
    listener = {"id": "slack-one", "direction": "bidirectional", "token_env": "SLACK_TOKEN",
                "channel_ids": ["C1"], "bridge_agent": "slack-agent", "mailbox_recipients": ["worker"]}
    monkeypatch.setenv("SLACK_TOKEN", "secret")
    monkeypatch.setattr("mailbox_channel_relay_bridging_proxy.slack_adapter.listeners_for", lambda adapter: [listener])
    monkeypatch.setattr("mailbox_channel_relay_bridging_proxy.channel_routes.load_routes", lambda: [])
    session, mailbox = Session(), Mailbox(tmp_path)
    adapter = SlackAdapter(session=session)
    assert adapter.configure()
    adapter.cycle(mailbox)
    adapter.cycle(mailbox)
    assert [item[0] for item in mailbox.sent] == ["slack-agent", "worker"]
    adapter.send_message({"listener_id": "slack-one", "channel_id": "C1", "text": "reply"})
    assert any(url.endswith("/chat.postMessage") for url, _kwargs in session.posts)
