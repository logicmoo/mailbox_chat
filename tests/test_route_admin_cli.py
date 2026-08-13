import json
from pathlib import Path

from mailbox_channels import route_admin
from mailbox_channels.route_admin import main


def registry(path: Path) -> Path:
    target = path / "relays.json"
    target.write_text(json.dumps({"version": 1, "listeners": [
        {"id": "irc-one", "adapter": "irc", "enabled": True},
        {"id": "wa-personal", "adapter": "whatsapp_personal", "enabled": True,
         "presence_id": "personal-phone"},
    ], "routes": []}), encoding="utf-8")
    return target


def test_route_command_attaches_lists_and_detaches(tmp_path: Path, capsys) -> None:
    path = registry(tmp_path)
    prefix = ["--config-dir", str(tmp_path)]
    assert main([*prefix, "attach", "irc-one", "#agents", "wa-personal", "family@g.us",
                 "--id", "irc-family"]) == 0
    route = json.loads(path.read_text(encoding="utf-8"))["routes"][0]
    assert route["source"] == {"listener_id": "irc-one", "channel_id": "#agents"}
    assert route["destinations"][0]["adapter"] == "whatsapp_personal"
    assert main(["list", *prefix]) == 0
    assert "irc-family" in capsys.readouterr().out
    assert main([*prefix, "detach", "irc-family"]) == 0
    assert not json.loads(path.read_text(encoding="utf-8"))["routes"][0]["enabled"]


def test_route_command_star_omits_source_channel(tmp_path: Path) -> None:
    path = registry(tmp_path)
    assert main(["--config-dir", str(tmp_path), "attach", "irc-one", "*",
                 "wa-personal", "family@g.us", "--controller", "agent:router"]) == 0
    route = json.loads(path.read_text(encoding="utf-8"))["routes"][0]
    assert route["source"] == {"listener_id": "irc-one"}
    assert route["controller"] == {"type": "relay_agent", "mailbox_recipient": "router"}


def test_route_command_manages_remote_relay_with_token(monkeypatch, capsys) -> None:
    calls = []

    def request(url, method, **kwargs):
        calls.append((url, method, kwargs))
        if method == "GET":
            return {"routes": [{"id": "remote", "enabled": True, "source": {}, "destinations": []}]}
        if kwargs["payload"]["action"] == "attach":
            return {"route": {"id": "remote-new"}}
        return {"detached": True}

    monkeypatch.setattr(route_admin, "_request", request)
    assert main(["list", "--url", "http://relay:46667", "--token", "secret"]) == 0
    assert main(["attach", "irc", "*", "wa", "group@g.us",
                 "--url", "http://relay:46667", "--token", "secret"]) == 0
    assert main(["detach", "remote-new", "--url", "http://relay:46667", "--token", "secret"]) == 0
    assert all(call[2]["token"] == "secret" for call in calls)
    assert calls[1][2]["payload"]["source_channel"] == "*"
    assert "remote" in capsys.readouterr().out
