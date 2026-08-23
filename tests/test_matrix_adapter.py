from pathlib import Path

from mailbox_channels.adapters.matrix_adapter import MatrixAdapter


class Response:
    def __init__(self, payload): self.payload = payload
    def json(self): return self.payload
    def raise_for_status(self): return None


class Session:
    def __init__(self): self.syncs = 0; self.puts = []
    def get(self, url, **kwargs):
        if url.endswith("/account/whoami"): return Response({"user_id": "@bot:example.org"})
        self.syncs += 1
        if self.syncs == 1: return Response({"next_batch": "s1", "rooms": {}})
        return Response({"next_batch": "s2", "rooms": {"join": {"!room:example.org": {
            "timeline": {"events": [{"type": "m.room.message", "event_id": "$e1",
            "sender": "@user:example.org", "content": {"msgtype": "m.text", "body": "hello"}}]}
        }}}})
    def put(self, url, **kwargs): self.puts.append((url, kwargs)); return Response({"event_id": "$sent"})


class Mailbox:
    def __init__(self, root: Path): self.root = root; self.sent = []
    def mailbox_dir(self): return self.root
    def send(self, recipient, text, **kwargs): self.sent.append((recipient, text, kwargs))


def test_matrix_sync_and_send(monkeypatch, tmp_path: Path) -> None:
    connector = {"id": "matrix-one", "direction": "bidirectional",
                "homeserver": "https://matrix.example.org", "token_env": "MATRIX_TOKEN",
                "channel_ids": ["!room:example.org"], "bridge_agent": "matrix-agent",
                "mailbox_recipients": ["worker"]}
    monkeypatch.setenv("MATRIX_TOKEN", "secret")
    monkeypatch.setattr("mailbox_channels.adapters.matrix_adapter.connectors_for", lambda adapter: [connector])
    session, mailbox = Session(), Mailbox(tmp_path)
    adapter = MatrixAdapter(session=session)
    assert adapter.configure()
    adapter.cycle(mailbox)
    adapter.cycle(mailbox)
    assert [item[0] for item in mailbox.sent] == ["matrix-agent", "worker"]
    adapter.send_message({"connector_id": "matrix-one", "channel_id": "!room:example.org", "text": "reply"})
    assert session.puts[0][1]["json"]["body"] == "reply"
