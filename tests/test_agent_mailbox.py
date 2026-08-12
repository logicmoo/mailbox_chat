import json
from pathlib import Path

import pytest

from mailbox_channel_relay_bridging_proxy import agent_mailbox


def test_jsonl_send_receive_and_cursor(tmp_path: Path) -> None:
    sent = agent_mailbox.send("agent-b", "hello", sender="agent-a", root=tmp_path)
    assert agent_mailbox.receive("agent-b", root=tmp_path) == [sent]
    assert agent_mailbox.receive("agent-b", root=tmp_path) == []


def test_unknown_envelope_fields_are_preserved(tmp_path: Path) -> None:
    sent = agent_mailbox.send(
        "channel-relay", "done", root=tmp_path,
        extra_fields={"workflow_run_id": "run-1", "correlation_id": "corr-1"},
    )
    assert sent["workflow_run_id"] == "run-1"
    assert json.loads((tmp_path / "messages.jsonl").read_text(encoding="utf-8"))["correlation_id"] == "corr-1"


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
        "--from", "worker-1", "send", "planner", "done", "--channel-type", "slack",
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
        ["--curl", "send", "planner", "done"],
        ["send", "--curl", "planner", "done"],
        ["send", "planner", "--curl", "done"],
        ["send", "planner", "done", "--curl"],
    ]
    for arguments in placements:
        assert agent_mailbox.main(arguments) == 0
        assert "/v1/messages" in capsys.readouterr().out


def test_double_dash_preserves_switch_looking_message_text(tmp_path, capsys) -> None:
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "send", "planner", "--", "--curl --token secret --verbose",
    ]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert sent["text"] == "--curl --token secret --verbose"


def test_curl_after_double_dash_is_not_treated_as_flag(tmp_path, capsys) -> None:
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "send", "planner", "--", "--curl",
    ]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert sent["text"] == "--curl"


def test_input_supplies_send_text_from_any_option_position(tmp_path, capsys) -> None:
    source = tmp_path / "message.txt"
    source.write_text("first line\n--curl remains text\n", encoding="utf-8")
    mailbox = tmp_path / "mailbox"
    placements = [
        ["--dir", str(mailbox), "--input", str(source), "send", "planner"],
        ["--dir", str(mailbox), "send", "--input", str(source), "planner"],
        ["--dir", str(mailbox), "send", "planner", "--input", str(source)],
    ]
    for arguments in placements:
        assert agent_mailbox.main(arguments) == 0
        sent = json.loads(capsys.readouterr().out)
        assert sent["text"] == "first line\n--curl remains text\n"


def test_input_after_double_dash_is_literal_text(tmp_path, capsys) -> None:
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "send", "planner", "--", "--input message.txt",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == "--input message.txt"


def test_removed_file_option_is_rejected_before_double_dash(tmp_path) -> None:
    with pytest.raises(SystemExit):
        agent_mailbox.main([
            "--dir", str(tmp_path), "send", "planner", "--file", "message.txt",
        ])


def test_removed_file_option_remains_literal_after_double_dash(tmp_path, capsys) -> None:
    assert agent_mailbox.main([
        "--dir", str(tmp_path), "send", "planner", "--", "--file message.txt",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == "--file message.txt"


def test_run_executes_complete_json_command_document(tmp_path, capsys) -> None:
    mailbox = tmp_path / "mailbox"
    command_file = tmp_path / "send.json"
    command_file.write_text(json.dumps({
        "command": "send",
        "dir": str(mailbox),
        "recipient": "planner",
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
