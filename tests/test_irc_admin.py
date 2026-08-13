from mailbox_channels.admin_io import normalize_options
from mailbox_channels.agent_mailbox import CHAT_COMMANDS
from mailbox_channels.irc_admin import _mattermost_arguments, _platform, parser, protocol_line


def test_irc_help_lists_normal_commands() -> None:
    help_text = parser().format_help()
    for command in CHAT_COMMANDS:
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


def test_qualified_mattermost_address_selects_platform_and_maps_mode() -> None:
    args = parser().parse_args(["mode", "mm/0/town-square", "+o", "alice"])
    assert _platform(args) == "mm"
    assert _mattermost_arguments(args, None) == {
        "channel": "mm/0/town-square", "setting": "+o", "user": "alice",
    }


def test_on_selects_mattermost_for_addressless_commands() -> None:
    args = parser().parse_args(normalize_options(["list", "--on", "mm/0"]))
    assert _platform(args) == "mm"


def test_command_help_explains_targets_platform_and_mattermost_modes() -> None:
    choices = next(action for action in parser()._actions
                   if isinstance(action, __import__("argparse")._SubParsersAction)).choices
    mode_help = choices["mode"].format_help()
    assert "qualified channel/user address" in mode_help
    assert "Mattermost public/private/+o USER/-o USER" in mode_help
    assert "--on TYPE/INSTANCE" in mode_help
