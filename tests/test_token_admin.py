from pathlib import Path

import pytest

from mailbox_channel_relay_bridging_proxy.token_admin import register_token, token_registered


def test_register_token_preserves_env_and_replaces_existing_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=value\nMAILBOX_RELAY_TOKEN=old-value-that-is-long-enough-000\n", encoding="utf-8")

    registered = register_token(tmp_path, "new-value-that-is-at-least-32-characters")

    assert registered == "new-value-that-is-at-least-32-characters"
    assert env_file.read_text(encoding="utf-8") == (
        "OTHER=value\nMAILBOX_RELAY_TOKEN=new-value-that-is-at-least-32-characters\n"
    )
    assert token_registered(tmp_path)


def test_register_token_generates_strong_value_and_rejects_short_one(tmp_path: Path) -> None:
    assert len(register_token(tmp_path)) >= 32
    with pytest.raises(ValueError, match="at least 32"):
        register_token(tmp_path, "short")
