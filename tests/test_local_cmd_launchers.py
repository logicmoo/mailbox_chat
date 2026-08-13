from pathlib import Path


def test_agent_mailbox_cmd_is_location_independent() -> None:
    launcher = (Path(__file__).resolve().parents[1] / "mailbox-client.cmd").read_text(encoding="utf-8")
    assert "%~dp0" in launcher
    assert "src;%PYTHONPATH%" in launcher
    assert "mailbox_channels.agent_mailbox %*" in launcher
    assert "exit /b %ERRORLEVEL%" in launcher


def test_agent_mailbox_posix_launcher_is_location_independent() -> None:
    launcher = (Path(__file__).resolve().parents[1] / "mailbox-client").read_text(encoding="utf-8")
    assert launcher.startswith("#!/usr/bin/env sh\n")
    assert 'dirname -- "$0"' in launcher
    assert '.venv/bin/python' in launcher
    assert 'mailbox_channels.agent_mailbox "$@"' in launcher


def test_canonical_mailbox_client_launchers_are_location_independent() -> None:
    root = Path(__file__).resolve().parents[1]
    windows = (root / "mailbox-client.cmd").read_text(encoding="utf-8")
    posix = (root / "mailbox-client").read_text(encoding="utf-8")
    assert "%~dp0" in windows
    assert "mailbox_channels.agent_mailbox %*" in windows
    assert posix.startswith("#!/usr/bin/env sh\n")
    assert 'mailbox_channels.agent_mailbox "$@"' in posix


def test_server_launchers_are_location_independent() -> None:
    root = Path(__file__).resolve().parents[1]
    windows = (root / "mailbox-server.cmd").read_text(encoding="utf-8")
    assert "%~dp0" in windows
    assert "mailbox_channels.server %*" in windows
    posix = (root / "mailbox-server").read_text(encoding="utf-8")
    assert posix.startswith("#!/usr/bin/env sh\n")
    assert 'mailbox_channels.server "$@"' in posix


def test_every_published_command_has_windows_and_posix_launchers() -> None:
    root = Path(__file__).resolve().parents[1]
    commands = {
        "mailbox-client": "agent_mailbox",
        "mailbox-server": "server",
        "mailbox-console": "console_client",
    }
    for command, module in commands.items():
        windows = (root / f"{command}.cmd").read_text(encoding="utf-8")
        posix_path = root / command
        posix = posix_path.read_text(encoding="utf-8")
        assert f"mailbox_channels.{module}" in windows
        assert f"mailbox_channels.{module}" in posix
        assert posix.startswith("#!/usr/bin/env sh\n")


def test_retired_top_level_commands_are_absent() -> None:
    root = Path(__file__).resolve().parents[1]
    for command in (
        "agent-mailbox", "mailbox-chat", "trusted-speaker", "mailbox-relay-server",
        "mailbox-relay-token", "mailbox-relay-contacts", "mailbox-relay-route",
        "mailbox-token", "mailbox-contacts", "mailbox-route",
    ):
        assert not (root / command).exists()
        assert not (root / f"{command}.cmd").exists()


def test_whatsapp_personal_companion_has_root_launchers() -> None:
    root = Path(__file__).resolve().parents[1]
    assert "companions/whatsapp-personal" in (root / "whatsapp-personal-relay").read_text(encoding="utf-8")
    assert "companions\\whatsapp-personal" in (root / "whatsapp-personal-relay.cmd").read_text(encoding="utf-8")
