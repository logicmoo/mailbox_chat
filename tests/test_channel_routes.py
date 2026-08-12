from mailbox_channel_relay_bridging_proxy import channel_routes


class Mailbox:
    def __init__(self): self.sent = []
    def send(self, recipient, text, **kwargs): self.sent.append((recipient, text, kwargs))


def test_presence_controller_and_unregistered_relay_agent(monkeypatch) -> None:
    monkeypatch.setattr(channel_routes, "load_routes", lambda: [
        {"id": "direct", "enabled": True, "source": {"listener_id": "source"},
         "destinations": [{"adapter": "irc", "listener_id": "irc-one", "channel_id": "#target"}],
         "controller": {"type": "presence_controller", "presence_id": "p1"}},
        {"id": "agent", "enabled": True, "source": {"listener_id": "source"},
         "destinations": [{"adapter": "matrix", "channel_id": "!room:x"}],
         "controller": {"type": "relay_agent", "mailbox_recipient": "not-in-agents-json"}},
    ])
    mailbox = Mailbox()
    channel_routes.dispatch_routes(mailbox, listener_id="source", channel_id="C1",
                                   message={"text": "hello", "origin_id": "origin-1"})
    assert mailbox.sent[0][0] == "channel-relay"
    assert mailbox.sent[0][2]["extra_fields"]["presence_id"] == "p1"
    assert mailbox.sent[1][0] == "not-in-agents-json"
