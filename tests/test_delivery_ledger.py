from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mailbox_channel_relay_bridging_proxy.delivery_ledger import DeliveryLedger, endpoint_id, origin_id, with_origin


def test_origin_survives_relay_message_ids() -> None:
    fields = with_origin({}, adapter="irc", listener_id="libera", source_id="msg-1", channel_id="#agents")
    first = {"id": "mail-1", **fields}
    second = {"id": "mail-2", "origin_id": first["origin_id"]}
    assert origin_id(first) == origin_id(second)


def test_delivery_claim_is_durable_and_atomic(tmp_path: Path) -> None:
    message = {"id": "mail-1", "origin_id": "origin:one"}
    endpoint = endpoint_id("irc", listener_id="libera", channel_id="#agents")
    ledger = DeliveryLedger(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: ledger.claim(message, endpoint), range(16)))
    assert results.count(True) == 1
    assert results.count(False) == 15
    assert DeliveryLedger(tmp_path).traversals("origin:one") == [endpoint]


def test_same_origin_can_traverse_distinct_endpoints(tmp_path: Path) -> None:
    ledger = DeliveryLedger(tmp_path)
    message = {"id": "mail-1", "origin_id": "origin:one"}
    assert ledger.claim(message, endpoint_id("irc", listener_id="one", channel_id="#a"))
    assert ledger.claim(message, endpoint_id("irc", listener_id="two", channel_id="#a"))
