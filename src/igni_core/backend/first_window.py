"""Waiting for the backend's first window, or for shutdown, whichever comes first."""

from __future__ import annotations

import asyncio
import contextlib
from enum import Enum

from igni_core.backend.host_backend import ObservedTransport
from igni_core.backend.supervisor import BackendSupervisor
from igni_core.core.activity import background


class FirstWindowOutcome(Enum):
    CONNECTED = "connected"
    SHUTDOWN = "shutdown"


class FirstWindow:
    """The first client's arrival, raced against a shutdown request.

    Raced because closing the transport, which is all a shutdown does to
    it, does not wake a wait for the first client.
    """

    def __init__(
        self, *, transport: ObservedTransport, supervisor: BackendSupervisor, no_timeout: bool
    ) -> None:
        self._transport = transport
        self._supervisor = supervisor
        # GUI clients may open long after the backend is ready and reload at
        # will, so on WebSocket there is no timeout: shutdown comes from
        # signals, the parent watchdog, or an explicit Shutdown message.
        self._no_timeout = no_timeout

    async def arrives(self) -> FirstWindowOutcome:
        if self._no_timeout:
            connecting = self._transport.wait_for_connection(timeout=None)
        else:
            connecting = self._transport.wait_for_connection()
        # Daemons, not busy work: a backend waiting for its first window is
        # idle, and a layer that suspends idle processes may suspend it.
        connected = background.daemon(connecting, name="first-window")
        stopping = background.daemon(self._supervisor.wait_for_shutdown(), name="first-window-stop")
        try:
            await asyncio.wait({connected, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (connected, stopping):
                if not waiter.done():
                    waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await waiter
        if stopping.done() and not stopping.cancelled():
            return FirstWindowOutcome.SHUTDOWN
        connected.result()  # a timed-out wait raises here
        return FirstWindowOutcome.CONNECTED
