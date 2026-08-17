from mailbox_channels.admin_io import normalize_options
from mailbox_channels.agent_mailbox import CHAT_COMMANDS
from mailbox_channels.chat_admin import _platform, main, parser
from mailbox_channels.adapters.irc_adapter import protocol_line
from mailbox_channels.adapters.mattermost_adapter import arguments_from_namespace
from mailbox_channels.identifier_directory import IdentifierDirectory


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
    assert arguments_from_namespace(args, None) == {
        "channel": "mm/0/town-square", "setting": "+o", "user": "alice",
    }


def test_on_selects_mattermost_for_addressless_commands() -> None:
    args = parser().parse_args(normalize_options(["list", "--on", "mm/0"]))
    assert _platform(args) == "mm"


def test_downloaded_bare_id_selects_its_registered_platform(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MAILBOX_DIR", str(tmp_path))
    IdentifierDirectory(tmp_path).remember(
        "j4pok4rbqtfytcrcn8d3nhgkto", "some-user",
        system="mm/chat.singularitynet.io", kind="user",
    )
    args = parser().parse_args(["whois", "j4pok4rbqtfytcrcn8d3nhgkto"])
    assert _platform(args) == "mm"


def test_downloaded_channel_name_selects_its_registered_platform(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MAILBOX_DIR", str(tmp_path))
    IdentifierDirectory(tmp_path).remember(
        "channel-id", "test", system="mm/chat.singularitynet.io", kind="channel",
    )
    args = parser().parse_args(["names", "test"])
    assert _platform(args) == "mm"


def test_command_help_explains_targets_platform_and_mattermost_modes() -> None:
    choices = next(action for action in parser()._actions
                   if isinstance(action, __import__("argparse")._SubParsersAction)).choices
    mode_help = choices["mode"].format_help()
    assert "qualified channel/user address" in mode_help
    assert "Mattermost public/private/+o USER/-o USER" in mode_help
    assert "--on TYPE/INSTANCE" in mode_help


def test_mattermost_command_reports_new_registry_entries(monkeypatch, capsys) -> None:
    monkeypatch.setattr("mailbox_channels.chat_admin.post_mattermost_command", lambda *_args, **_kwargs: {
        "result": [], "registry": {"new_entries": 3, "total_entries": 20},
    })
    assert main(["list", "--on", "mm/0"]) == 0
    captured = capsys.readouterr()
    assert "[registry] 3 new entries found" in captured.err


def test_join_subscribe_all_is_sent_to_mattermost_server(monkeypatch, capsys) -> None:
    captured = {}

    def post(_url, _token, command, arguments):
        captured.update({"command": command, "arguments": arguments})
        return {
            "result": {"ok": True},
            "subscriptions": {
                "channel": "mm-chat-example-team-channel-id",
                "subscribed_agents": ["one", "two"],
                "count": 2,
            },
        }

    monkeypatch.setattr("mailbox_channels.chat_admin.post_mattermost_command", post)

    assert main(["join", "mm/0/channel-id", "--subscribe-all"]) == 0
    assert captured == {
        "command": "join",
        "arguments": {"channel": "mm/0/channel-id", "subscribe_all": True},
    }
    assert '"count": 2' in capsys.readouterr().out


def test_bare_list_loops_through_configured_providers(monkeypatch, capsys) -> None:
    monkeypatch.setattr("mailbox_channels.chat_admin.load_connectors", lambda: [
        {"enabled": True, "adapter": "mattermost"},
        {"enabled": True, "adapter": "irc"},
        {"enabled": True, "adapter": "mattermost"},
    ])
    monkeypatch.setattr("mailbox_channels.chat_admin.post_mattermost_command",
                        lambda *_args, **_kwargs: {
                            "result": [{"address": "mm/chat.example/general"}],
                            "registry": {"new_entries": 2},
                        })
    monkeypatch.setattr("mailbox_channels.chat_admin._remote_irc_list",
                        lambda *_args: {"channels": [{"address": "irc/example/#general"}]})

    assert main(["list"]) == 0
    captured = capsys.readouterr()
    assert '"provider": "mm"' in captured.out
    assert '"provider": "irc"' in captured.out
    assert captured.out.count('"provider": "mm"') == 1
    assert "[registry] 2 new entries found" in captured.err
