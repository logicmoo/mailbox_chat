import argparse

from mailbox_channels.mattermost_admin import COMMANDS, _payload, execute, parser


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
