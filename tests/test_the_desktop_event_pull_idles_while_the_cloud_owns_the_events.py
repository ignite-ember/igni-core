"""While the person's igni in the cloud owns their events, the desktop's pull idles.

``docs/design/local-cloud-switch.md`` §7: igni server serves a person's
events to one side at a time. The desktop's pull, while the cloud owns
them, answers ``409 {"error": "events_owned_elsewhere", "owner": "cloud"}``.
That is no failure and no refusal: the pull waits the interval, says so
once, and takes the events again when the owner comes back.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from igni_core.extensions.event_pull import (
    EventsOwnedElsewhere,
    PersonEventPull,
    PullFailed,
)
from igni_core.extensions.service_events import DeliveryReceipt, ServiceEvent
from igni_core.extensions.service_token import IgniSession
from igni_core.notifications.server_client import SignedInServer

LOGGER = "igni_core.extensions.event_pull"


def _event(event_id: str) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "extension_id": "acme.indexer",
        "client_id": "c1",
        "type": "index.ready",
        "payload": {},
        "event_key": None,
        "created_at": "2026-10-27T09:00:00Z",
        "expires_at": "2026-11-10T09:00:00Z",
    }


def _owned_by_the_cloud() -> httpx.Response:
    return httpx.Response(409, json={"error": "events_owned_elsewhere", "owner": "cloud"})


class Inbox:
    def __init__(self) -> None:
        self.got: list[str] = []

    async def deliver(self, events: list[ServiceEvent]) -> DeliveryReceipt:
        self.got += [e.event_id for e in events]
        return DeliveryReceipt(delivered=[e.event_id for e in events])


class Igni:
    """igni server: answers each pull with the next scripted reply, then an empty page."""

    def __init__(self, *replies: Callable[[], httpx.Response]) -> None:
        self.replies = list(replies)
        self.pulls = 0
        self.acks = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == PersonEventPull.ACK_PATH:
            self.acks += 1
            return httpx.Response(200)
        self.pulls += 1
        if self.replies:
            return self.replies.pop(0)()
        return httpx.Response(200, json={"events": []})


def _pull(igni: Igni) -> tuple[PersonEventPull, Inbox]:
    inbox = Inbox()
    server = SignedInServer(
        api_url=lambda: "https://igni.test",
        session=lambda: IgniSession(access_token="the-session"),
    )
    pull = PersonEventPull(
        server=server,
        inbox=inbox,  # type: ignore[arg-type]
        transport=httpx.MockTransport(igni.handle),
    )
    return pull, inbox


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.005)


async def test_one_pull_while_the_cloud_owns_the_events_says_so_and_delivers_nothing():
    igni = Igni(_owned_by_the_cloud)
    pull, inbox = _pull(igni)
    with pytest.raises(EventsOwnedElsewhere) as raised:
        await pull.once()
    assert raised.value.owner == "cloud"
    assert "cloud" in str(raised.value)
    assert inbox.got == [] and igni.acks == 0


async def test_an_owner_the_server_does_not_name_still_reads_as_elsewhere():
    igni = Igni(lambda: httpx.Response(409, json={"error": "events_owned_elsewhere"}))
    with pytest.raises(EventsOwnedElsewhere) as raised:
        await _pull(igni)[0].once()
    assert raised.value.owner == ""
    assert "other side" in str(raised.value)


@pytest.mark.parametrize(
    "reply",
    [
        lambda: httpx.Response(409),
        lambda: httpx.Response(409, content=b"not json"),
        lambda: httpx.Response(409, json={"owner": "cloud"}),
    ],
    ids=["no body", "not json", "no error"],
)
async def test_a_409_that_is_not_the_owner_s_is_a_failure(reply):
    with pytest.raises(PullFailed, match="409"):
        await _pull(Igni(reply))[0].once()


async def test_a_409_naming_another_conflict_is_a_failure_that_names_it():
    reply = lambda: httpx.Response(409, json={"error": "lease_held"})  # noqa: E731
    with pytest.raises(PullFailed, match=r"409 \(lease_held\)"):
        await _pull(Igni(reply))[0].once()


async def test_the_loop_idles_waits_the_interval_and_keeps_asking(monkeypatch, caplog):
    """No busy loop: one pull per interval, and one line said, not one per pull."""
    monkeypatch.setattr(PersonEventPull, "INTERVAL_SECONDS", 0.02)
    igni = Igni(*[_owned_by_the_cloud] * 50)
    pull, _ = _pull(igni)
    caplog.set_level(logging.INFO, logger=LOGGER)
    pull.start()
    try:
        await _until(lambda: igni.pulls >= 4)
        assert pull.idle
        assert pull._task is not None and not pull._task.done(), "the loop goes on"
    finally:
        await pull.stop()
    said = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert len(said) == 1, said
    assert "cloud" in said[0] and caplog.records[0].levelno == logging.INFO


async def test_the_loop_does_not_busy_loop_while_idle(monkeypatch):
    monkeypatch.setattr(PersonEventPull, "INTERVAL_SECONDS", 3600.0)
    igni = Igni(*[_owned_by_the_cloud] * 50)
    pull, _ = _pull(igni)
    pull.start()
    try:
        await _until(lambda: igni.pulls >= 1)
        await asyncio.sleep(0.05)
        assert igni.pulls == 1, "it waits the interval before asking again"
    finally:
        await pull.stop()


async def test_the_loop_takes_the_events_again_when_the_owner_comes_back(monkeypatch, caplog):
    monkeypatch.setattr(PersonEventPull, "INTERVAL_SECONDS", 0.01)
    igni = Igni(
        _owned_by_the_cloud,
        _owned_by_the_cloud,
        lambda: httpx.Response(200, json={"events": [_event("back")]}),
    )
    pull, inbox = _pull(igni)
    caplog.set_level(logging.INFO, logger=LOGGER)
    pull.start()
    try:
        await _until(lambda: inbox.got == ["back"])
        await _until(lambda: not pull.idle)
        assert igni.acks == 1
    finally:
        await pull.stop()
    said = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert said[0].startswith("events: the events go to the cloud side")
    assert said[1] == "events: this side gets them again"
    assert len(said) == 2


async def test_a_second_spell_of_idling_is_said_again(monkeypatch, caplog):
    monkeypatch.setattr(PersonEventPull, "INTERVAL_SECONDS", 0.01)
    igni = Igni(
        _owned_by_the_cloud,
        lambda: httpx.Response(200, json={"events": []}),
        _owned_by_the_cloud,
        _owned_by_the_cloud,
    )
    pull, _ = _pull(igni)
    caplog.set_level(logging.INFO, logger=LOGGER)
    pull.start()
    try:
        await _until(lambda: igni.pulls >= 5)
    finally:
        await pull.stop()
    said = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert [m.split(";")[0] for m in said] == [
        "events: the events go to the cloud side",
        "events: this side gets them again",
        "events: the events go to the cloud side",
        "events: this side gets them again",
    ]


async def test_a_pull_that_never_idled_says_nothing_when_it_succeeds(monkeypatch, caplog):
    monkeypatch.setattr(PersonEventPull, "INTERVAL_SECONDS", 0.01)
    igni = Igni()
    pull, _ = _pull(igni)
    caplog.set_level(logging.INFO, logger=LOGGER)
    pull.start()
    try:
        await _until(lambda: igni.pulls >= 3)
    finally:
        await pull.stop()
    assert not pull.idle
    assert [r for r in caplog.records if r.name == LOGGER] == []
