import json
from pathlib import Path

from mailbox_chat import mailbox_store
from mailbox_chat.bootstrap_config import bootstrap_mailbox_state


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_bootstrap_creates_registries_and_runtime_artifacts(tmp_path: Path) -> None:
    mailbox_root = tmp_path / "mailbox"
    config_root = mailbox_root / "config"

    _write_json(
        config_root / "server_builtin_agents.json",
        {
            "system_agents": [
                {
                    "id": "system-lifecycle-agent",
                    "payload": {
                        "kind": "agent",
                        "agent_kind": "role_agent",
                        "agent_roles": ["lifecycle_emitter"],
                        "writes_to_mailboxes": ["server_events_log"],
                    },
                }
            ],
            "relay_agents": [
                {"id": "relay-one", "file": "relay-agent-one.json"},
            ],
        },
    )
    _write_json(
        config_root / "relay-agent-one.json",
        {
            "id": "relay-one",
            "payload": {
                "kind": "agent",
                "agent_kind": "relay_agent",
                "connector_id": "mm-main",
                "agent_roles": ["bridge_agent"],
            },
            "connector": {
                "id": "mm-main",
                "adapter": "mattermost",
                "direction": "bidirectional",
                "base_url": "https://chat.example.org",
                "mailbox_ids": ["room-one"],
            },
        },
    )
    _write_json(
        config_root / "server_config_connectors.json",
        {"entries": {}},
    )
    _write_json(
        config_root / "server_config_mailboxes.json",
        {
            "mailboxes": [
                {
                    "id": "server_events_log",
                    "mailbox_type": "system_builtin_mailboxes",
                    "payload": {"kind": "mailbox", "subscribers": []},
                },
                {
                    "id": "room-one",
                    "mailbox_type": "system_mailboxes",
                    "payload": {"kind": "mailbox", "subscribers": []},
                },
            ]
        },
    )

    report = bootstrap_mailbox_state(mailbox_root)

    assert report["agents_upserted"] == 2
    assert report["connectors_upserted"] == 1
    assert report["mailboxes_upserted"] == 2
    assert report["missing_files"] == []

    assert mailbox_store.get_entry(
        mailbox_root, mailbox_store.AGENTS_REGISTRY, "system-lifecycle-agent",
    ) is not None
    assert mailbox_store.get_entry(
        mailbox_root, mailbox_store.AGENTS_REGISTRY, "relay-one",
    ) is not None
    assert mailbox_store.get_entry(
        mailbox_root, mailbox_store.CONNECTORS_REGISTRY, "mm-main",
    ) is not None
    assert mailbox_store.get_entry(
        mailbox_root, mailbox_store.MAILBOXES_REGISTRY, "server_events_log",
    ) is not None

    assert (mailbox_root / "messages").is_dir()
    assert (mailbox_root / "resolver").is_dir()
    assert (mailbox_root / "runtime").is_dir()
    assert (mailbox_root / "messages" / "server_events_log.json").exists()
    assert (mailbox_root / "resolver" / "resolver_mm-main.json").exists()
    assert mailbox_store.mailbox_path(mailbox_root, "server_events_log").exists()
    assert mailbox_store.mailbox_path(mailbox_root, "room-one").exists()
    connector_payload = mailbox_store.get_entry(
        mailbox_root, mailbox_store.CONNECTORS_REGISTRY, "mm-main",
    )["payload"]
    assert connector_payload["bridge_agent"] == "relay-one"


def test_bootstrap_uses_explicit_config_dir(tmp_path: Path) -> None:
    mailbox_root = tmp_path / "mailbox"
    config_root = tmp_path / "custom-config"

    _write_json(config_root / "server_builtin_agents.json", {"system_agents": [], "relay_agents": []})
    _write_json(config_root / "server_config_connectors.json", {"entries": {}})
    _write_json(
        config_root / "server_config_mailboxes.json",
        {"mailboxes": [{"id": "agent_to_agent", "payload": {"kind": "mailbox", "subscribers": []}}]},
    )

    report = bootstrap_mailbox_state(mailbox_root, config_dir=config_root)

    assert report["config_root"] == str(config_root.resolve())
    assert report["missing_files"] == []
    assert report["mailboxes_upserted"] == 1
    assert mailbox_store.get_entry(
        mailbox_root, mailbox_store.MAILBOXES_REGISTRY, "agent_to_agent",
    ) is not None


def test_builtin_agents_can_declare_required_mailboxes_without_mailbox_file(tmp_path: Path) -> None:
    mailbox_root = tmp_path / "mailbox"
    config_root = mailbox_root / "config"

    _write_json(
        config_root / "server_builtin_agents.json",
        {
            "system_agents": [
                {
                    "id": "system-lifecycle-agent",
                    "payload": {
                        "kind": "agent",
                        "agent_kind": "role_agent",
                        "required_mailboxes": ["server_events_log", "agent_to_agent"],
                        "writes_to_mailboxes": ["server_events_log"],
                    },
                }
            ],
            "relay_agents": [],
        },
    )
    _write_json(config_root / "server_config_connectors.json", {"entries": {}})

    report = bootstrap_mailbox_state(mailbox_root)

    assert report["mailboxes_upserted"] >= 2
    assert mailbox_store.get_entry(
        mailbox_root, mailbox_store.MAILBOXES_REGISTRY, "server_events_log",
    ) is not None
    assert mailbox_store.get_entry(
        mailbox_root, mailbox_store.MAILBOXES_REGISTRY, "agent_to_agent",
    ) is not None


def test_presence_files_merge_connector_instances_with_inheritance(tmp_path: Path) -> None:
    mailbox_root = tmp_path / "mailbox"
    config_root = mailbox_root / "config"

    _write_json(
        config_root / "relay-agent-base.json",
        {
            "id": "relay-base",
            "payload": {
                "kind": "agent",
                "agent_kind": "relay_agent",
                "adapter": "mattermost",
                "endpoint": "https://chat.example.org",
                "agent_roles": ["bridge_agent"],
            },
            "connector": {
                "adapter": "mattermost",
                "direction": "bidirectional",
                "base_url": "https://chat.example.org",
            },
        },
    )
    _write_json(
        config_root / "presence-1-min.json",
        {
            "id": "presence-mm-min",
            "payload": {
                "kind": "agent",
                "agent_kind": "relay_presence_agent",
                "adapter": "mattermost",
                "endpoint": "https://chat.example.org",
                "agent_roles": ["live_presence"],
            },
            "connector": {
                "base_url": "https://chat.example.org",
                "instances": [
                    {
                        "name": "@min.botnick",
                        "relay-type": "relay_via_bot",
                        "mailbox_types": ["room-one"],
                        "reply-in-threads": True,
                    }
                ],
            },
        },
    )
    _write_json(
        config_root / "presence-2-atom.json",
        {
            "id": "presence-mm-atom",
            "payload": {
                "kind": "agent",
                "agent_kind": "relay_presence_agent",
                "adapter": "mattermost",
                "endpoint": "https://chat.example.org",
                "agent_roles": ["live_presence"],
            },
            "connector": {
                "base_url": "https://chat.example.org",
                "instances": [
                    {
                        "name": "@atom.ant",
                        "inherit-from": "@min.botnick",
                        "password": "$MM_BOT_TOKEN_ATOM_ANT",
                    }
                ],
            },
        },
    )

    report = bootstrap_mailbox_state(mailbox_root)

    assert report["connectors_upserted"] == 1
    connector_id = "mattermost-chat.example.org"
    connector_payload = mailbox_store.get_entry(
        mailbox_root, mailbox_store.CONNECTORS_REGISTRY, connector_id,
    )["payload"]
    instances = connector_payload["instances"]
    names = {item.get("name") for item in instances}
    assert names == {"@min.botnick", "@atom.ant"}
    atom = next(item for item in instances if item.get("name") == "@atom.ant")
    assert atom["relay-type"] == "relay_via_bot"
    assert atom["mailbox_types"] == ["room-one"]
    assert atom["reply-in-threads"] is True
    assert atom["password"] == "$MM_BOT_TOKEN_ATOM_ANT"
