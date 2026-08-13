import json

from mailbox_channels import agent_mailbox
from mailbox_channels.local_channels import set_subscription, subscribers, subscriptions


def test_local_channel_subscription_is_durable(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    path = config / "relays.json"
    path.write_text('{"version":1,"listeners":[]}', encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    set_subscription("server_events", "symbolic-workbench-codex", enabled=True)
    assert subscribers("server_events") == ["symbolic-workbench-codex"]
    assert subscriptions("symbolic-workbench-codex") == ["server_events"]
    assert json.loads(path.read_text(encoding="utf-8"))["local_channels"][0]["subscribers"] == [
        "symbolic-workbench-codex",
    ]
    set_subscription("server_events", "symbolic-workbench-codex", enabled=False)
    assert subscribers("server_events") == []


def test_mailbox_client_manages_local_channel_subscription(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text('{"version":1,"listeners":[]}', encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    assert agent_mailbox.main(["subscribe", "server_events", "--to", "omegaclaw-min"]) == 0
    assert json.loads(capsys.readouterr().out)["subscribed"] is True
    assert agent_mailbox.main(["subscriptions", "--to", "omegaclaw-min"]) == 0
    assert json.loads(capsys.readouterr().out)["channels"] == ["server_events"]
    assert agent_mailbox.main(["unsubscribe", "server_events", "--to", "omegaclaw-min"]) == 0
    assert json.loads(capsys.readouterr().out)["subscribed"] is False


def test_poll_can_idempotently_ensure_all_declared_subscriptions(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text('{"version":1,"listeners":[]}', encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    arguments = [
        "--dir", str(tmp_path / "mailbox"), "poll", "--to", "symbolic-workbench-codex",
        "--subscribed", "server_events,mm_3423423434234,mm_2342444444444",
        "--checks", "1",
    ]
    assert agent_mailbox.main(arguments) == 0
    assert agent_mailbox.main(arguments) == 0
    capsys.readouterr()
    assert subscriptions("symbolic-workbench-codex") == [
        "server_events", "mm_3423423434234", "mm_2342444444444",
    ]
