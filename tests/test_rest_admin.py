import mailbox_mailboxs.agent_mailbox as am


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
        mailbox_ids=["c1"], config={"x": 1}, dry_run=True, base_url="http://h",
    )
    am.unregister_connector_rest("mm1", dry_run=True, base_url="http://h")
    assert calls[0][0:2] == ("POST", "/v1/connectors")
    assert calls[0][2] == {
        "connector_id": "mm1", "kind": "mattermost", "direction": "inbound",
        "instance": "chat.example", "mailbox_ids": ["c1"], "config": {"x": 1},
        "dry_run": True,
    }
    assert calls[0][3] == "http://h"
    assert calls[1][0:2] == ("POST", "/v1/connectors")
    assert calls[1][2] == {"action": "delete", "connector_id": "mm1", "dry_run": True}


def test_mailbox_rest_helpers_post_expected_bodies(monkeypatch) -> None:
    calls = _capture(monkeypatch)
    am.add_mailbox_rest(
        "team", metadata={"title": "Updates"}, aliases=["alias1"],
        subscribe_all=True, base_url="http://h",
    )
    am.delete_mailbox_rest("team", force=True, base_url="http://h")
    assert calls[0][0:2] == ("POST", "/v1/mailboxs")
    assert calls[0][2] == {
        "mailbox_id": "team", "metadata": {"title": "Updates"},
        "aliases": ["alias1"], "subscribe_all": True,
    }
    assert calls[1][2] == {"action": "delete", "mailbox_id": "team", "force": True}


def test_relay_rest_helpers_post_expected_bodies(monkeypatch) -> None:
    calls = _capture(monkeypatch)
    am.add_relay_rest(
        "team", "mm/chat.example/cid", relay_id="r1", start="beginning",
        dry_run=True, base_url="http://h",
    )
    am.delete_relay_rest("r1", dry_run=True, base_url="http://h")
    assert calls[0][0:2] == ("POST", "/v1/relays")
    assert calls[0][2] == {
        "source_mailbox": "team", "destination": "mm/chat.example/cid",
        "relay_id": "r1", "start": "beginning", "dry_run": True,
    }
    assert calls[1][2] == {"action": "delete", "relay_id": "r1", "dry_run": True}
