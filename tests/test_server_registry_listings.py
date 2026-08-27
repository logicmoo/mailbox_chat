from mailbox_chat import server


def test_mailbox_listing_payload_uses_registry_mailboxes() -> None:
    payload = server._mailbox_listing_payload({
        "version": 7,
        "mailboxes": [{"id": "alpha"}, {"id": "beta"}],
    })
    assert payload == {
        "version": 7,
        "mailboxes": [{"id": "alpha"}, {"id": "beta"}],
        "count": 2,
    }


def test_service_listing_payload_maps_connectors_to_services() -> None:
    payload = server._service_listing_payload({
        "version": 3,
        "connectors": [
            {
                "id": "irc-libera",
                "adapter": "irc",
                "instance": "irc.libera.chat",
                "enabled": True,
                "can_listen": True,
                "can_send": True,
                "mailbox_ids": ["##logicmoo"],
            },
        ],
    })
    assert payload == {
        "version": 3,
        "services": [
            {
                "id": "irc-libera",
                "adapter": "irc",
                "instance": "irc.libera.chat",
                "enabled": True,
                "can_listen": True,
                "can_send": True,
                "mailbox_ids": ["##logicmoo"],
            },
        ],
        "count": 1,
    }
