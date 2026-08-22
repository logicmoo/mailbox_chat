"""Tests for the exec command: run now (local), enqueue, server-run, env/cwd, curl."""

import json
from pathlib import Path

import pytest

from mailbox_channels import agent_mailbox, channel_store, cmd_client_page


def _queue_messages(root: Path) -> list[dict]:
    document = channel_store.load_channel(root, agent_mailbox.SERVER_WORKER_QUEUE)
    return [record for _key, record in channel_store.ordered_messages(document)]


def test_exec_now_local_is_identical_to_direct_command(tmp_path: Path, capsys) -> None:
    store = tmp_path / "store"
    assert agent_mailbox.main(["--dir", str(store), "exec", "send", "worker-1", "hello"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["to"] == "worker-1"
    assert record["text"] == "hello"


def test_exec_now_local_preserves_switchlike_inner_flags(tmp_path: Path, capsys) -> None:
    store = tmp_path / "store"
    # --from is a global "anywhere" flag; the exec separator must keep it inner.
    assert agent_mailbox.main([
        "--dir", str(store), "exec", "send", "worker-1", "hi", "--from", "someone",
    ]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["to"] == "worker-1" and record["from"] == "someone"


def test_exec_enque_local_appends_work_item(tmp_path: Path, capsys) -> None:
    store = tmp_path / "store"
    assert agent_mailbox.main([
        "--dir", str(store), "exec", "--priority=enque", "send", "worker-1", "do it",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["enqueued"] is True
    assert result["queue"] == agent_mailbox.SERVER_WORKER_QUEUE

    items = _queue_messages(store)
    assert len(items) == 1
    assert items[0]["type"] == "work_item"
    assert items[0]["argv"] == ["send", "worker-1", "do it"]
    assert items[0]["program"] == "mailbox-client"
    assert items[0]["priority"] == "enque"


def test_exec_enque_local_records_env_and_cwd(tmp_path: Path, capsys) -> None:
    store = tmp_path / "store"
    assert agent_mailbox.main([
        "--dir", str(store), "exec", "--priority=enque",
        "--env-vars", "API_KEY=xyz", "--env-vars=DEBUG=1", "--cwd", str(tmp_path),
        "status",
    ]) == 0
    capsys.readouterr()
    item = _queue_messages(store)[0]
    assert item["env"] == {"API_KEY": "xyz", "DEBUG": "1"}
    assert item["cwd"] == str(tmp_path)


def test_exec_program_without_server_errors(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        agent_mailbox.main([
            "--dir", str(tmp_path), "exec", "--program", "mailbox-client", "status",
        ])


def test_exec_requires_an_inner_command(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        agent_mailbox.main(["--dir", str(tmp_path), "exec"])


def test_exec_curl_enque_and_program(monkeypatch, capsys) -> None:
    monkeypatch.setenv("AGENT_MAILBOX_URL", "https://relay.example")
    monkeypatch.delenv("AGENT_MAILBOX_DIR", raising=False)

    assert agent_mailbox.main(["--curl", "exec", "--priority=enque", "send", "x", "hi"]) == 0
    enque = capsys.readouterr().out
    assert enque.startswith("curl ") and "-X POST" in enque and "/v1/exec" in enque
    assert '"priority": "enque"' in enque

    assert agent_mailbox.main([
        "--curl", "exec", "--program", "mailbox-client", "status",
    ]) == 0
    program = capsys.readouterr().out
    assert "/v1/exec" in program and '"program": "mailbox-client"' in program


def test_exec_curl_now_local_has_no_rest_equivalent(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MAILBOX_URL", "https://relay.example")
    with pytest.raises(SystemExit):
        agent_mailbox.main(["--curl", "exec", "status"])


def test_run_mailbox_client_argv_runs_and_captures_output() -> None:
    result = cmd_client_page.run_mailbox_client_argv(
        ["-h"], relay_url="http://127.0.0.1:1", token="", timeout=60,
    )
    assert result["returncode"] == 0
    assert "usage: mailbox-client" in str(result["output"])
