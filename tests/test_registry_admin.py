from mailbox_channels.identifier_directory import IdentifierDirectory
from mailbox_channels.registry_admin import main
import pytest


def test_registry_command_remembers_finds_and_deduplicates_requests(tmp_path, capsys) -> None:
    prefix = ["--dir", str(tmp_path)]
    identifier = "{12345678-1234-5678-9ABC-1234567890AB}"
    assert main([*prefix, "remember", "discord", identifier, "Operations", "--kind", "channel"]) == 0
    assert main([*prefix, "find", "--system", "discord", "--identifier", identifier]) == 0
    assert "Operations" in capsys.readouterr().out
    assert main([*prefix, "request", "discord", identifier, "--resolver", "get-channel"]) == 0
    assert main([*prefix, "request", "discord", identifier, "--resolver", "get-channel"]) == 0
    requests = IdentifierDirectory(tmp_path).resolution_requests(system="discord")
    assert len(requests) == 1
    assert requests[0]["request_count"] == 1


def test_registry_alias_command_is_collision_safe(tmp_path, capsys) -> None:
    prefix = ["--dir", str(tmp_path)]
    assert main([*prefix, "alias", "mm/chat.example", "user-1",
                 "patrick.hammer", "--kind", "user"]) == 0
    capsys.readouterr()
    with pytest.raises(ValueError, match="already names another identifier"):
        main([*prefix, "alias", "mm/chat.example", "user-2",
              "patrick.hammer", "--kind", "user"])
