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
