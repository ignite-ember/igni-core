"""The edges of the notification stream: the parser, the backoff, the follower.

``test_the_notification_stream_wakes_the_host`` pins the stream's
main road. These tests pin what is left: the lines the format says to
ignore, the delays and their bounds, every status the stream can be
answered with, and the follower's start, stop and reconnect rules.
"""

from __future__ import annotations

import httpx
import pytest

from igni_core.notifications.stream import (
    Backoff,
    NotificationStreamFollower,
    SseParser,
)
from tests.notification_helpers import (
    FakeNotificationServer,
    RecordingSleep,
    settle,
    signed_in,
)


def feed_all(*lines: str):
    parser = SseParser()
    result = None
    for line in lines:
        result = parser.feed(line)
    return result


# ── The parser ──────────────────────────────────────────────────────


class TestTheParserEdges:
    def test_a_blank_line_with_nothing_buffered_is_nothing(self):
        assert SseParser().feed("") is None

    def test_a_stream_of_comments_dispatches_nothing(self):
        assert feed_all(": a", ":b", "") is None

    def test_an_event_without_a_name_is_a_message(self):
        event = feed_all("data: x", "")
        assert event is not None
        assert event.event == "message"

    def test_an_empty_event_name_is_a_message(self):
        event = feed_all("event:", "data: x", "")
        assert event is not None
        assert event.event == "message"

    def test_only_one_space_after_the_colon_is_dropped(self):
        event = feed_all("data:  two", "")
        assert event is not None
        assert event.data == " two"

    def test_data_without_a_space_is_kept_whole(self):
        event = feed_all("data:x", "")
        assert event is not None
        assert event.data == "x"

    def test_a_field_without_a_colon_has_an_empty_value(self):
        event = feed_all("data", "data: b", "")
        assert event is not None
        assert event.data == "\nb"

    def test_an_unknown_field_is_ignored_and_dispatches_nothing(self):
        assert feed_all("colour: red", "") is None

    def test_an_id_with_a_nul_is_ignored(self):
        assert feed_all("id: a\0b", "") is None
        event = feed_all("id: a\0b", "data: 1", "")
        assert event is not None
        assert event.id is None

    def test_an_id_alone_is_returned_with_no_data(self):
        event = feed_all("id: c4", "")
        assert event is not None
        assert (event.id, event.data) == ("c4", "")

    @pytest.mark.parametrize("value", ["abc", "-5", "1.5", "", " 7"])
    def test_a_retry_that_is_not_plain_digits_is_ignored(self, value):
        event = feed_all(f"retry: {value}", "data: 1", "")
        assert event is not None
        assert event.retry_ms is None

    def test_a_retry_of_zero_is_a_retry(self):
        event = feed_all("retry: 0", "")
        assert event is not None
        assert event.retry_ms == 0

    def test_a_retry_alone_is_returned(self):
        event = feed_all("retry: 1500", "")
        assert event is not None
        assert (event.retry_ms, event.data) == (1500, "")

    def test_fields_do_not_leak_into_the_next_event(self):
        feed_all("event: a", "id: 1", "retry: 9", "data: x", "")
        event = feed_all("data: y", "")
        assert event is not None
        assert (event.event, event.id, event.retry_ms, event.data) == ("message", None, None, "y")


# ── The backoff ─────────────────────────────────────────────────────


class TestTheBackoff:
    def test_delays_double_to_the_ceiling(self):
        backoff = Backoff(base=1.0, ceiling=5.0, jitter=0.0)
        assert [backoff.next() for _ in range(5)] == [1.0, 2.0, 4.0, 5.0, 5.0]

    def test_reset_starts_the_sequence_again(self):
        backoff = Backoff(base=1.0, ceiling=60.0, jitter=0.0)
        backoff.next()
        backoff.next()
        backoff.reset()
        assert backoff.next() == 1.0

    def test_jitter_spreads_either_side_of_the_delay(self):
        low = Backoff(base=10.0, jitter=0.2, rng=lambda: 0.0)
        high = Backoff(base=10.0, jitter=0.2, rng=lambda: 1.0)
        assert low.next() == pytest.approx(8.0)
        assert high.next() == pytest.approx(12.0)

    def test_a_ceiling_below_the_base_wins(self):
        assert Backoff(base=10.0, ceiling=3.0, jitter=0.0).next() == 3.0

    def test_a_delay_is_never_negative(self):
        assert Backoff(base=1.0, jitter=5.0, rng=lambda: 0.0).next() == 0.0


# ── The follower ────────────────────────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Rig:
    """A follower on a fake server, with what it woke and reopened."""

    def __init__(self, server: FakeNotificationServer, *, session="s", transport=None) -> None:
        self.server = server
        self.sleep = RecordingSleep()
        self.clock = Clock()
        self.wakes = 0
        self.reopens = 0
        self.follower = NotificationStreamFollower(
            signed_in(session=session),
            on_reconnect=self._reopened,
            on_wake=self._woke,
            transport=transport or server.transport,
            backoff=Backoff(base=1.0, ceiling=8.0, rng=lambda: 0.5),
            sleep=self.sleep,
            clock=self.clock,
        )

    def _reopened(self) -> None:
        self.reopens += 1

    def _woke(self) -> None:
        self.wakes += 1


@pytest.fixture
def server() -> FakeNotificationServer:
    return FakeNotificationServer()


@pytest.fixture
async def rig(server):
    rig = Rig(server)
    yield rig
    await rig.follower.stop()


@pytest.mark.parametrize("status", [403, 404])
async def test_a_refusal_or_a_missing_stream_stops_the_follower(server, rig, status):
    server.stream_status = status
    rig.follower.start()
    await settle()

    assert not rig.follower.following
    assert rig.sleep.delays == []
    assert server.calls("/v1/notifications/stream") == 1


@pytest.mark.parametrize("status", [400, 429, 500, 502])
async def test_another_failing_status_is_retried_after_a_backoff(server, rig, status):
    server.stream_status = status
    rig.follower.start()
    await settle()

    assert rig.follower.following
    assert rig.sleep.delays == [1.0]
    rig.sleep.release()
    await settle()
    assert rig.sleep.delays == [1.0, 2.0]
    assert rig.wakes == 0


async def test_the_request_names_the_session_and_asks_for_an_event_stream(server):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404)

    rig = Rig(server, transport=httpx.MockTransport(handler))
    try:
        rig.follower.start()
        await settle()
    finally:
        await rig.follower.stop()

    (request,) = seen
    assert request.method == "GET"
    assert request.url.path == "/v1/notifications/stream"
    assert request.headers["accept"] == "text/event-stream"
    assert request.headers["cache-control"] == "no-cache"
    assert request.headers["authorization"].startswith("Bearer ")
    assert "last-event-id" not in request.headers


async def test_an_event_with_an_id_and_no_data_moves_the_cursor_and_wakes_nobody(server, rig):
    rig.follower.start()
    await server.wait_connected()
    await server.send("id: c8\n\n")
    await settle()

    assert rig.follower.cursor == "c8"
    assert rig.wakes == 0


async def test_an_event_with_data_wakes_the_host_once(server, rig):
    rig.follower.start()
    await server.wait_connected()
    await server.wake("c2")
    await settle()

    assert rig.wakes == 1


async def test_a_server_retry_is_the_shortest_delay(server, rig):
    rig.follower.start()
    await server.wait_connected()
    await server.send("retry: 5000\n\n")
    await server.end()
    await settle()

    # The backoff alone would say 1.0.
    assert rig.sleep.delays == [5.0]


async def test_a_server_retry_does_not_shorten_a_longer_backoff(server, rig):
    server.stream_status = 500
    rig.follower.start()
    await settle()
    rig.sleep.release()
    await settle()
    rig.sleep.release()
    await settle()
    assert rig.sleep.delays == [1.0, 2.0, 4.0]


async def test_a_connection_that_ended_cleanly_is_dropped_and_backed_off(server, rig):
    rig.follower.start()
    await server.wait_connected()
    await server.end()
    await settle()

    assert rig.follower.following
    assert rig.sleep.delays == [1.0]


async def test_a_connection_that_stayed_up_resets_the_backoff(server, rig):
    rig.follower.start()
    for n in range(2):
        await server.wait_connections(n + 1)
        await server.end()
        await settle()
        rig.sleep.release()
    await server.wait_connections(3)
    rig.clock.now += NotificationStreamFollower.STABLE_SECONDS
    await server.end()
    await settle()
    rig.sleep.release()
    await server.wait_connections(4)
    await server.end()
    await settle()

    assert rig.sleep.delays == [1.0, 2.0, 1.0, 2.0]


async def test_a_spent_connection_waits_the_server_retry_when_it_had_stayed_up(server, rig):
    rig.follower.start()
    await server.wait_connected()
    rig.clock.now += NotificationStreamFollower.STABLE_SECONDS
    await server.send("retry: 2000\nevent: reconnect\n\n")
    await settle()

    assert rig.sleep.delays == [2.0]


async def test_a_transport_error_is_retried_after_a_backoff(server):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    rig = Rig(server, transport=httpx.MockTransport(handler))
    try:
        rig.follower.start()
        await settle()
        assert rig.follower.following
        assert rig.sleep.delays == [1.0]
    finally:
        await rig.follower.stop()


async def test_a_read_timeout_is_a_dropped_connection(server):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("silence")

    rig = Rig(server, transport=httpx.MockTransport(handler))
    try:
        rig.follower.start()
        await settle()
        assert rig.sleep.delays == [1.0]
    finally:
        await rig.follower.stop()


async def test_the_read_timeout_is_longer_than_a_keepalive_gap(server):
    assert NotificationStreamFollower.READ_TIMEOUT == 60.0


async def test_a_wake_that_raises_is_retried_not_fatal(server):
    rig = Rig(server)

    def boom() -> None:
        raise RuntimeError("bad host")

    rig.follower._on_wake = boom
    try:
        rig.follower.start()
        await server.wait_connected()
        await server.wake("c2")
        await settle()
        assert rig.follower.following
        assert rig.sleep.delays == [1.0]
    finally:
        await rig.follower.stop()


async def test_a_second_start_while_following_changes_nothing(server, rig):
    rig.follower.start()
    await server.wait_connected()
    rig.follower.start()
    await settle()

    assert server.calls("/v1/notifications/stream") == 1


async def test_stop_is_idempotent_and_ends_following(server, rig):
    await rig.follower.stop()
    rig.follower.start()
    await server.wait_connected()
    assert rig.follower.following

    await rig.follower.stop()
    await rig.follower.stop()

    assert not rig.follower.following


async def test_reconnect_opens_afresh_with_the_cursor_and_fires_on_reconnect(server, rig):
    rig.follower.start()
    await server.wait_connected()
    await server.wake("c6")
    await settle()

    await rig.follower.reconnect()
    await server.wait_connections(2)
    await settle()

    assert server.stream_cursors == [None, "c6"]
    assert rig.follower.following
    assert rig.sleep.delays == []


async def test_reconnect_resets_the_backoff(server, rig):
    rig.follower.start()
    for n in range(2):
        await server.wait_connections(n + 1)
        await server.end()
        await settle()
        rig.sleep.release()
    await server.wait_connections(3)

    await rig.follower.reconnect()
    await server.wait_connections(4)
    await server.end()
    await settle()

    assert rig.sleep.delays[-1] == 1.0


async def test_reconnect_does_nothing_when_not_following(server, rig):
    await rig.follower.reconnect()
    await settle()

    assert not rig.follower.following
    assert server.requests == []


async def test_restart_while_stopped_follows_without_a_cursor(server, rig):
    await rig.follower.restart()
    await server.wait_connected()

    assert rig.follower.following
    assert server.stream_cursors == [None]


async def test_the_first_open_after_a_restart_is_not_a_reconnect(server, rig):
    rig.follower.start()
    await server.wait_connected()
    await server.end()
    await settle()
    rig.sleep.release()
    await server.wait_connections(2)
    await settle()
    assert rig.reopens == 1

    await rig.follower.restart()
    await server.wait_connections(3)
    await settle()

    assert rig.reopens == 1


async def test_nobody_signed_in_at_a_reconnect_stops_the_follower(server):
    rig = Rig(server)
    try:
        rig.follower.start()
        await server.wait_connected()
        rig.follower._server = signed_in(session=None)
        await server.end()
        await settle()
        rig.sleep.release()
        await settle()

        assert not rig.follower.following
    finally:
        await rig.follower.stop()
