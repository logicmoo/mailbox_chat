import json
from pathlib import Path

from mailbox_channel_relay_bridging_proxy.contact_admin import load_contacts, main
from mailbox_channel_relay_bridging_proxy.identifier_directory import IdentifierDirectory


def test_loads_csv_json_and_vcard_contacts(tmp_path: Path) -> None:
    csv_path = tmp_path / "contacts.csv"
    csv_path.write_text("name,phone\nAlice,+1 (555) 123-4567\n", encoding="utf-8")
    assert load_contacts(csv_path, system="whatsapp")[0]["identifier"] == "15551234567"
    json_path = tmp_path / "contacts.json"
    json_path.write_text(json.dumps({"contacts": [{"name": "Bob", "id": "user-2"}]}), encoding="utf-8")
    assert load_contacts(json_path, system="line")[0]["text"] == "Bob"
    vcf_path = tmp_path / "contacts.vcf"
    vcf_path.write_text("BEGIN:VCARD\nVERSION:3.0\nFN:Carol\nTEL:+15559876543\nEND:VCARD\n", encoding="utf-8")
    assert load_contacts(vcf_path, system="whatsapp")[0]["text"] == "Carol"


def test_import_command_persists_contacts(tmp_path: Path, capsys) -> None:
    source = tmp_path / "contacts.json"
    source.write_text('[{"name":"Alice","phone":"+15551234567"}]', encoding="utf-8")
    root = tmp_path / "mailbox"
    assert main(["--dir", str(root), "import", str(source), "--system", "whatsapp"]) == 0
    assert IdentifierDirectory(root).find(system="whatsapp")[0]["text"] == "Alice"
    assert '"imported": 1' in capsys.readouterr().out
