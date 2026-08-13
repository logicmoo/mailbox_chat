from mailbox_channels import discovery_admin


def test_discovery_help_lists_irc_channel_capability(capsys) -> None:
    assert discovery_admin.main(["platforms"]) == 0
    output = capsys.readouterr().out
    assert '"id": "irc"' in output
    assert '"channels"' in output


def test_discovery_channels_outputs_provider_results(monkeypatch, capsys) -> None:
    monkeypatch.setattr(discovery_admin, "discover_irc_channels", lambda **_kwargs: [
        {"address": "irc/irc.example/%23testing", "text": "Test room"},
    ])
    assert discovery_admin.main(["channels", "--platform", "irc"]) == 0
    assert "irc/irc.example/%23testing" in capsys.readouterr().out
