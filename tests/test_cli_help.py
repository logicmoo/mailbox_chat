import argparse

from mailbox_channel_relay_bridging_proxy.agent_mailbox import build_parser as agent_parser
from mailbox_channel_relay_bridging_proxy.console_client import parser as speaker_parser
from mailbox_channel_relay_bridging_proxy.server import build_parser as server_parser
from mailbox_channel_relay_bridging_proxy.token_admin import parser as token_parser


def _agent_commands():
    parser = agent_parser()
    subparsers = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    return subparsers.choices


def _assert_actions_documented(parser: argparse.ArgumentParser) -> None:
    for action in parser._actions:
        if action.dest == "help" or isinstance(action, argparse._SubParsersAction):
            continue
        assert action.help, f"{parser.prog}:{action.dest} is missing help text"


def test_server_help_lists_every_public_configuration_option() -> None:
    parser = server_parser()
    help_text = parser.format_help()
    assert help_text.startswith("usage: mailbox-relay-server")
    for option in (
        "--host", "--port", "--mailbox-dir", "--config-dir", "--public-address",
        "--public-url", "--token", "MAILBOX_RELAY_HOST", "MAILBOX_RELAY_PORT",
        "MAILBOX_RELAY_PUBLIC_URL", "MAILBOX_RELAY_TOKEN",
    ):
        assert option in help_text
    _assert_actions_documented(parser)


def test_agent_help_lists_every_global_option() -> None:
    parser = agent_parser()
    help_text = parser.format_help()
    for option in (
        "--run", "--dir", "--url", "--mailbox", "--config", "--from", "--to", "--format",
        "--output", "--timeout", "--token", "--curl", "--input", "--retry",
        "--retry-delay", "--quiet", "--verbose", "--nobuffer", "--version",
    ):
        assert option in help_text
    assert "COMMAND --help" in help_text
    _assert_actions_documented(parser)


def test_every_agent_command_has_comprehensive_help() -> None:
    expected = {"send", "receive", "peek", "poll", "follow", "unread-count", "ack", "status", "check"}
    commands = _agent_commands()
    assert set(commands) == expected
    for name, command_parser in commands.items():
        help_text = command_parser.format_help()
        assert command_parser.description
        assert command_parser.epilog and "Example:" in command_parser.epilog
        assert f"agent-mailbox {name}" in help_text or name in {"status", "check"}
        for action in command_parser._actions:
            if action.dest == "help":
                continue
            assert action.help, f"{name}:{action.dest} is missing help text"


def test_trusted_speaker_and_token_help_are_complete() -> None:
    speaker = speaker_parser()
    speaker_help = speaker.format_help()
    assert "identity" in speaker_help and "--to" in speaker_help and "--url" in speaker_help
    _assert_actions_documented(speaker)
    token = token_parser()
    token_help = token.format_help()
    assert "{register,status}" in token_help and "--config-dir" in token_help and "--token" in token_help
    _assert_actions_documented(token)
