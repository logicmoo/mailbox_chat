from mailbox_channel_relay_bridging_proxy.agent_mailbox import build_parser as agent_parser
from mailbox_channel_relay_bridging_proxy.console_client import parser as speaker_parser
from mailbox_channel_relay_bridging_proxy.server import build_parser as server_parser
from mailbox_channel_relay_bridging_proxy.token_admin import parser as token_parser


def test_server_help_lists_every_public_configuration_option() -> None:
    help_text = server_parser().format_help()
    assert help_text.startswith("usage: mailbox-relay-server")
    for option in (
        "--host", "--port", "--mailbox-dir", "--config-dir", "--public-address",
        "--public-url", "--token", "MAILBOX_RELAY_HOST", "MAILBOX_RELAY_PORT",
        "MAILBOX_RELAY_PUBLIC_URL", "MAILBOX_RELAY_TOKEN",
    ):
        assert option in help_text


def test_agent_help_lists_every_global_option() -> None:
    help_text = agent_parser().format_help()
    for option in (
        "--run", "--dir", "--url", "--mailbox", "--config", "--from", "--to", "--format",
        "--output", "--timeout", "--token", "--curl", "--input", "--retry",
        "--retry-delay", "--quiet", "--verbose", "--nobuffer", "--version",
    ):
        assert option in help_text
    assert "COMMAND --help" in help_text


def test_trusted_speaker_and_token_help_are_complete() -> None:
    speaker_help = speaker_parser().format_help()
    assert "identity" in speaker_help and "--to" in speaker_help and "--url" in speaker_help
    token_help = token_parser().format_help()
    assert "{register,status}" in token_help and "--config-dir" in token_help and "--token" in token_help
