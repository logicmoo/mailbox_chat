from pathlib import Path


def test_agent_mailbox_cmd_is_location_independent() -> None:
    launcher = (Path(__file__).resolve().parents[1] / "agent-mailbox.cmd").read_text(encoding="utf-8")
    assert "%~dp0" in launcher
    assert "src;%PYTHONPATH%" in launcher
    assert "mailbox_channel_relay_bridging_proxy.agent_mailbox %*" in launcher
    assert "exit /b %ERRORLEVEL%" in launcher


def test_agent_mailbox_posix_launcher_is_location_independent() -> None:
    launcher = (Path(__file__).resolve().parents[1] / "agent-mailbox").read_text(encoding="utf-8")
    assert launcher.startswith("#!/usr/bin/env sh\n")
    assert 'dirname -- "$0"' in launcher
    assert '.venv/bin/python' in launcher
    assert 'mailbox_channel_relay_bridging_proxy.agent_mailbox "$@"' in launcher


def test_server_launchers_are_location_independent() -> None:
    root = Path(__file__).resolve().parents[1]
    windows = (root / "mailbox-relay-server.cmd").read_text(encoding="utf-8")
    assert "%~dp0" in windows
    assert "mailbox_channel_relay_bridging_proxy.server %*" in windows
    posix = (root / "mailbox-relay-server").read_text(encoding="utf-8")
    assert posix.startswith("#!/usr/bin/env sh\n")
    assert 'mailbox_channel_relay_bridging_proxy.server "$@"' in posix
