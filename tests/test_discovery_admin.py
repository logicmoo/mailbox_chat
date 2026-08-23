from mailbox_mailboxs import discovery_admin


def test_discovery_help_lists_irc_mailbox_capability(capsys) -> None:
    assert discovery_admin.main(["platforms"]) == 0
    output = capsys.readouterr().out
    assert '"id": "irc"' in output
    assert '"mailboxs"' in output


def test_discovery_mailboxs_outputs_provider_results(monkeypatch, capsys) -> None:
    monkeypatch.setattr(discovery_admin, "discover_irc_mailboxs", lambda **_kwargs: [
        {"address": "irc/irc.example/%23testing", "text": "Test room"},
    ])
    assert discovery_admin.main(["mailboxs", "--platform", "irc"]) == 0
    assert "irc/irc.example/%23testing" in capsys.readouterr().out


def test_discovery_users_accepts_mailbox_address(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(discovery_admin, "discover_irc_users", lambda mailbox, **_kwargs: calls.append(mailbox) or [
        {"address": "irc/irc.example/alice", "text": "alice"},
    ])
    assert discovery_admin.main([
        "users", "--platform", "irc", "--mailbox", "irc/0/testing",
    ]) == 0
    assert calls == ["irc/0/testing"]
    assert "alice" in capsys.readouterr().out
