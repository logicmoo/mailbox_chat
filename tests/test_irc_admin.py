from mailbox_channels.admin_io import normalize_options
from mailbox_channels.irc_admin import parser, protocol_line


def test_irc_help_lists_normal_commands() -> None:
    help_text = parser().format_help()
    for command in (
        "ping", "list", "names", "join", "part", "topic", "nick", "whois", "mode",
        "invite", "kick", "message", "notice", "raw",
    ):
        assert command in help_text


def test_irc_protocol_lines_are_constructed_safely() -> None:
    parse = parser().parse_args
    assert protocol_line(parse(["ping", "relay.example"])) == "PING :relay.example"
    assert protocol_line(parse(["join", "#testing"])) == "JOIN #testing"
    assert protocol_line(parse(["part", "#testing", "done"])) == "PART #testing :done"
    assert protocol_line(parse(["topic", "#testing", "New topic"])) == "TOPIC #testing :New topic"
    assert protocol_line(parse(["mode", "#testing", "+o", "alice"])) == "MODE #testing +o alice"
    assert protocol_line(parse(["kick", "#testing", "spammer", "bye"])) == (
        "KICK #testing spammer :bye"
    )


def test_platform_options_can_follow_command() -> None:
    args = parser().parse_args(normalize_options([
        "message", "#testing", "--input", "message.txt", "--input-format", "text",
        "--format", "text",
    ]))
    assert args.input == "message.txt"
    assert args.format == "text"
