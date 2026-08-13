from pathlib import Path

from mailbox_channels.adapters.telegram_adapter import TelegramAdapter


class Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class Session:
    def __init__(self):
        self.update_reads = 0
        self.posts = []

    def get(self, url, **kwargs):
        if url.endswith("/getMe"):
            return Response({"ok": True, "result": {"id": 99, "username": "relay_bot"}})
        if url.endswith("/getChat"):
            return Response({"ok": True, "result": {"id": -100123, "title": "Operations"}})
        self.update_reads += 1
        if self.update_reads == 1:
            return Response({"ok": True, "result": []})
        return Response({"ok": True, "result": [{
            "update_id": 500,
            "message": {
                "message_id": 42,
                "message_thread_id": 7,
                "text": "hello",
                "chat": {"id": -100123, "type": "supergroup"},
                "from": {"id": 12, "username": "douglas"},
            },
        }]})

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return Response({"ok": True, "result": {"message_id": 43}})


class Mailbox:
    def __init__(self, root: Path):
        self.root = root
        self.sent = []

    def mailbox_dir(self):
        return self.root

    def send(self, recipient, text, **kwargs):
        self.sent.append((recipient, text, kwargs))


def test_telegram_inbound_outbound_threads_and_attachments(monkeypatch, tmp_path: Path) -> None:
    listener = {
        "id": "telegram-one", "direction": "bidirectional", "channel_ids": ["-100123"],
        "bridge_agent": "telegram-agent", "mailbox_recipients": ["worker"],
    }
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    monkeypatch.setattr("mailbox_channels.adapters.telegram_adapter.listeners_for", lambda adapter: [listener])
    session = Session()
    adapter = TelegramAdapter(session=session)
    mailbox = Mailbox(tmp_path)

    assert adapter.configure()
    adapter.cycle(mailbox)
    adapter.cycle(mailbox)

    assert [item[0] for item in mailbox.sent] == ["telegram-agent", "worker"]
    assert mailbox.sent[0][2]["channel_type"] == "telegram"
    assert mailbox.sent[0][2]["thread_id"] == "7"

    attachment = tmp_path / "report.txt"
    attachment.write_text("result", encoding="utf-8")
    adapter.send_message({
        "listener_id": "telegram-one", "channel_id": "-100123", "thread_id": "7",
        "text": "reply", "attachments": [{"path": str(attachment), "mime_type": "text/plain"}],
    })
    assert session.posts[0][0].endswith("/sendMessage")
    assert session.posts[0][1]["json"]["message_thread_id"] == 7
    assert session.posts[1][0].endswith("/sendDocument")
    assert session.posts[1][1]["data"]["chat_id"] == "-100123"


def test_telegram_rejects_unconfigured_chat(monkeypatch, tmp_path: Path) -> None:
    listener = {
        "id": "telegram-one", "direction": "inbound", "channel_ids": ["allowed"],
        "bridge_agent": "telegram-agent", "mailbox_recipients": [],
    }
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    monkeypatch.setattr("mailbox_channels.adapters.telegram_adapter.listeners_for", lambda adapter: [listener])
    adapter = TelegramAdapter(session=Session())
    mailbox = Mailbox(tmp_path)
    assert adapter.configure()
    adapter.cycle(mailbox)
    adapter.cycle(mailbox)
    assert mailbox.sent == []
