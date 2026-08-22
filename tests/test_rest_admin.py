import mailbox_channels.agent_mailbox as am


def _capture(monkeypatch):
    calls = []

    def fake(method, path, payload=None, *, base_url=None):
        calls.append((method, path, payload, base_url))
        return {"ok": True}

    monkeypatch.setattr(am, "_rest_request", fake)
    return calls


def test_connector_rest_helpers_post_expected_bodies(monkeypatch) -> None:
    calls = _capture(monkeypatch)
    am.register_connector_rest(
        "mm1", "mattermost", direction="inbound", instance="chat.example",
        channel_ids=["c1"], config={"x": 1}, dry_run=True, base_url="http://h",
    )
    am.unregister_connector_rest("mm1", dry_run=True, base_url="http://h")
    assert calls[0][0:2] == ("POST", "/v1/connectors")
    assert calls[0][2] == {
        "connector_id": "mm1", "kind": "mattermost", "direction": "inbound",
        "instance": "chat.example", "channel_ids": ["c1"], "config": {"x": 1},
        "dry_run": True,
    }
    assert calls[0][3] == "http://h"
    assert calls[1][0:2] == ("POST", "/v1/connectors")
    assert calls[1][2] == {"action": "delete", "connector_id": "mm1", "dry_run": True}


def test_channel_rest_helpers_post_expected_bodies(monkeypatch) -> None:
    calls = _capture(monkeypatch)
    am.add_channel_rest(
        "team", metadata={"title": "Updates"}, aliases=["alias1"],
        subscribe_all=True, base_url="http://h",
    )
    am.delete_channel_rest("team", force=True, base_url="http://h")
    assert calls[0][0:2] == ("POST", "/v1/channels")
    assert calls[0][2] == {
        "channel_id": "team", "metadata": {"title": "Updates"},
        "aliases": ["alias1"], "subscribe_all": True,
    }
    assert calls[1][2] == {"action": "delete", "channel_id": "team", "force": True}


def test_relay_rest_helpers_post_expected_bodies(monkeypatch) -> None:
    calls = _capture(monkeypatch)
    am.add_relay_rest(
        "team", "mm/chat.example/cid", relay_id="r1", start="beginning",
        dry_run=True, base_url="http://h",
    )
    am.delete_relay_rest("r1", dry_run=True, base_url="http://h")
    assert calls[0][0:2] == ("POST", "/v1/relays")
    assert calls[0][2] == {
        "source_channel": "team", "destination": "mm/chat.example/cid",
        "relay_id": "r1", "start": "beginning", "dry_run": True,
    }
    assert calls[1][2] == {"action": "delete", "relay_id": "r1", "dry_run": True}
