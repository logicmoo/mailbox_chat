import json
from pathlib import Path

from mailbox_channel_relay_bridging_proxy.route_admin import main


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
