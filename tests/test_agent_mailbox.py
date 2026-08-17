import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mailbox_channels import agent_mailbox


def test_status_reports_agents_connectors_channels_and_relays(
    monkeypatch, tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text(json.dumps({
        "version": 1,
        "agents": [{
            "agent_id": "agent-one",
            "presences": [{"presence_id": "agent-one-console"}],
        }],
        "subscriptions": [],
        "connectors": [
            {"id": "in", "adapter": "irc", "direction": "inbound", "enabled": True},
            {"id": "both", "adapter": "mattermost", "instance": "example.test",
             "direction": "bidirectional", "enabled": True},
            {"id": "disabled", "adapter": "discord", "direction": "bidirectional",
             "enabled": False},
        ],
        "relays": [{
            "id": "relay-one", "enabled": True, "source_channel": "source-channel",
            "cursor": "relay-one-cursor", "destination": "mm/example.test/channel-id",
        }],
    }, indent=2), encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    result = agent_mailbox.status(root=tmp_path / "mailbox")

    assert "peers" not in result
    assert [item["agent_id"] for item in result["agents"]] == ["agent-one"]
    assert result["agents"][0]["kind"] == "agent"
    assert result["agents"][0]["cursor"] == "agent-one"
    assert [(item["id"], item["can_listen"], item["can_send"]) for item in result["connectors"]] == [
        ("in", True, False), ("both", True, True),
    ]
    assert all(item["kind"] == "connector" for item in result["connectors"])
    assert {item["id"] for item in result["channels"]} >= {
        "server_events", "agent_to_agent", "agent_to_channel", "agent-one",
    }
    assert [item["id"] for item in result["relays"]] == ["relay-one"]
    assert result["relays"][0]["kind"] == "relay"
    assert "destinations" not in result
    direct = next(item for item in result["channels"] if item["id"] == "agent-one")
    assert (direct["kind"], direct["channel_type"]) == ("channel", "agent_direct")


def test_served_client_runs_as_standalone_script() -> None:
    result = subprocess.run(
        [sys.executable, str(Path(agent_mailbox.__file__).resolve()), "--version"],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("mailbox-client ")


def test_batch_runs_each_nonblank_line_in_order(monkeypatch, tmp_path: Path) -> None:
    batch = tmp_path / "commands.txt"
    batch.write_text("status\n\nmailbox-client check --quiet\n", encoding="utf-8")
    calls = []

    def fake_main(arguments):
        calls.append(arguments)
        return 0

    monkeypatch.setattr(agent_mailbox, "main", fake_main)
    assert agent_mailbox._run_batch(batch) == 0
    assert calls == [["status"], ["check", "--quiet"]]


def test_batch_stops_at_first_failed_command(monkeypatch, tmp_path: Path, capsys) -> None:
    batch = tmp_path / "commands.txt"
    batch.write_text("status\ncheck\nsend later\n", encoding="utf-8")
    calls = []

    def fake_main(arguments):
        calls.append(arguments)
        return 7 if arguments[0] == "check" else 0

    monkeypatch.setattr(agent_mailbox, "main", fake_main)
    assert agent_mailbox._run_batch(batch) == 7
    assert calls == [["status"], ["check"]]
    assert f"{batch}:2" in capsys.readouterr().err


def test_chat_command_is_found_after_position_independent_options() -> None:
    assert agent_mailbox._normalize_chat_dispatch([
        "--on", "mm/0", "--format=text", "list",
    ]) == ["list", "--on", "mm/0", "--format=text"]


def test_jsonl_send_receive_and_cursor(tmp_path: Path) -> None:
    sent = agent_mailbox.send("agent-b", "hello", sender="agent-a", root=tmp_path)
    assert agent_mailbox.receive("agent-b", root=tmp_path) == [sent]
    assert agent_mailbox.receive("agent-b", root=tmp_path) == []


def test_unknown_envelope_fields_are_preserved(tmp_path: Path) -> None:
    sent = agent_mailbox.send(
        "outbound_delivery", "done", root=tmp_path,
        extra_fields={"workflow_run_id": "run-1", "correlation_id": "corr-1"},
    )
    assert sent["workflow_run_id"] == "run-1"
    first_line = (tmp_path / "messages.jsonl").read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(first_line)["correlation_id"] == "corr-1"


def test_every_send_is_copied_to_agent_to_channel_audit(tmp_path: Path) -> None:
    sent = agent_mailbox.send("some-channel", "hello", sender="worker", root=tmp_path)

    copies = agent_mailbox.peek("agent_to_channel", root=tmp_path)
    assert len(copies) == 1
    assert copies[0]["audit_of"] == sent["id"]
    assert copies[0]["dedupe_id"] == sent["dedupe_id"] == sent["id"]
    assert copies[0]["audit_recipient"] == "some-channel"
    assert copies[0]["text"] == "hello"


def test_direct_registered_agent_send_is_also_copied_to_agent_channel(
    tmp_path: Path, monkeypatch,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text(json.dumps({
        "version": 1,
        "agents": [{"agent_id": "worker-two", "presences": []}],
        "connectors": [],
    }), encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    sent = agent_mailbox.send("worker-two", "direct", sender="worker-one", root=tmp_path / "mailbox")

    direct = agent_mailbox.peek("agent_to_agent", root=tmp_path / "mailbox")
    all_sends = agent_mailbox.peek("agent_to_channel", root=tmp_path / "mailbox")
    agent_inbox = agent_mailbox.peek("worker-two", root=tmp_path / "mailbox")
    assert direct[0]["audit_of"] == sent["id"]
    assert all_sends[0]["audit_of"] == sent["id"]
    assert direct[0]["dedupe_id"] == all_sends[0]["dedupe_id"] == sent["id"]
    assert agent_inbox == [sent]


def test_audit_channel_writes_do_not_recursively_audit_themselves(tmp_path: Path) -> None:
    agent_mailbox.send("agent_to_channel", "audit", root=tmp_path)
    assert len((tmp_path / "messages.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_presence_address_stays_intact_while_owner_agent_receives_channel_copy(
    tmp_path: Path, monkeypatch,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "relays.json").write_text(json.dumps({
        "version": 1,
        "agents": [{
            "agent_id": "workspace-codex-agent",
            "presences": [{"presence_id": "codex.star"}],
        }],
        "connectors": [],
    }), encoding="utf-8")
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    sent = agent_mailbox.send(
        "codex.star", "for the workflow agent", sender="another-agent",
        root=tmp_path / "mailbox",
    )

    assert sent["to"] == "codex.star"
    copy = agent_mailbox.peek(
        "workspace-codex-agent", root=tmp_path / "mailbox",
    )[0]
    assert copy["audit_of"] == sent["id"]
    assert copy["audit_recipient"] == "codex.star"
    assert copy["addressed_presence_id"] == "codex.star"
    assert copy["resolved_agent_id"] == "workspace-codex-agent"


def test_url_flag_selects_rest_transport(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(agent_mailbox, "status_rest", lambda **kwargs: calls.append(kwargs) or {"service": "relay"})
    assert agent_mailbox.main(["--url", "http://127.0.0.1:46667", "status"]) == 0
    assert calls == [{"base_url": "http://127.0.0.1:46667"}]


def test_dir_flag_overrides_environment_transports(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv(agent_mailbox.MAILBOX_ENV, str(tmp_path / "environment-mailbox"))
    monkeypatch.setenv(agent_mailbox.MAILBOX_URL_ENV, "http://127.0.0.1:9")

    assert agent_mailbox.main(["--dir", str(tmp_path / "selected-mailbox"), "status"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["directory"] == str((tmp_path / "selected-mailbox").resolve())


def test_mailbox_dir_environment_overrides_rest_environment(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv(agent_mailbox.MAILBOX_ENV, str(tmp_path))
    monkeypatch.setenv(agent_mailbox.MAILBOX_URL_ENV, "http://127.0.0.1:9")

    assert agent_mailbox.main(["status"]) == 0

    assert json.loads(capsys.readouterr().out)["directory"] == str(tmp_path.resolve())


def test_peek_and_named_cursor_do_not_consume_default_cursor(tmp_path: Path) -> None:
    sent = agent_mailbox.send("agent-b", "hello", root=tmp_path)
    assert agent_mailbox.peek("agent-b", root=tmp_path, cursor="audit") == [sent]
    assert agent_mailbox.peek("agent-b", root=tmp_path, cursor="audit") == [sent]
    assert agent_mailbox.receive("agent-b", root=tmp_path) == [sent]


def test_acknowledge_advances_selected_cursor(tmp_path: Path) -> None:
    first = agent_mailbox.send("agent-b", "one", root=tmp_path)
    second = agent_mailbox.send("agent-b", "two", root=tmp_path)
    assert agent_mailbox.acknowledge("agent-b", first["id"], root=tmp_path, cursor="worker")
    assert agent_mailbox.receive("agent-b", root=tmp_path, cursor="worker") == [second]


def test_cursor_initialization_is_create_once_and_remembers_channel(tmp_path: Path) -> None:
    agent_mailbox.send("a", "before login", root=tmp_path)
    result = agent_mailbox.initialize_cursor("a", cursor="agent-1", start="now", root=tmp_path)
    assert result["offset"] > 0
    assert agent_mailbox.cursor_subscriptions("agent-1", root=tmp_path) == ["a"]
    assert agent_mailbox.receive("a", root=tmp_path, cursor="agent-1") == []
    with pytest.raises(ValueError, match="already initialized"):
        agent_mailbox.initialize_cursor("a", cursor="agent-1", start="beginning", root=tmp_path)


def test_ensure_cursor_repairs_subscription_without_overwriting_position(tmp_path: Path) -> None:
    cursor_path = agent_mailbox._cursor_path(tmp_path, "channel:agent-1")
    agent_mailbox._write_cursor(cursor_path, 42)

    result = agent_mailbox.ensure_cursor("channel", cursor="agent-1", root=tmp_path)

    assert result["offset"] == 42
    assert result["start"] == "existing"
    assert agent_mailbox.cursor_subscriptions("agent-1", root=tmp_path) == ["channel"]


def test_trim_messages_before_keeps_recent_records_and_remaps_cursors(tmp_path: Path) -> None:
    old = {"id": "old", "timestamp": "2026-08-01T00:00:00Z", "from": "a",
           "to": "channel", "type": "message", "text": "old"}
    recent = {"id": "recent", "timestamp": "2026-08-18T00:00:00Z", "from": "a",
              "to": "channel", "type": "message", "text": "recent"}
    messages = tmp_path / "messages.jsonl"
    records = [old, recent]
    messages.parent.mkdir(parents=True, exist_ok=True)
    messages.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8",
    )
    agent_mailbox.initialize_cursor("channel", cursor="agent", start="beginning", root=tmp_path)

    result = agent_mailbox.trim_messages_before(
        datetime.fromisoformat("2026-08-16T00:00:00+00:00"), root=tmp_path,
    )

    assert result["removed_records"] == 1
    assert result["retained_records"] == 1
    assert Path(result["backup"]).exists()
    assert agent_mailbox.receive("channel", cursor="agent", root=tmp_path) == [recent]


def test_trim_messages_to_size_can_target_selected_channels(tmp_path: Path) -> None:
    records = [
        {"id": "a1", "timestamp": "2026-08-01T00:00:00Z", "to": "a"},
        {"id": "b1", "timestamp": "2026-08-02T00:00:00Z", "to": "b"},
        {"id": "a2", "timestamp": "2026-08-03T00:00:00Z", "to": "a"},
    ]
    path = tmp_path / "messages.jsonl"
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
    newest_a_size = len(path.read_bytes().splitlines(keepends=True)[2])

    result = agent_mailbox.trim_messages(
        max_bytes=newest_a_size, channels={"a"}, root=tmp_path,
    )

    retained = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [item["id"] for item in retained] == ["b1", "a2"]
    assert result["channels"] == ["a"]


def test_cursor_listing_reports_every_initialized_channel_and_position(tmp_path: Path, capsys) -> None:
    agent_mailbox.send("private-agent", "before login", root=tmp_path)
    initialized = agent_mailbox.initialize_cursor(
        "private-agent", cursor="agent-1", start="now", root=tmp_path,
    )

    assert agent_mailbox.main([
        "--dir", str(tmp_path), "cursors", "--cursor", "agent-1",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["recipients"] == ["private-agent"]
    assert result["positions"] == [{
        "recipient": "private-agent", "cursor": "agent-1", "offset": initialized["offset"],
    }]


def test_agent_add_and_del_commands_manage_agent_and_presence(
        tmp_path: Path, monkeypatch, capsys) -> None:
    config = tmp_path / "config"
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    assert agent_mailbox.main([
        "--dir", str(tmp_path / "mailbox"), "agent-add", "review-agent",
        "--presence", "review-agent-app",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["agent_created"] is True
    assert result["presence_created"] is True
    assert result["agent"]["agent_id"] == "review-agent"
    assert result["agent"]["presences"] == [{
        "presence_id": "review-agent-app",
    }]
    assert result["initial_mailboxes"] == ["review-agent"]

    assert agent_mailbox.main([
        "--dir", str(tmp_path / "mailbox"), "agent-del", "review-agent",
        "--presence", "review-agent-app",
    ]) == 0
    removed_presence = json.loads(capsys.readouterr().out)
    assert removed_presence["presence_removed"] is True
    assert removed_presence["agent_removed"] is False

    assert agent_mailbox.main([
        "--dir", str(tmp_path / "mailbox"), "agent-del", "review-agent",
    ]) == 0
    removed_agent = json.loads(capsys.readouterr().out)
    assert removed_agent["agent_removed"] is True


def test_agent_add_dry_run_previews_without_writing(tmp_path: Path, monkeypatch, capsys) -> None:
    config = tmp_path / "config"
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))

    assert agent_mailbox.main([
        "--dir", str(tmp_path / "mailbox"), "agent-add", "review-agent",
        "--presence", "review-agent-app", "--dry-run",
    ]) == 0

    preview = json.loads(capsys.readouterr().out)
    assert preview == {
        "dry_run": True,
        "agent_id": "review-agent",
        "presence_id": "review-agent-app",
        "would_create_agent": True,
        "would_create_presence": True,
        "would_add_mailboxes": ["review-agent"],
        "initial_mailboxes": ["review-agent"],
    }
    assert not (config / "relays.json").exists()
    assert not (tmp_path / "mailbox" / "messages.jsonl").exists()


def test_agents_listing_includes_read_only_cursor_metadata(
        tmp_path: Path, monkeypatch, capsys) -> None:
    config = tmp_path / "config"
    mailbox = tmp_path / "mailbox"
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))
    agent_mailbox.main(["--dir", str(mailbox), "agent-add", "review-agent"])
    capsys.readouterr()
    agent_mailbox.initialize_cursor(
        "review-agent", cursor="review-agent",
        start="now", root=mailbox,
    )

    assert agent_mailbox.main(["--dir", str(mailbox), "agents"]) == 0

    agent = json.loads(capsys.readouterr().out)["agents"][0]
    assert agent["mailbox"] == "review-agent"
    assert agent["subscriptions"] == [{
            "channel": "review-agent",
            "cursor": "review-agent",
            "offset": agent["subscriptions"][0]["offset"],
    }]


def test_agent_del_purge_dry_run_reports_without_deleting(
        tmp_path: Path, monkeypatch, capsys) -> None:
    config = tmp_path / "config"
    mailbox = tmp_path / "mailbox"
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))
    agent_mailbox.main([
        "--dir", str(mailbox), "agent-add", "review-agent",
        "--presence", "review-agent-app",
    ])
    capsys.readouterr()
    agent_mailbox.send("review-agent", "private direct message", root=mailbox)
    agent_mailbox.initialize_cursor(
        "review-agent", cursor="review-agent",
        start="beginning", root=mailbox,
    )
    before_registry = (config / "relays.json").read_bytes()
    before_messages = (mailbox / "messages.jsonl").read_bytes()

    assert agent_mailbox.main([
        "--dir", str(mailbox), "agent-del", "review-agent", "--purge", "--dry-run",
    ]) == 0

    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True
    assert preview["would_remove_agent"] is True
    assert preview["would_purge_channels"] == ["review-agent", "review-agent-app"]
    assert preview["would_purge_records"] == 1
    assert preview["would_purge_cursor_files"] == 1
    assert (config / "relays.json").read_bytes() == before_registry
    assert (mailbox / "messages.jsonl").read_bytes() == before_messages
    assert agent_mailbox.cursor_subscriptions("review-agent", root=mailbox) == [
        "review-agent"
    ]


def test_agent_del_purge_removes_private_history_but_keeps_global_audit(
        tmp_path: Path, monkeypatch, capsys) -> None:
    config = tmp_path / "config"
    mailbox = tmp_path / "mailbox"
    monkeypatch.setenv("MAILBOX_RELAY_CONFIG_DIR", str(config))
    agent_mailbox.main(["--dir", str(mailbox), "agent-add", "review-agent"])
    capsys.readouterr()
    agent_mailbox.send("review-agent", "private direct message", root=mailbox)

    assert agent_mailbox.main([
        "--dir", str(mailbox), "agent-del", "review-agent", "--purge",
    ]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["agent_removed"] is True
    assert result["purged"] is True
    assert result["purged_records"] == 1
    records = [
        json.loads(line) for line in (mailbox / "messages.jsonl").read_text().splitlines()
    ]
    assert not any(item["to"] in result["purged_channels"] for item in records)
    assert any(
        item["to"] == "agent_to_agent"
        and item.get("audit_recipient") == "review-agent"
        for item in records
    )


def test_cursor_can_start_at_caller_chosen_relative_history_boundary(tmp_path: Path) -> None:
    old = agent_mailbox.send("a", "six days old", root=tmp_path)
    recent = agent_mailbox.send("a", "recent", root=tmp_path)
    records = [json.loads(line) for line in (tmp_path / "messages.jsonl").read_text().splitlines()]
    records[0]["timestamp"] = (
        datetime.now(timezone.utc) - timedelta(days=6)
    ).isoformat().replace("+00:00", "Z")
    (tmp_path / "messages.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8",
    )
    agent_mailbox.initialize_cursor("a", cursor="agent-1", start="7d", root=tmp_path)
    assert [item["id"] for item in agent_mailbox.receive(
        "a", root=tmp_path, cursor="agent-1",
    )] == [old["id"], recent["id"]]


def test_global_poll_reads_every_channel_initialized_for_agent(tmp_path: Path, capsys) -> None:
    for channel in ("a", "b"):
        agent_mailbox.initialize_cursor(channel, cursor="agent-1", start="now", root=tmp_path)
        agent_mailbox.send(channel, f"from {channel}", root=tmp_path)
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "--as", "agent-1", "poll", "--subscriptions", "--checks", "1",
    ]) == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {item["to"] for item in output} == {"a", "b"}


def test_history_search_does_not_move_live_cursor(tmp_path: Path, capsys) -> None:
    sent = agent_mailbox.send("a", "retained", root=tmp_path)
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "history", "a", "--since", "7d",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == sent["id"]
    assert agent_mailbox.receive("a", root=tmp_path, cursor="live") == [sent]


def test_named_mailbox_resolves_relative_directory(tmp_path: Path) -> None:
    config = tmp_path / "config" / "mailboxes.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"mailboxes": {"team": {"dir": "../team-mailbox"}}}), encoding="utf-8")
    directory, url = agent_mailbox.named_mailbox("team", config)
    assert directory == (tmp_path / "team-mailbox").resolve()
    assert url is None


def test_cli_filter_count_and_text_output(tmp_path: Path, capsys) -> None:
    agent_mailbox.send("agent-b", "hello", sender="a", message_type="task", root=tmp_path)
    agent_mailbox.send("agent-b", "ignore", sender="x", root=tmp_path)
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "--format", "text", "peek", "agent-b", "--where", "type=task",
    ]) == 0
    assert "a: hello" in capsys.readouterr().out
    assert agent_mailbox.main(["--dir", str(tmp_path), "unread-count", "agent-b"]) == 0
    assert capsys.readouterr().out.strip() == "2"


def test_cli_text_output_explains_chat_server_diagnostics(tmp_path: Path, capsys) -> None:
    agent_mailbox.send(
        "worker", "discord chat server connection failed: offline",
        sender="local-discord-server", message_type="chat_server_status",
        channel_type="discord", root=tmp_path,
        extra_fields={
            "adapter": "discord", "connection_state": "connection_failed",
            "service_context": {
                "adapter": "discord", "connector_ids": ["discord-main"], "channel_ids": ["ops"],
            },
            "diagnostic": {
                "operation": "connect_or_poll", "error_type": "ConnectionError",
                "error_message": "offline", "will_retry": True,
            },
        },
    )
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "--format", "text", "peek", "worker",
    ]) == 0
    output = capsys.readouterr().out
    assert "local-discord-server: discord chat server connection failed: offline" in output
    assert "adapter=discord; state=connection_failed" in output
    assert "connectors=discord-main; channels=ops" in output
    assert "error=ConnectionError: offline; will_retry=true" in output


def test_rest_token_is_sent_as_bearer_authorization(monkeypatch) -> None:
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def read(self): return b'{"service":"relay"}'

    captured = []
    monkeypatch.setattr(agent_mailbox.urllib.request, "urlopen",
                        lambda request, timeout: captured.append(request) or Response())
    agent_mailbox.REST_TOKEN = "secret-token"
    try:
        assert agent_mailbox.status_rest(base_url="http://relay.example") == {"service": "relay"}
    finally:
        agent_mailbox.REST_TOKEN = None
    assert captured[0].get_header("Authorization") == "Bearer secret-token"


def test_curl_switch_prints_redacted_send_without_network(monkeypatch, capsys) -> None:
    monkeypatch.setattr(agent_mailbox.urllib.request, "urlopen",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network called")))
    assert agent_mailbox.main([
        "--url", "https://relay.example", "--token", "super-secret", "--curl",
        "--from", "worker-1", "send", "agent-beta", "done", "--channel-type", "slack",
        "--channel-id", "C1",
    ]) == 0
    command = capsys.readouterr().out
    assert "curl" in command and "/v1/messages" in command
    assert "worker-1" in command and "C1" in command
    assert "super-secret" not in command
    assert "REDACTED_TOKEN" in command


def test_curl_switch_shows_non_consuming_peek(capsys) -> None:
    assert agent_mailbox.main([
        "--url", "https://relay.example", "--curl", "peek", "worker-1", "--cursor", "audit",
    ]) == 0
    command = capsys.readouterr().out
    assert "advance=false" in command and "cursor=audit" in command


def test_curl_switch_is_accepted_anywhere(monkeypatch, capsys) -> None:
    monkeypatch.setenv("AGENT_MAILBOX_URL", "https://relay.example")
    placements = [
        ["--curl", "send", "agent-beta", "done"],
        ["send", "--curl", "agent-beta", "done"],
        ["send", "agent-beta", "--curl", "done"],
        ["send", "agent-beta", "done", "--curl"],
    ]
    for arguments in placements:
        assert agent_mailbox.main(arguments) == 0
        assert "/v1/messages" in capsys.readouterr().out


def test_double_dash_preserves_switch_looking_message_text(tmp_path, capsys) -> None:
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "send", "agent-beta", "--", "--curl --token secret --verbose",
    ]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert sent["text"] == "--curl --token secret --verbose"


def test_curl_after_double_dash_is_not_treated_as_flag(tmp_path, capsys) -> None:
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "send", "agent-beta", "--", "--curl",
    ]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert sent["text"] == "--curl"


def test_input_supplies_send_text_from_any_option_position(tmp_path, capsys) -> None:
    source = tmp_path / "message.txt"
    source.write_text("first line\n--curl remains text\n", encoding="utf-8")
    mailbox = tmp_path / "mailbox"
    placements = [
        ["--dir", str(mailbox), "--input", str(source), "send", "agent-beta"],
        ["--dir", str(mailbox), "send", "--input", str(source), "agent-beta"],
        ["--dir", str(mailbox), "send", "agent-beta", "--input", str(source)],
    ]
    for arguments in placements:
        assert agent_mailbox.main(arguments) == 0
        sent = json.loads(capsys.readouterr().out)
        assert sent["text"] == "first line\n--curl remains text\n"


def test_input_after_double_dash_is_literal_text(tmp_path, capsys) -> None:
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "send", "agent-beta", "--", "--input message.txt",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == "--input message.txt"


def test_removed_file_option_is_rejected_before_double_dash(tmp_path) -> None:
    with pytest.raises(SystemExit):
        agent_mailbox.main([
            "--dir", str(tmp_path), "send", "agent-beta", "--file", "message.txt",
        ])


def test_removed_file_option_remains_literal_after_double_dash(tmp_path, capsys) -> None:
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "send", "agent-beta", "--", "--file message.txt",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == "--file message.txt"


def test_run_executes_complete_json_command_document(tmp_path, capsys) -> None:
    mailbox = tmp_path / "mailbox"
    command_file = tmp_path / "send.json"
    command_file.write_text(json.dumps({
        "command": "send",
        "dir": str(mailbox),
        "recipient": "agent-beta",
        "text": "--curl is message text",
        "channel_type": "telegram",
        "channel_id": "123",
    }), encoding="utf-8")

    assert agent_mailbox.main(["--run", str(command_file)]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert sent["text"] == "--curl is message text"
    assert sent["channel_type"] == "telegram"


def test_run_accepts_exact_argument_array(tmp_path, capsys) -> None:
    command_file = tmp_path / "status.json"
    command_file.write_text(json.dumps({"args": ["--dir", str(tmp_path), "status"]}), encoding="utf-8")
    assert agent_mailbox.main([f"--run={command_file}"]) == 0
    assert Path(json.loads(capsys.readouterr().out)["directory"]) == tmp_path


def test_run_rejects_mixed_cli_arguments(tmp_path) -> None:
    command_file = tmp_path / "status.json"
    command_file.write_text(json.dumps({"command": "status", "dir": str(tmp_path)}), encoding="utf-8")
    with pytest.raises(SystemExit):
        agent_mailbox.main(["--run", str(command_file), "--verbose"])


def test_nobuffer_is_accepted_anywhere(monkeypatch, tmp_path, capsys) -> None:
    calls = []
    monkeypatch.setattr(agent_mailbox, "_enable_unbuffered_output", lambda: calls.append(True))
    placements = [
        ["--nobuffer", "--dir", str(tmp_path), "status"],
        ["--dir", str(tmp_path), "status", "--nobuffer"],
    ]
    for arguments in placements:
        assert agent_mailbox.main(arguments) == 0
        capsys.readouterr()
    assert calls == [True, True]


def test_nobuffer_after_double_dash_is_literal_text(tmp_path, capsys) -> None:
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "send", "agent-beta", "--", "--nobuffer",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == "--nobuffer"


def test_run_document_supports_nobuffer(monkeypatch, tmp_path, capsys) -> None:
    calls = []
    monkeypatch.setattr(agent_mailbox, "_enable_unbuffered_output", lambda: calls.append(True))
    command_file = tmp_path / "status.json"
    command_file.write_text(json.dumps({
        "command": "status", "dir": str(tmp_path), "nobuffer": True,
    }), encoding="utf-8")
    assert agent_mailbox.main(["--run", str(command_file)]) == 0
    capsys.readouterr()
    assert calls == [True]


def test_to_supplies_send_recipient_from_any_option_position(tmp_path, capsys) -> None:
    placements = [
        ["--dir", str(tmp_path), "--to", "agent-beta", "send", "hello"],
        ["--dir", str(tmp_path), "send", "--to", "agent-beta", "hello"],
        ["--dir", str(tmp_path), "send", "hello", "--to", "agent-beta"],
    ]
    for arguments in placements:
        assert agent_mailbox.main(arguments) == 0
        assert json.loads(capsys.readouterr().out)["to"] == "agent-beta"


def test_positional_external_endpoint_routes_through_channel_relay(tmp_path, capsys) -> None:
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "send",
        "mm/chat.singularitynet.io/channel-1", "hello",
    ]) == 0

    record = json.loads(capsys.readouterr().out)
    assert record["to"] == "outbound_delivery"
    assert record["text"] == "hello"
    assert record["channel_type"] == "mattermost"
    assert record["channel_id"] == "channel-1"
    assert record["endpoint_address"] == "mm/chat.singularitynet.io/channel-1"


def test_positional_and_to_external_endpoints_have_matching_routing(tmp_path, capsys) -> None:
    endpoint = "mm/chat.singularitynet.io/channel-1"
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "send", endpoint, "positional",
    ]) == 0
    positional = json.loads(capsys.readouterr().out)

    assert agent_mailbox.main([
        "--dir", str(tmp_path), "--to", endpoint, "send", "global-to",
    ]) == 0
    global_to = json.loads(capsys.readouterr().out)

    routing_fields = ("to", "channel_type", "channel_id", "endpoint_address")
    assert {field: positional[field] for field in routing_fields} == {
        field: global_to[field] for field in routing_fields
    }


def test_to_and_positional_recipient_are_mutually_exclusive(tmp_path) -> None:
    with pytest.raises(SystemExit):
        agent_mailbox.main([
            "--dir", str(tmp_path), "send", "agent-beta", "hello", "--to", "other",
        ])


def test_to_supplies_receiver_for_read_commands(tmp_path, capsys) -> None:
    agent_mailbox.send("agent-beta", "hello", root=tmp_path)
    assert agent_mailbox.main(["--dir", str(tmp_path), "peek", "--to", "agent-beta"]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == "hello"


def test_from_and_to_work_after_send_command(tmp_path, capsys) -> None:
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "send", "hello", "--from", "agent-a", "--to", "agent-b",
    ]) == 0
    record = json.loads(capsys.readouterr().out)
    assert (record["from"], record["to"]) == ("agent-a", "agent-b")


def test_ack_uses_from_for_cursor_owner(tmp_path, capsys) -> None:
    record = agent_mailbox.send("symbolic-workbench-codex", "hello", root=tmp_path)
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "ack", "--from", "symbolic-workbench-codex", record["id"],
    ]) == 0
    assert json.loads(capsys.readouterr().out)["acknowledged"] is True


def test_ack_uses_as_for_cursor_owner(tmp_path, capsys) -> None:
    record = agent_mailbox.send("symbolic-workbench-codex", "hello", root=tmp_path)
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "ack", "--as", "symbolic-workbench-codex", record["id"],
    ]) == 0
    assert json.loads(capsys.readouterr().out)["acknowledged"] is True


def test_run_document_accepts_to_alias(tmp_path, capsys) -> None:
    command_file = tmp_path / "send.json"
    command_file.write_text(json.dumps({
        "command": "send", "dir": str(tmp_path), "to": "agent-beta", "text": "hello",
    }), encoding="utf-8")
    assert agent_mailbox.main(["--run", str(command_file)]) == 0
    assert json.loads(capsys.readouterr().out)["to"] == "agent-beta"
