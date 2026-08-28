import json
from pathlib import Path

from mailbox_chat import mailbox_store
from mailbox_chat.bootstrap_config import bootstrap_mailbox_state
from mailbox_chat.endpoint_address import mailbox_resource_id


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_bootstrap_scans_presence_config_and_materializes_registries(tmp_path: Path) -> None:
    mailbox_root = tmp_path / "mailbox"
    config_root = mailbox_root / "config"

    _write_json(
        config_root / "system-agent-lifecycle.json",
        {
            "id": "system-lifecycle-agent",
            "payload": {
                "kind": "agent",
                "agent_kind": "role_agent",
                "agent_roles": ["lifecycle_emitter"],
                "writes_to_mailboxes": ["server_events_log"],
            },
        },
    )
    _write_json(
        config_root / "system-agent-event-queue.json",
        {
            "id": "event-queue-agent",
            "payload": {
                "kind": "agent",
                "agent_kind": "role_agent",
                "agent_roles": ["queue_manager"],
                "writes_to_mailboxes": ["server_worker_queue", "server_outbound_relay_agent_to_mailbox"],
            },
        },
    )
    _write_json(
        config_root / "server_builtin_agents.json",
        {
            "system_agents": [
                {"id": "system-lifecycle-agent", "file": "system-agent-lifecycle.json"},
                {"id": "event-queue-agent", "file": "system-agent-event-queue.json"},
            ]
        },
    )
    _write_json(
        config_root / "presence-mm-snet-min-botnick.json",
        {
            "id": "presence-mm-snet-min-botnick",
            "payload": {
                "kind": "agent",
                "agent_kind": "presence_agent",
                "adapter": "mattermost",
                "endpoint": "https://chat.example.org",
                "token_env": "MM_BOT_TOKEN_MIN",
            },
            "connector": {
                "adapter": "mattermost",
                "base_url": "https://chat.example.org",
                "mailbox_ids": ["$MM_CHANNEL_ID"],
                "instances": [
                    {"name": "@min.botnick", "mailbox_types": ["room-one"], "reply-in-threads": True}
                ],
            },
        },
    )
    _write_json(
        config_root / "presence-mm-snet-atom-ant.json",
        {
            "id": "presence-mm-snet-atom-ant",
            "payload": {
                "kind": "agent",
                "agent_kind": "presence_agent",
                "adapter": "mattermost",
                "endpoint": "https://chat.example.org",
                "token_env": "MM_BOT_TOKEN_ATOM_ANT",
            },
            "connector": {
                "base_url": "https://chat.example.org",
                "instances": [
                    {"name": "@atom.ant", "inherit-from": "@min.botnick", "password": "$MM_BOT_TOKEN_ATOM_ANT"}
                ],
            },
        },
    )
    _write_json(
        config_root / "presence-irc-libera-prologmud.json",
        {
            "id": "presence-irc-libera-prologmud",
            "payload": {
                "kind": "agent",
                "agent_kind": "presence_agent",
                "adapter": "irc",
                "server": "irc.libera.chat",
            },
            "connector": {
                "adapter": "irc",
                "server": "irc.libera.chat",
                "port": 6697,
                "tls": True,
                "nickname": "prologmud",
                "mailbox_ids": ["##logicmoo"],
            },
        },
    )
    _write_json(
        config_root / "presence-discord-prologmud-bot.json",
        {
            "id": "presence-discord-prologmud-bot",
            "payload": {
                "kind": "agent",
                "agent_kind": "presence_agent",
                "adapter": "discord",
                "endpoint": "https://discord.com",
                "token_env": "DISCORD_BOT_TOKEN_PROLOGMUD",
            },
            "connector": {
                "adapter": "discord",
                "mailbox_ids": ["$DISCORD_CHANNEL_ID"],
            },
        },
    )
    _write_json(
        config_root / "server_config_mailboxes.json",
        {
            "mailboxes": [
                {
                    "id": "server_events_log",
                    "mailbox_type": "system_builtin_mailboxes",
                    "payload": {"kind": "mailbox", "aliases": ["events"]},
                }
            ]
        },
    )

    report = bootstrap_mailbox_state(mailbox_root)

    assert report["missing_files"] == []
    assert report["agents_upserted"] == 6
    assert report["connectors_upserted"] == 3
    assert report["mailboxes_upserted"] >= 4
    assert mailbox_store.get_entry(mailbox_root, mailbox_store.AGENTS_REGISTRY, "presence-mm-snet-min-botnick")
    assert mailbox_store.get_entry(mailbox_root, mailbox_store.CONNECTORS_REGISTRY, "mattermost-chat.example.org")
    assert mailbox_store.get_entry(mailbox_root, mailbox_store.CONNECTORS_REGISTRY, "irc-irc.libera.chat")
    assert mailbox_store.get_entry(mailbox_root, mailbox_store.CONNECTORS_REGISTRY, "discord-discord.com")
    assert mailbox_store.get_entry(mailbox_root, mailbox_store.MAILBOXES_REGISTRY, "##logicmoo")
    assert mailbox_store.get_entry(
        mailbox_root,
        mailbox_store.MAILBOXES_REGISTRY,
        mailbox_resource_id("irc", "irc.libera.chat", "##logicmoo"),
    )

    mm_connector = mailbox_store.get_entry(
        mailbox_root, mailbox_store.CONNECTORS_REGISTRY, "mattermost-chat.example.org",
    )["payload"]
    atom = next(item for item in mm_connector["instances"] if item.get("name") == "@atom.ant")
    assert atom["mailbox_types"] == ["room-one"]
    assert atom["reply-in-threads"] is True
    assert atom["password"] == "$MM_BOT_TOKEN_ATOM_ANT"
    assert mm_connector["token_env"] == "MM_BOT_TOKEN_ATOM_ANT"
    assert mm_connector["token_env_fallbacks"] == [
        "MM_BOT_TOKEN_ATOM_ANT",
        "MM_BOT_TOKEN_MIN",
    ]


def test_bootstrap_uses_explicit_config_dir(tmp_path: Path) -> None:
    mailbox_root = tmp_path / "mailbox"
    config_root = tmp_path / "bootstrap-config"
    _write_json(config_root / "server_builtin_agents.json", {"system_agents": []})
    _write_json(
        config_root / "presence-irc-libera-prologmud.json",
        {
            "id": "presence-irc-libera-prologmud",
            "payload": {"kind": "agent", "agent_kind": "presence_agent", "adapter": "irc", "server": "irc.libera.chat"},
            "connector": {"adapter": "irc", "server": "irc.libera.chat", "mailbox_ids": ["##logicmoo"]},
        },
    )

    report = bootstrap_mailbox_state(mailbox_root, config_dir=config_root)

    assert report["config_root"] == str(config_root.resolve())
    assert report["agents_upserted"] == 1
    assert report["connectors_upserted"] == 1
    assert mailbox_store.get_entry(mailbox_root, mailbox_store.CONNECTORS_REGISTRY, "irc-irc.libera.chat")
