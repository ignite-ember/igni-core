"""The desktop's event pull as a loop: its pace, its stops, and what it will not accept.

:class:`~igni_core.extensions.event_pull.PersonEventPull` pulls again at
once after a full page, waits after a short one, retries when nobody is
signed in or igni server fails, and stops for good when it is refused.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from igni_core.extensions.event_pull import PersonEventPull, PullFailed
from igni_core.extensions.service_events import DeliveryReceipt, ServiceEvent
from igni_core.extensions.service_token import IgniSession
from igni_core.notifications.server_client import SignedInServer


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


class Inbox:
    async def deliver(self, events: list[ServiceEvent]) -> DeliveryReceipt:
        return DeliveryReceipt(delivered=[e.event_id for e in events])


class Igni:
    """igni server: answers each pull with the next scripted reply."""

    def __init__(self, *replies: Callable[[], httpx.Response]) -> None:
        self.replies = list(replies)
        self.pulls = 0
        self.headers: list[str] = []
        self.acks: list[dict[str, Any]] = []
        self.ack_status = 200

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers["Authorization"])
        if request.url.path == PersonEventPull.ACK_PATH:
            self.acks.append(json.loads(request.content))
            return httpx.Response(self.ack_status)
        self.pulls += 1
        reply = self.replies.pop(0) if self.replies else (lambda: _page())
        return reply()


def _page(*ids: str) -> httpx.Response:
    return httpx.Response(200, json={"events": [_event(i) for i in ids]})


def _pull(igni: Igni, *, token: str | None = "the-session") -> PersonEventPull:
    server = SignedInServer(
        api_url=lambda: "https://igni.test",
        session=lambda: IgniSession(access_token=token) if token else None,
    )
    return PersonEventPull(
        server=server,
        inbox=Inbox(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(igni.handle),
    )


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.005)


@pytest.fixture
def small_pages(monkeypatch):
    monkeypatch.setattr(PersonEventPull, "PAGE", 2)
    monkeypatch.setattr(PersonEventPull, "INTERVAL_SECONDS", 3600.0)


async def test_a_full_page_is_followed_at_once_and_a_short_one_by_a_wait(small_pages):
    igni = Igni(lambda: _page("a", "b"), lambda: _page("c", "d"), lambda: _page("e"))
    pull = _pull(igni)
    pull.start()
    try:
        await _until(lambda: igni.pulls == 3)
        await asyncio.sleep(0.05)
        assert igni.pulls == 3, "after the short page igni waits the interval"
        assert [a["event_ids"] for a in igni.acks] == [["a", "b"], ["c", "d"], ["e"]]
    finally:
        await pull.stop()


async def test_a_pull_that_fails_or_finds_nobody_signed_in_is_asked_again_after_the_wait(
    monkeypatch,
):
    monkeypatch.setattr(PersonEventPull, "INTERVAL_SECONDS", 0.01)
    igni = Igni(lambda: httpx.Response(503), lambda: httpx.Response(200, content=b"nope"))
    pull = _pull(igni)
    pull.start()
    try:
        await _until(lambda: igni.pulls >= 4)
    finally:
        await pull.stop()

    signed_out = _pull(Igni(), token=None)
    monkeypatch.setattr(PersonEventPull, "INTERVAL_SECONDS", 0.01)
    signed_out.start()
    await asyncio.sleep(0.05)
    assert signed_out._task is not None and not signed_out._task.done()
    await signed_out.stop()


async def test_a_refusal_ends_the_loop_for_good(small_pages):
    igni = Igni(lambda: httpx.Response(403))
    pull = _pull(igni)
    pull.start()
    assert pull._task is not None
    async with asyncio.timeout(5):
        await asyncio.wait({pull._task})
    assert igni.pulls == 1
    await pull.stop()


async def test_starting_twice_runs_one_loop_and_stopping_ends_it(small_pages):
    igni = Igni()
    pull = _pull(igni)
    pull.start()
    first = pull._task
    pull.start()
    assert pull._task is first
    await _until(lambda: igni.pulls >= 1)
    await pull.stop()
    assert first is not None and first.done()
    assert pull._task is None
    await pull.stop()


async def test_a_stop_before_any_start_is_harmless():
    await _pull(Igni()).stop()


async def test_a_body_that_is_not_events_is_a_failure_not_a_crash():
    for content in (b"not json", b'{"events": [{"event_id": 1}]}', b'{"events": "x"}'):
        igni = Igni(lambda content=content: httpx.Response(200, content=content))
        with pytest.raises(PullFailed, match="malformed"):
            await _pull(igni).once()


@pytest.mark.parametrize("status", [201, 204, 301, 401, 404, 500])
async def test_any_status_but_200_and_403_is_a_failure(status):
    with pytest.raises(PullFailed, match=str(status)):
        await _pull(Igni(lambda: httpx.Response(status))).once()


async def test_an_unreachable_server_is_a_failure():
    def refuse() -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(PullFailed, match="could not be reached"):
        await _pull(Igni(refuse)).once()


async def test_every_call_carries_the_session_as_a_bearer_token():
    igni = Igni(lambda: _page("a"))
    await _pull(igni, token="s3cret").once()
    assert igni.headers == ["Bearer s3cret", "Bearer s3cret"], "the pull and its acknowledgement"


async def test_an_acknowledgement_that_fails_is_a_failure_after_the_delivery():
    igni = Igni(lambda: _page("a"))
    igni.ack_status = 500
    with pytest.raises(PullFailed, match="500"):
        await _pull(igni).once()
    assert [a["event_ids"] for a in igni.acks] == [["a"]], (
        "it was delivered; its lease will run out"
    )
