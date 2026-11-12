"""A backend waiting for its first window leaves when it is told to, or when one comes.

Closing the transport, which is all a shutdown does to it, does not wake
a wait for the first client. A backend that only waited for the client
hung forever on a signal and left its lockfile behind. These pin the race
on its own, with a transport and a supervisor that can be made to answer
in any order.
"""

from __future__ import annotations

import asyncio

import pytest

from igni_core.backend.first_window import FirstWindow, FirstWindowOutcome


class _Transport:
    """A transport whose first-client wait is under the test's control."""

    def __init__(self) -> None:
        self.arrived = asyncio.Event()
        self.timeouts: list[object] = []
        self.failure: BaseException | None = None
        self.cancelled = False

    async def wait_for_connection(self, timeout: float | None = 30.0) -> None:
        self.timeouts.append(timeout)
        try:
            if self.failure is not None:
                raise self.failure
            await self.arrived.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _Supervisor:
    def __init__(self) -> None:
        self.stop = asyncio.Event()
        self.cancelled = False

    async def wait_for_shutdown(self) -> None:
        try:
            await self.stop.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _window(transport: _Transport, supervisor: _Supervisor, *, no_timeout: bool = True):
    return FirstWindow(
        transport=transport,  # type: ignore[arg-type]
        supervisor=supervisor,  # type: ignore[arg-type]
        no_timeout=no_timeout,
    )


@pytest.mark.asyncio
async def test_a_client_arriving_first_is_connected():
    transport, supervisor = _Transport(), _Supervisor()
    waiting = asyncio.ensure_future(_window(transport, supervisor).arrives())
    await asyncio.sleep(0)
    transport.arrived.set()

    assert await asyncio.wait_for(waiting, 2) is FirstWindowOutcome.CONNECTED
    assert supervisor.cancelled, "the shutdown waiter is not left running"


@pytest.mark.asyncio
async def test_a_shutdown_with_nobody_connected_is_shutdown():
    transport, supervisor = _Transport(), _Supervisor()
    waiting = asyncio.ensure_future(_window(transport, supervisor).arrives())
    await asyncio.sleep(0)
    supervisor.stop.set()

    assert await asyncio.wait_for(waiting, 2) is FirstWindowOutcome.SHUTDOWN
    assert transport.cancelled, "the client waiter is not left running"


@pytest.mark.asyncio
async def test_a_shutdown_that_races_a_connection_wins():
    transport, supervisor = _Transport(), _Supervisor()
    transport.arrived.set()
    supervisor.stop.set()

    outcome = await asyncio.wait_for(_window(transport, supervisor).arrives(), 2)

    assert outcome is FirstWindowOutcome.SHUTDOWN


@pytest.mark.asyncio
async def test_a_websocket_waits_for_a_client_without_a_timeout():
    transport, supervisor = _Transport(), _Supervisor()
    transport.arrived.set()
    await _window(transport, supervisor, no_timeout=True).arrives()

    assert transport.timeouts == [None]


@pytest.mark.asyncio
async def test_without_the_flag_the_transport_s_own_timeout_applies():
    transport, supervisor = _Transport(), _Supervisor()
    transport.arrived.set()
    await _window(transport, supervisor, no_timeout=False).arrives()

    assert transport.timeouts == [30.0]


@pytest.mark.asyncio
async def test_a_wait_that_times_out_raises():
    transport, supervisor = _Transport(), _Supervisor()
    transport.failure = TimeoutError("no client in time")

    with pytest.raises(TimeoutError, match="no client in time"):
        await asyncio.wait_for(_window(transport, supervisor, no_timeout=False).arrives(), 2)
    assert supervisor.cancelled


@pytest.mark.asyncio
async def test_a_caller_that_is_cancelled_leaves_neither_waiter_running():
    transport, supervisor = _Transport(), _Supervisor()
    waiting = asyncio.ensure_future(_window(transport, supervisor).arrives())
    await asyncio.sleep(0.01)
    waiting.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert transport.cancelled
    assert supervisor.cancelled
