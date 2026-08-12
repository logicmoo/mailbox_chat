from pathlib import Path


def test_agent_mailbox_cmd_is_location_independent() -> None:
    launcher = (Path(__file__).resolve().parents[1] / "agent-mailbox.cmd").read_text(encoding="utf-8")
    assert "%~dp0" in launcher
    assert "src;%PYTHONPATH%" in launcher
    assert "mailbox_channel_relay_bridging_proxy.agent_mailbox %*" in launcher
    assert "exit /b %ERRORLEVEL%" in launcher
