"""The busy HTTP clients' contract (``core/activity/http.py``, ``http2.py``).

Both the ``httpx`` and the ``httpx2`` client hold a lease named
``METHOD host`` from send until the answer is read, and a streamed
answer until it is closed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import httpx2
import pytest

from igni_core.core.activity import ActivityRegistry, BusyKind
from igni_core.core.activity import http as http_module
from igni_core.core.activity import http2 as http2_module
from igni_core.core.activity.http import BusyAsyncClient, BusyClient, busy_call


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Chunks:
    """A body that counts how often it was closed and may fail to close."""

    def __init__(self, fail_on_close: bool = False) -> None:
        self.closed = 0
        self.fail_on_close = fail_on_close

    async def __aiter__(self):
        for chunk in (b"a", b"b", b"c"):
            yield chunk

    async def aclose(self) -> None:
        self.closed += 1
        if self.fail_on_close:
            raise OSError("close failed")


class SyncChunks:
    def __init__(self, fail_on_close: bool = False) -> None:
        self.closed = 0
        self.fail_on_close = fail_on_close

    def __iter__(self):
        yield from (b"a", b"b", b"c")

    def close(self) -> None:
        self.closed += 1
        if self.fail_on_close:
            raise OSError("close failed")


def async_stream(lib: Any, body: Chunks):
    class Stream(lib.AsyncByteStream):
        async def __aiter__(self):
            async for chunk in body:
                yield chunk

        async def aclose(self) -> None:
            await body.aclose()

    return Stream()


def sync_stream(body: SyncChunks):
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield from body

        def close(self) -> None:
            body.close()

    return Stream()


ASYNC_LIBS = [
    pytest.param(httpx, http_module.BusyAsyncClient, id="httpx"),
    pytest.param(httpx2, http2_module.BusyAsyncClient, id="httpx2"),
]

# ── busy_call ─────────────────────────────────────────────────────


async def test_busy_call_is_a_lease_on_the_shared_registry_for_both_with_forms():
    registry = ActivityRegistry.shared()
    with busy_call("igni-server", "GET me") as lease:
        [held] = registry.busy()
        assert (held.id, held.kind, held.holder, held.reason) == (
            lease.id,
            BusyKind.HTTP_CALL,
            "igni-server",
            "GET me",
        )
    assert registry.busy() == []
    async with busy_call("svc", kind=BusyKind.MCP_CALL):
        [held] = registry.busy()
        assert (held.kind, held.reason) == (BusyKind.MCP_CALL, "")
    assert registry.busy() == []


def test_a_busy_client_needs_a_holder():
    with pytest.raises(TypeError):
        BusyAsyncClient()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        BusyClient()  # type: ignore[call-arg]


def test_the_stream_lease_lasts_fifteen_minutes():
    assert BusyAsyncClient.STREAM_TTL_SECONDS == BusyClient.STREAM_TTL_SECONDS == 900
    assert http2_module.BusyAsyncClient.STREAM_TTL_SECONDS == 900


# ── Async, both libraries ─────────────────────────────────────────


@pytest.mark.parametrize(("lib", "client_cls"), ASYNC_LIBS)
async def test_a_lease_is_named_by_method_and_host_and_held_only_while_the_request_is_out(
    lib: Any, client_cls: Any
):
    registry = ActivityRegistry()
    seen: list[Any] = []

    def answer(request: Any) -> Any:
        seen.extend(registry.busy())
        return lib.Response(200, json={})

    async with client_cls(
        busy_holder="svc",
        busy_kind=BusyKind.MCP_CALL,
        busy_registry=registry,
        transport=lib.MockTransport(answer),
    ) as client:
        await client.post("https://api.example.test:8443/v1/x")
    [held] = seen
    assert (held.kind, held.holder, held.reason) == (
        BusyKind.MCP_CALL,
        "svc",
        "POST api.example.test",
    )
    assert registry.busy() == []


@pytest.mark.parametrize(("lib", "client_cls"), ASYNC_LIBS)
async def test_the_registry_is_the_shared_one_resolved_when_the_request_is_sent(
    lib: Any, client_cls: Any
):
    seen: list[int] = []

    def answer(request: Any) -> Any:
        seen.append(len(ActivityRegistry.shared().busy()))
        return lib.Response(200)

    client = client_cls(busy_holder="svc", transport=lib.MockTransport(answer))
    ActivityRegistry.reset_shared()
    async with client:
        await client.get("https://h.test/")
    assert seen == [1], "the lease lands in the registry current at send, not at construction"


@pytest.mark.parametrize(("lib", "client_cls"), ASYNC_LIBS)
async def test_a_failed_send_lets_go_and_raises(lib: Any, client_cls: Any):
    registry = ActivityRegistry()

    def refuse(request: Any) -> Any:
        raise lib.ConnectError("no route", request=request)

    async with client_cls(
        busy_holder="svc", busy_registry=registry, transport=lib.MockTransport(refuse)
    ) as client:
        with pytest.raises(lib.ConnectError):
            await client.get("https://h.test/")
    assert registry.busy() == []


@pytest.mark.parametrize(("lib", "client_cls"), ASYNC_LIBS)
async def test_a_cancelled_send_lets_go(lib: Any, client_cls: Any):
    import asyncio

    registry = ActivityRegistry()

    def cancel(request: Any) -> Any:
        raise asyncio.CancelledError

    async with client_cls(
        busy_holder="svc", busy_registry=registry, transport=lib.MockTransport(cancel)
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.get("https://h.test/")
    assert registry.busy() == []


@pytest.mark.parametrize(("lib", "client_cls"), ASYNC_LIBS)
async def test_a_streamed_answer_holds_a_renewed_lease_until_closed_and_passes_chunks(
    lib: Any, client_cls: Any
):
    clock = Clock()
    registry = ActivityRegistry(clock=clock)
    body = Chunks()
    client = client_cls(
        busy_holder="model",
        busy_kind=BusyKind.MODEL_CALL,
        busy_registry=registry,
        transport=lib.MockTransport(lambda r: lib.Response(200, stream=async_stream(lib, body))),
    )
    async with client:
        response = await client.send(client.build_request("GET", "https://m.test/"), stream=True)
        [held] = registry.busy()
        assert held.expires_at == clock.now + timedelta(seconds=900)
        chunks = response.aiter_raw()
        assert await chunks.__anext__() == b"a"
        assert registry.is_busy(), "read but not closed"
        assert [c async for c in chunks] == [b"b", b"c"]
        assert registry.busy() == [] and body.closed == 1, "reading to the end closes it"
        await response.aclose()
        assert body.closed == 1


@pytest.mark.parametrize(("lib", "client_cls"), ASYNC_LIBS)
async def test_an_unclosed_stream_stops_counting_after_its_ttl(lib: Any, client_cls: Any):
    clock = Clock()
    registry = ActivityRegistry(clock=clock)
    client = client_cls(
        busy_holder="model",
        busy_registry=registry,
        transport=lib.MockTransport(
            lambda r: lib.Response(200, stream=async_stream(lib, Chunks()))
        ),
    )
    async with client:
        await client.send(client.build_request("GET", "https://m.test/"), stream=True)
        clock.now += timedelta(seconds=899)
        assert registry.is_busy()
        clock.now += timedelta(seconds=1)
        assert not registry.is_busy()


@pytest.mark.parametrize(("lib", "client_cls"), ASYNC_LIBS)
async def test_a_stream_whose_inner_close_raises_still_lets_go(lib: Any, client_cls: Any):
    registry = ActivityRegistry()
    client = client_cls(
        busy_holder="model",
        busy_registry=registry,
        transport=lib.MockTransport(
            lambda r: lib.Response(200, stream=async_stream(lib, Chunks(fail_on_close=True)))
        ),
    )
    async with client:
        response = await client.send(client.build_request("GET", "https://m.test/"), stream=True)
        assert registry.is_busy()
        with pytest.raises(OSError, match="close failed"):
            await response.aclose()
    assert registry.busy() == []


@pytest.mark.parametrize(("lib", "client_cls"), ASYNC_LIBS)
async def test_an_already_closed_streamed_answer_lets_go_at_once(lib: Any, client_cls: Any):
    registry = ActivityRegistry()
    client = client_cls(
        busy_holder="svc",
        busy_registry=registry,
        transport=lib.MockTransport(lambda r: lib.Response(200, content=b"whole")),
    )
    async with client:
        response = await client.send(client.build_request("GET", "https://h.test/"), stream=True)
        assert response.is_closed
        assert registry.busy() == []


@pytest.mark.parametrize(("lib", "client_cls"), ASYNC_LIBS)
async def test_concurrent_requests_each_hold_their_own_lease(lib: Any, client_cls: Any):
    registry = ActivityRegistry()
    client = client_cls(
        busy_holder="svc",
        busy_registry=registry,
        transport=lib.MockTransport(
            lambda r: lib.Response(200, stream=async_stream(lib, Chunks()))
        ),
    )
    async with client:
        one = await client.send(client.build_request("GET", "https://a.test/"), stream=True)
        two = await client.send(client.build_request("GET", "https://b.test/"), stream=True)
        assert sorted(lease.reason for lease in registry.busy()) == ["GET a.test", "GET b.test"]
        await one.aclose()
        assert [lease.reason for lease in registry.busy()] == ["GET b.test"]
        await two.aclose()
    assert registry.busy() == []


# ── Sync client ───────────────────────────────────────────────────


def test_a_sync_request_is_a_named_lease_held_only_while_it_is_out():
    registry = ActivityRegistry()
    seen: list[Any] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.extend(registry.busy())
        return httpx.Response(200, json={})

    with BusyClient(
        busy_holder="svc",
        busy_kind=BusyKind.MODEL_CALL,
        busy_registry=registry,
        transport=httpx.MockTransport(answer),
    ) as client:
        client.get("https://sync.example.test/x")
    [held] = seen
    assert (held.kind, held.holder, held.reason) == (
        BusyKind.MODEL_CALL,
        "svc",
        "GET sync.example.test",
    )
    assert registry.busy() == []


def test_a_sync_streamed_answer_holds_a_renewed_lease_until_closed_and_passes_chunks():
    clock = Clock()
    registry = ActivityRegistry(clock=clock)
    body = SyncChunks()
    client = BusyClient(
        busy_holder="model",
        busy_registry=registry,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=sync_stream(body))),
    )
    with client:
        response = client.send(client.build_request("GET", "https://m.test/"), stream=True)
        [held] = registry.busy()
        assert held.expires_at == clock.now + timedelta(seconds=900)
        chunks = response.iter_raw()
        assert next(chunks) == b"a"
        assert registry.is_busy()
        assert list(chunks) == [b"b", b"c"]
        assert registry.busy() == [] and body.closed == 1


def test_a_sync_stream_whose_inner_close_raises_still_lets_go():
    registry = ActivityRegistry()
    client = BusyClient(
        busy_holder="model",
        busy_registry=registry,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=sync_stream(SyncChunks(fail_on_close=True)))
        ),
    )
    with client:
        response = client.send(client.build_request("GET", "https://m.test/"), stream=True)
        with pytest.raises(OSError, match="close failed"):
            response.close()
    assert registry.busy() == []


def test_a_sync_already_closed_streamed_answer_lets_go_at_once():
    registry = ActivityRegistry()
    client = BusyClient(
        busy_holder="svc",
        busy_registry=registry,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x")),
    )
    with client:
        client.send(client.build_request("GET", "https://h.test/"), stream=True)
        assert registry.busy() == []


# ── renewed (httpx2) ──────────────────────────────────────────────


def test_renewed_keeps_holder_kind_registry_auth_and_timeout_and_converts_limits():
    registry = ActivityRegistry()
    auth = httpx2.BasicAuth("u", "p")
    client = http2_module.BusyAsyncClient(
        busy_holder="claude",
        busy_kind=BusyKind.MODEL_CALL,
        busy_registry=registry,
        auth=auth,
        timeout=httpx2.Timeout(7.0),
    )
    fresh = client.renewed(
        limits=httpx.Limits(max_connections=5, max_keepalive_connections=2, keepalive_expiry=9.0)
    )
    assert fresh is not client and isinstance(fresh, http2_module.BusyAsyncClient)
    assert (fresh._busy.holder, fresh._busy.kind, fresh._busy.registry) == (
        "claude",
        BusyKind.MODEL_CALL,
        registry,
    )
    assert fresh.auth is not None and fresh.timeout == httpx2.Timeout(7.0)
    pool = fresh._transport._pool
    assert (pool._max_connections, pool._max_keepalive_connections, pool._keepalive_expiry) == (
        5,
        2,
        9.0,
    )


def test_renewed_accepts_limits_with_unlimited_fields():
    client = http2_module.BusyAsyncClient(busy_holder="claude")
    fresh = client.renewed(limits=httpx2.Limits(max_connections=None, keepalive_expiry=None))
    pool = fresh._transport._pool
    assert pool._keepalive_expiry is None


def test_a_sync_failed_send_lets_go_and_raises():
    registry = ActivityRegistry()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with (
        BusyClient(
            busy_holder="svc", busy_registry=registry, transport=httpx.MockTransport(refuse)
        ) as client,
        pytest.raises(httpx.ConnectError),
    ):
        client.get("https://h.test/")
    assert registry.busy() == []
