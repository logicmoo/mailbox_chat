from pathlib import Path

import pytest

from mailbox_channels.identifier_directory import IdentifierDirectory


def test_directory_resolves_uuid_to_text_and_back(tmp_path: Path) -> None:
    directory = IdentifierDirectory(tmp_path)
    identifier = "{12345678-1234-5678-9ABC-1234567890AB}"

    saved = directory.remember(identifier, "Operations room", system="discord", kind="channel")

    assert saved["identifier"] == "12345678-1234-5678-9abc-1234567890ab"
    assert directory.find(system="discord", identifier=identifier)[0]["text"] == "Operations room"
    assert directory.find(system="discord", text="operations ROOM", kind="channel")[0]["identifier"] == saved["identifier"]


def test_directory_keeps_multiple_readable_aliases(tmp_path: Path) -> None:
    directory = IdentifierDirectory(tmp_path)
    identifier = "12345678-1234-5678-9abc-1234567890ab"
    directory.remember(identifier, "ops", system="discord", kind="channel")
    directory.remember(identifier, "Operations room", system="discord", kind="channel")

    assert {entry["text"] for entry in directory.find(system="discord", identifier=identifier)} == {
        "ops", "Operations room",
    }


def test_directory_enriches_known_ids_without_replacing_them(tmp_path: Path) -> None:
    directory = IdentifierDirectory(tmp_path)
    identifier = "12345678-1234-5678-9abc-1234567890ab"
    directory.remember(identifier, "Alice", system="discord", kind="user")

    enriched = directory.enrich({"source_id": identifier, "text": "hello"}, system="discord")

    assert enriched["source_id"] == identifier
    assert enriched["source_id_text"] == "Alice"


def test_directory_keeps_platform_identifiers_and_rejects_empty_values(tmp_path: Path) -> None:
    directory = IdentifierDirectory(tmp_path)
    directory.remember("-100123", "Telegram operations", system="telegram", kind="chat")
    assert directory.find(system="telegram", identifier="-100123")[0]["text"] == "Telegram operations"
    with pytest.raises(ValueError, match="identifier is required"):
        directory.remember("", "Name", system="telegram")


def test_directory_tracks_resolution_requests_by_system(tmp_path: Path) -> None:
    directory = IdentifierDirectory(tmp_path)
    first = directory.request_resolution("telegram", "-100123", resolver="getChat")
    duplicate = directory.request_resolution("telegram", "-100123", resolver="getChat")
    other_system = directory.request_resolution("slack", "-100123", resolver="conversations.info")

    assert first["should_request"] is True
    assert duplicate["should_request"] is False
    assert other_system["should_request"] is True
    directory.finish_resolution(
        "telegram", "-100123", resolver="getChat", text="Operations", kind="chat",
    )
    assert directory.resolution_requests(system="telegram")[0]["status"] == "resolved"
