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


def test_every_published_command_has_windows_and_posix_launchers() -> None:
    root = Path(__file__).resolve().parents[1]
    commands = {
        "agent-mailbox": "agent_mailbox",
        "mailbox-relay-server": "server",
        "trusted-speaker": "console_client",
        "mailbox-relay-token": "token_admin",
        "mailbox-relay-contacts": "contact_admin",
    }
    for command, module in commands.items():
        windows = (root / f"{command}.cmd").read_text(encoding="utf-8")
        posix_path = root / command
        posix = posix_path.read_text(encoding="utf-8")
        assert f"mailbox_channel_relay_bridging_proxy.{module}" in windows
        assert f"mailbox_channel_relay_bridging_proxy.{module}" in posix
        assert posix.startswith("#!/usr/bin/env sh\n")
    assert "trusted-speaker.cmd" in (root / "mailbox-chat.cmd").read_text(encoding="utf-8")
    assert "trusted-speaker" in (root / "mailbox-chat").read_text(encoding="utf-8")


def test_whatsapp_personal_companion_has_root_launchers() -> None:
    root = Path(__file__).resolve().parents[1]
    assert "companions/whatsapp-personal" in (root / "whatsapp-personal-relay").read_text(encoding="utf-8")
    assert "companions\\whatsapp-personal" in (root / "whatsapp-personal-relay.cmd").read_text(encoding="utf-8")
