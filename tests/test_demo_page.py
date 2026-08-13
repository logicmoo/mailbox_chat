from pathlib import Path

from mailbox_channels import server


def test_demo_page_links_to_live_relay_resources() -> None:
    page = (Path(server.RESOURCE_ROOT) / "special_websocket_client.html").read_text(encoding="utf-8")
    for path in (
        "/v1/status",
        "/v1/adapters",
        "/v1/listeners",
        "/v1/routes",
        "/v1/identifiers",
        "/v1/identifier-resolution-requests",
        "/agent_mailbox.py",
        "/INSTALL_WITH_CODEX.md",
        "/AUTOMATION_PROMPT.md",
    ):
        assert f'href="{path}"' in page
