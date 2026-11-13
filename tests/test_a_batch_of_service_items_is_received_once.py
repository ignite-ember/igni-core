"""How a batch of items is received: once each, in a receipt the gateway can act on.

The real inbox, lander and stores run over a real database (the rig); only
the chats are played. The receipt says what to acknowledge, what to report
as not handled and what to hand out again.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from igni_core.core.db.database import Database
from igni_core.core.db.urls import DatabaseUrl
from igni_core.extensions.service_events import (
    DeliveredEvents,
    DeliveryReceipt,
    EventHandling,
    NotHandled,
    ServiceEvent,
)
from tests.service_item_rig import Files, Inbox, item, message
from tests.storage_backends import Scopes


def test_the_receipt_acknowledges_delivered_then_not_handled_then_duplicates_and_never_failed():
    receipt = DeliveryReceipt(
        delivered=["d1", "d2"],
        not_handled=[NotHandled(event_id="n1", reason="r")],
        duplicate=["u1"],
        failed=["f1"],
    )
    assert receipt.acknowledge == ["d1", "d2", "n1", "u1"]


async def test_an_empty_batch_has_an_empty_receipt(database_url: DatabaseUrl):
    receipt = await Inbox(database_url).deliver()

    assert receipt == DeliveryReceipt() and receipt.acknowledge == []


async def test_an_id_repeated_in_one_batch_is_handled_once_and_the_repeat_is_a_duplicate(
    database_url: DatabaseUrl,
):
    handled: list[str] = []

    async def hand(event: ServiceEvent) -> EventHandling:
        handled.append(event.event_id)
        return EventHandling(handled=True)

    rig = Inbox(database_url, hand=hand)
    receipt = await rig.deliver(item("e1"), item("e1"))

    assert handled == ["e1"]
    assert (receipt.delivered, receipt.duplicate) == (["e1"], ["e1"])


async def test_a_landing_that_fails_is_listed_unrecorded_and_tried_again_next_time(
    database_url: DatabaseUrl,
):
    rig = Inbox(database_url)
    rig.desk.refuse = True
    first = await rig.deliver(message("m1", text="x"), item("e1"))

    assert first.failed == ["m1"], "the refused post"
    assert "m1" not in first.acknowledge, "left to be handed out again"
    assert first.not_handled[0].event_id == "e1", "and the batch went on"

    rig.desk.refuse = False
    second = await rig.deliver(message("m1", text="x"))
    assert (second.delivered, second.failed) == (["m1"], [])


async def test_a_failure_in_the_middle_does_not_stop_the_items_after_it(
    database_url: DatabaseUrl,
):
    rig = Inbox(database_url, files=Files())
    broken = message("m1", text="x", attachments=[{"attachment_id": "gone", "filename": "f"}])
    # Files() holds nothing, so the fetch raises KeyError: any landing error is the item's alone.
    receipt = await rig.deliver(broken, message("m2", text="y"))

    assert (receipt.failed, receipt.delivered) == (["m1"], ["m2"])


async def test_events_of_a_batch_are_handled_at_once_not_one_after_another(
    database_url: DatabaseUrl,
):
    async def slow(event: ServiceEvent) -> EventHandling:
        await asyncio.sleep(0.3)
        return EventHandling(handled=True)

    rig = Inbox(database_url, hand=slow)
    started = time.monotonic()
    receipt = await rig.deliver(*(item(f"e{n}") for n in range(5)))

    assert len(receipt.delivered) == 5
    assert time.monotonic() - started < 1.0, "five slow handlers took the slowest, not the sum"


async def test_a_handler_that_raises_is_not_handled_and_names_the_extension(
    database_url: DatabaseUrl,
):
    async def boom(event: ServiceEvent) -> EventHandling:
        raise ConnectionError("socket closed")

    rig = Inbox(database_url, hand=boom)
    receipt = await rig.deliver(item("e1"))

    assert receipt.not_handled == [
        NotHandled(event_id="e1", reason="acme.claims was not reached: socket closed")
    ]
    assert receipt.delivered == [] and receipt.acknowledge == ["e1"]


async def test_with_no_handler_wired_an_event_is_not_handled_and_not_recorded(
    database_url: DatabaseUrl,
):
    rig = Inbox(database_url)
    receipt = await rig.deliver(item("e1"))

    assert receipt.not_handled[0].reason == "this igni hands events to no extension"
    assert not DeliveredEvents(rig.db).seen("e1")


def test_a_recorded_id_is_seen_and_recording_it_again_is_harmless(database_url: DatabaseUrl):
    delivered = DeliveredEvents(Database(database_url, scope=Scopes.fresh()))
    event = item("e1")

    assert not delivered.seen("e1")
    delivered.record(event)
    delivered.record(event)
    assert delivered.seen("e1")


def test_an_id_recorded_for_one_producer_is_new_to_another(database_url: DatabaseUrl):
    first, second = Scopes.pair(database_url)
    DeliveredEvents(first).record(item("e1"))

    assert DeliveredEvents(first).seen("e1")
    assert not DeliveredEvents(second).seen("e1")


async def test_two_producers_each_land_the_same_event_id(database_url: DatabaseUrl):
    first, second = Inbox(database_url), Inbox(database_url)
    await first.deliver(message("m1", text="x"))
    receipt: Any = await second.deliver(message("m1", text="x"))

    assert receipt.delivered == ["m1"] and receipt.duplicate == []
