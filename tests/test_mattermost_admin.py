import argparse

from mailbox_channels.identifier_directory import IdentifierDirectory
from mailbox_channels.adapters.mattermost_adapter import (
    COMMANDS, _payload, execute, parser, remember_named_ids, resolve_address,
)


class Response:
    def __init__(self, payload=None):
        self.payload = payload
        self.content = b"" if payload is None else b"json"

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Session:
    def __init__(self):
        self.calls = []

    def request(self, method, url, json=None, timeout=0):
        self.calls.append((method, url, json, timeout))
        if url.endswith("/api/v4/users/me"):
            return Response({"id": "a" * 26})
        return Response({"ok": True})


class DiscoverySession(Session):
    def request(self, method, url, json=None, timeout=0):
        self.calls.append((method, url, json, timeout))
        if url.endswith("/api/v4/users/me"):
            return Response({"id": "a" * 26, "username": "relay-bot"})
        if url.endswith(f"/api/v4/users/{'a' * 26}/teams"):
            return Response([{"id": "t" * 26, "name": "engineering", "display_name": "Engineering"}])
        if url.endswith(f"/api/v4/users/{'a' * 26}/channels"):
            return Response([{"id": "c" * 26, "name": "town-square",
                              "display_name": "Town Square", "type": "O"}])
        return Response({"ok": True})


def test_mm_help_lists_familiar_commands() -> None:
    choices = next(action for action in parser()._actions
                   if isinstance(action, argparse._SubParsersAction)).choices
    assert set(choices) == set(COMMANDS)


def test_mm_message_accepts_qualified_address() -> None:
    session = Session()
    execute("message", {"target": "mm/0/channel-id", "text": "hello"},
            session=session, base_url="https://chat.example")
    assert session.calls[-1][:3] == (
        "POST", "https://chat.example/api/v4/posts",
        {"channel_id": "channel-id", "message": "hello"},
    )


def test_mm_join_adds_authenticated_bot() -> None:
    session = Session()
    execute("join", {"channel": "mm/0/channel-id"}, session=session,
            base_url="https://chat.example")
    assert session.calls[-1][0:3] == (
        "POST", "https://chat.example/api/v4/channels/channel-id/members",
        {"user_id": "a" * 26},
    )


def test_mm_mode_maps_operator_to_channel_admin_role() -> None:
    session = Session()
    execute("mode", {"channel": "channel-id", "setting": "+o", "user": "b" * 26},
            session=session, base_url="https://chat.example")
    assert session.calls[-1][:3] == (
        "PUT", f"https://chat.example/api/v4/channels/channel-id/members/{'b' * 26}/roles",
        {"roles": "channel_user channel_admin"},
    )


def test_mm_notice_can_be_ephemeral_to_one_user() -> None:
    session = Session()
    execute("notice", {"target": "channel-id", "text": "maintenance",
                       "user": "b" * 26}, session=session, base_url="https://chat.example")
    assert session.calls[-1][:3] == (
        "POST", "https://chat.example/api/v4/posts/ephemeral",
        {"user_id": "b" * 26, "post": {"channel_id": "channel-id",
                                         "message": "maintenance",
                                         "props": {"mailbox_notice": True}}},
    )


def test_mm_ping_checks_authenticated_user() -> None:
    session = Session()
    result = execute("ping", {}, session=session, base_url="https://chat.example")
    assert result["ok"] is True
    assert session.calls == [("GET", "https://chat.example/api/v4/users/me", None, 30)]


def test_mm_raw_rejects_external_url() -> None:
    session = Session()
    try:
        execute("raw", {"method": "GET", "path": "https://evil.example/"},
                session=session, base_url="https://chat.example")
    except ValueError as error:
        assert "/api/v4/" in str(error)
    else:
        raise AssertionError("external raw URL was accepted")


def test_mm_raw_accepts_json_file_input(tmp_path) -> None:
    source = tmp_path / "post.json"
    source.write_text('{"message":"hello"}', encoding="utf-8")
    args = parser().parse_args([
        "--input", str(source), "--input-format", "json",
        "raw", "POST", "/api/v4/posts",
    ])
    assert _payload(args)["arguments"]["body"] == {"message": "hello"}


def test_mm_discovery_persists_teams_channels_and_readable_addresses(tmp_path) -> None:
    session = DiscoverySession()
    directory = IdentifierDirectory(tmp_path)
    teams = execute("teams", {}, session=session, base_url="https://chat.example",
                    directory=directory)
    channels = execute("list", {}, session=session, base_url="https://chat.example",
                       directory=directory)

    assert teams[0]["address"] == f"mm/chat.example/{'t' * 26}"
    assert channels[0]["address"] == f"mm/chat.example/{'c' * 26}"
    assert resolve_address("mm/0/Town Square", directory,
                           base_url="https://chat.example") == f"mm/0/{'c' * 26}"
    assert resolve_address("mm/0/town-square", directory,
                           base_url="https://chat.example") == f"mm/0/{'c' * 26}"
    assert directory.find(system="mm/chat.example", text="Engineering", kind="team")
    assert directory.find(system="mm/0") == []


def test_mattermost_channel_web_url_resolves_through_registry(tmp_path) -> None:
    directory = IdentifierDirectory(tmp_path)
    directory.remember(
        "c" * 26, "image-perception-to-recognizable-memory-and-arc3",
        system="mm/chat.singularitynet.io", kind="channel",
    )
    from mailbox_channels.adapters.mattermost_adapter import _id

    assert _id(
        "https://chat.singularitynet.io/chat/channels/"
        "image-perception-to-recognizable-memory-and-arc3",
        directory=directory, base_url="https://chat.singularitynet.io", kind="channel",
    ) == "c" * 26


def test_mm_thread_discovery_bounds_long_readable_preview(tmp_path) -> None:
    record = {"post": {"id": "p" * 26, "message": "x" * 700}}
    directory = IdentifierDirectory(tmp_path)
    from mailbox_channels.adapters.mattermost_adapter import _remember

    saved = _remember(directory, "https://chat.example", record, kind="thread")
    assert saved["address"] == f"mm/chat.example/{'p' * 26}"
    assert len(directory.find(system="mm/chat.example", kind="thread")[0]["text"]) == 512


def test_every_nested_mattermost_id_and_name_pair_is_persisted(tmp_path) -> None:
    directory = IdentifierDirectory(tmp_path)
    payload = {
        "channels": [{"id": "channel-1", "name": "town-square",
                      "display_name": "Town Square", "team_id": "team-1"}],
        "owner": {"id": "user-1", "username": "alice"},
        "deeply": {"nested": [{"channel_id": "channel-2",
                                 "channel_name": "developers"},
                                {"user_id": "user-2", "username": "bob"}]},
    }

    assert remember_named_ids(directory, "https://chat.singularitynet.io", payload) is payload
    assert {item["text"] for item in directory.find(
        system="mm/chat.singularitynet.io", identifier="channel-1",
    )} == {"town-square", "Town Square"}
    system = "mm/chat.singularitynet.io"
    assert directory.find(system=system, text="alice", kind="user")[0]["identifier"] == "user-1"
    assert directory.find(system=system, text="developers", kind="channel")[0]["identifier"] == "channel-2"
    assert directory.find(system=system, text="bob", kind="user")[0]["identifier"] == "user-2"
    assert directory.find(system="mm/0") == []


def test_registry_walker_ignores_unlabelled_ids_but_visits_all_objects(tmp_path) -> None:
    directory = IdentifierDirectory(tmp_path)
    payload = [{"event_id": "opaque-only", "child": {"id": "named", "nickname": "helper"}}]
    remember_named_ids(directory, "https://chat.example", payload)
    assert directory.find(system="mm/chat.example", identifier="opaque-only") == []
    assert directory.find(system="mm/chat.example", identifier="named")[0]["text"] == "helper"


def test_registry_walker_does_not_give_creator_the_channel_name(tmp_path) -> None:
    directory = IdentifierDirectory(tmp_path)
    payload = {
        "id": "channel-id", "display_name": "Channel title", "name": "channel-title",
        "creator_id": "creator-id", "team_id": "team-id",
    }
    remember_named_ids(directory, "https://chat.example", payload)

    assert directory.find(system="mm/chat.example", identifier="creator-id") == []
    assert {entry["text"] for entry in directory.find(
        system="mm/chat.example", identifier="channel-id",
    )} == {"Channel title", "channel-title"}


def test_mattermost_user_resolves_by_username_email_nickname_and_full_name(tmp_path) -> None:
    directory = IdentifierDirectory(tmp_path)
    user = {
        "id": "jo99f78563nqtcob4ac6zjddha", "username": "zarathustra",
        "email": "zarathustra@singularitynet.io", "nickname": "Zara",
        "first_name": "Zarathustra", "last_name": "Goertzel",
    }
    remember_named_ids(directory, "https://chat.singularitynet.io", user)

    for alias in (
        "zarathustra", "zarathustra@singularitynet.io", "Zara", "Zarathustra Goertzel",
    ):
        assert directory.find(
            system="mm/chat.singularitynet.io", text=alias, kind="user",
        )[0]["identifier"] == user["id"]
