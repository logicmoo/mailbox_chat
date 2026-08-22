"""Shared test fixtures for the channel/registry store.

The runtime keeps all durable state as per-channel JSON documents under a single
mailbox root (message channels plus ``server_registry_*`` id-keyed registries).
These helpers seed that store so tests need not hand-craft registry documents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mailbox_channels import channel_store


@pytest.fixture(autouse=True)
def _isolate_real_store(tmp_path_factory, monkeypatch):
    """Never let a test touch the repo's real ./mailbox or ./config.

    Both the mailbox store (``cwd/mailbox``) and the config dir (``cwd/config``)
    default to the current working directory, so chdir'ing into a per-test temp
    dir isolates them. This leaves transport selection (AGENT_MAILBOX_URL vs
    AGENT_MAILBOX_DIR precedence) untouched. Tests that set env vars or pass an
    explicit root/--dir still override this.
    """
    base = tmp_path_factory.mktemp("isolated_store")
    (base / "mailbox").mkdir()
    (base / "config").mkdir()
    monkeypatch.chdir(base)
    yield


def seed_agents(root: Path, agents: list[dict[str, Any]]) -> None:
    for agent in agents:
        agent_id = str(agent["agent_id"])
        payload = {"kind": "agent", "presences": [], **agent, "agent_id": agent_id}
        channel_store.put_entry(root, channel_store.AGENTS_REGISTRY, agent_id, payload)


def seed_connectors(root: Path, connectors: list[dict[str, Any]]) -> None:
    for connector in connectors:
        connector_id = str(connector["id"])
        payload = {"kind": "connector", **connector, "id": connector_id}
        channel_store.put_entry(root, channel_store.CONNECTORS_REGISTRY, connector_id, payload)


def seed_channels(root: Path, channels: list[dict[str, Any]]) -> None:
    for channel in channels:
        channel_id = str(channel["id"])
        payload = {"kind": "channel", "subscribers": [],
                   **{key: value for key, value in channel.items() if key != "id"}}
        channel_store.put_entry(root, channel_store.CHANNELS_REGISTRY, channel_id, payload)


def seed_relays(root: Path, relays: list[dict[str, Any]]) -> None:
    for relay in relays:
        relay_id = str(relay["id"])
        payload = {"kind": "relay", **relay, "id": relay_id}
        channel_store.put_entry(root, channel_store.RELAYS_REGISTRY, relay_id, payload)


def seed_messages(root: Path, channel_id: str, records: list[dict[str, Any]],
                  *, sub_kind: str = "channel") -> None:
    for record in records:
        channel_store.append_message(root, channel_id, record, sub_kind=sub_kind)


def seed_store(root: Path, *, agents: list[dict[str, Any]] | None = None,
               connectors: list[dict[str, Any]] | None = None,
               subscriptions: list[dict[str, Any]] | None = None,
               relays: list[dict[str, Any]] | None = None) -> Path:
    """Seed a mailbox root with registry entries from plain dictionaries."""
    root.mkdir(parents=True, exist_ok=True)
    seed_agents(root, agents or [])
    seed_connectors(root, connectors or [])
    seed_channels(root, subscriptions or [])
    seed_relays(root, relays or [])
    return root


@pytest.fixture
def store_seeder():
    """Return the :func:`seed_store` helper for convenience in tests."""
    return seed_store
