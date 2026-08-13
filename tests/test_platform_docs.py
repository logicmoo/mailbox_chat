from pathlib import Path


INSTRUCTIONS = Path(__file__).parents[1] / "docs" / "INSTRUCTIONS.md"


def test_every_platform_entry_starts_with_setup() -> None:
    text = INSTRUCTIONS.read_text(encoding="utf-8")
    platform_text = text.split("## Mattermost", 1)[1].split("## Client setup", 1)[0]
    sections = platform_text.split("\n## ")

    assert len(sections) == 12
    for section in sections:
        subheadings = [line for line in section.splitlines() if line.startswith("### ")]
        assert subheadings[0] == "### Setup"


def test_platform_instructions_match_implemented_configuration() -> None:
    text = INSTRUCTIONS.read_text(encoding="utf-8")

    assert "SLACK_APP_TOKEN" not in text
    slack = text.split("## Slack", 1)[1].split("## Matrix", 1)[0]
    assert '"presences"' not in slack
    assert '"server_id": "$DISCORD_GUILD_ID"' not in text
    assert "IRC_NICKSERV_PASSWORD" not in text
    assert "end-to-end encrypted room\ndecryption is not implemented" in text
    assert '"event_types":["message"' in text
    assert "then `npm start`" in text
    assert "WHATSAPP_VERIFY_TOKEN" in text
    assert "WHATSAPP_APP_SECRET" in text
    assert "FACEBOOK_VERIFY_TOKEN" in text
    assert "FACEBOOK_APP_SECRET" in text


def test_wsl_instructions_use_supported_root_launchers() -> None:
    text = INSTRUCTIONS.read_text(encoding="utf-8")
    wsl = text.split("When the daemon also runs inside WSL:", 1)[1]

    assert ".venv/bin/python server.py" not in wsl
    assert ".venv/bin/python agent_mailbox.py" not in wsl
    assert "./mailbox-server" in wsl
    assert "./mailbox-client status" in wsl
    assert "GET /v1/status" in wsl
    assert "does not expose start, stop, or restart endpoints" in wsl


def test_repository_docs_do_not_invoke_removed_python_entrypoints() -> None:
    root = INSTRUCTIONS.parents[1]
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    combined = instructions + readme

    assert "python server.py" not in combined
    assert "python agent_mailbox.py" not in instructions
    assert "\\agent_mailbox.py" not in instructions
    assert "mailbox_channels.token_admin" not in readme
    assert "mailbox_channels.console_client" not in instructions


def test_standalone_client_docs_avoid_cross_wsl_jsonl() -> None:
    prompt = (INSTRUCTIONS.parents[1] / "src" /
              "mailbox_channels" / "resources" /
              "AUTOMATION_PROMPT.md").read_text(encoding="utf-8")

    assert "AGENT_MAILBOX_DIR='/mnt/c" not in prompt
    assert "Use the relay's REST URL across the Windows/WSL boundary" in prompt


def test_endpoint_catalog_covers_conversation_shapes() -> None:
    text = INSTRUCTIONS.read_text(encoding="utf-8")
    catalog = text.split("The final source/destination component", 1)[1].split(
        "The CLI works through", 1,
    )[0]
    for platform in (
        "Mattermost", "Discord", "Slack", "Matrix / Element", "IRC", "Telegram",
        "WhatsApp Business", "Personal WhatsApp", "Facebook Messenger", "Viber",
        "LINE", "Discourse",
    ):
        assert f"| {platform} |" in catalog
    for heading in ("User", "Group", "Channel / room / topic", "Thread or reply", "Direct message"):
        assert heading in catalog
