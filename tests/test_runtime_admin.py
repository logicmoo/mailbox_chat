import json

from mailbox_channel_relay_bridging_proxy import runtime_admin


class Mailbox:
    def __init__(self): self.sent = []
    def send(self, recipient, text, **kwargs): self.sent.append((recipient, text, kwargs))


def test_trusted_irc_compatible_command_persists_route(tmp_path, monkeypatch) -> None:
    path = tmp_path / "listeners.json"
    payload = {"version": 1, "listeners": [
        {"id": "irc-source", "adapter": "irc", "enabled": True,
         "direction": "bidirectional", "channel_ids": ["#source"],
         "trusted_admins": ["admin"]},
        {"id": "slack-target", "adapter": "slack", "enabled": True,
         "direction": "bidirectional", "channel_ids": ["C1"]},
    ], "routes": []}
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(runtime_admin, "listeners_file", lambda: path)
    monkeypatch.setattr(runtime_admin, "load_listeners", lambda: payload["listeners"])
    mailbox = Mailbox()
    assert runtime_admin.handle_admin_command(
        mailbox, listener_id="irc-source", channel_id="#source", author="admin",
        text="!relay attach slack-target C2 presence",
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["routes"][0]["destinations"][0]["channel_id"] == "C2"
    assert saved["routes"][0]["controller"]["type"] == "presence_controller"


def test_untrusted_command_is_denied_without_config_change(tmp_path, monkeypatch) -> None:
    path = tmp_path / "listeners.json"
    payload = {"version": 1, "listeners": [
        {"id": "irc-source", "adapter": "irc", "enabled": True,
         "direction": "bidirectional", "channel_ids": ["#source"], "trusted_admins": ["admin"]},
    ], "routes": []}
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(runtime_admin, "listeners_file", lambda: path)
    monkeypatch.setattr(runtime_admin, "load_listeners", lambda: payload["listeners"])
    mailbox = Mailbox()
    assert runtime_admin.handle_admin_command(
        mailbox, listener_id="irc-source", channel_id="#source", author="intruder",
        text="!relay attach nowhere C2 presence",
    )
    assert json.loads(path.read_text(encoding="utf-8"))["routes"] == []
    assert "denied" in mailbox.sent[0][1].lower()
