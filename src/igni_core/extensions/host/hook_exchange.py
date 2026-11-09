"""The exchange of a hook frame and the answer it owes.

An extension's process answers a hook event over the socket it connected
on (:mod:`.channel`). This module owns that exchange: the processes that
are connected, the frames that went out, and the futures their answers
are awaited on. What an answer *means* — whether the event goes ahead,
and with what — is :mod:`igni_core.extensions.host.answers`.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Callable

from igni_core.extensions.host.wire import HookAnswer

logger = logging.getLogger(__name__)


class HookFailure(Exception):
    """An extension that did not give an answer the event can take.

    ``unanswered`` when its process said nothing — it timed out, crashed,
    went away, or its handler raised — which is what makes an extension
    faulty (:class:`~igni_core.extensions.faults.ExtensionFaults`). An
    answer the event cannot take is the extension's bug, not a fault.
    """

    def __init__(self, message: str, *, unanswered: bool = False) -> None:
        super().__init__(message)
        self.unanswered = unanswered


class HookConnections:
    """The connected extension processes, and the hook answers they owe.

    One instance per process: the senders, the events that say a process
    is connected, and the futures a frame's answer is awaited on. The
    coordinator (:class:`~igni_core.extensions.host.hooks.ExtensionHooks`)
    asks it to send a frame and awaits the answer through it.
    """

    def __init__(self) -> None:
        self._senders: dict[str, Callable[[str], None]] = {}
        self._connected: dict[str, asyncio.Event] = {}
        self._waiting: dict[str, tuple[str, asyncio.Future[HookAnswer]]] = {}
        self._ids = itertools.count(1)

    # ── Who is connected ───────────────────────────────────────────

    def connect(self, extension_id: str, send: Callable[[str], None]) -> None:
        """An extension's process connected: ``send`` puts a frame on its socket."""
        self._senders[extension_id] = send
        self._connected.setdefault(extension_id, asyncio.Event()).set()

    def disconnect(self, extension_id: str, send: Callable[[str], None] | None = None) -> None:
        """Its process went away. Whatever it owed an answer to has failed."""
        if send is not None and self._senders.get(extension_id) != send:
            return
        self._senders.pop(extension_id, None)
        event = self._connected.get(extension_id)
        if event is not None:
            event.clear()
        for hook_id, (owner, future) in list(self._waiting.items()):
            if owner == extension_id and not future.done():
                future.set_exception(HookFailure("its process went away", unanswered=True))
                self._waiting.pop(hook_id, None)

    def connected(self, extension_id: str) -> bool:
        """Whether its process is connected."""
        return extension_id in self._senders

    # ── What goes out ──────────────────────────────────────────────

    def send(self, extension_id: str, frame: str) -> bool:
        """Put a frame on its socket. ``False`` when it is not connected."""
        sender = self._senders.get(extension_id)
        if sender is None:
            return False
        sender(frame)
        return True

    def next_hook_id(self) -> str:
        """The id a frame carries, so that its answer can be matched to it."""
        return f"h-{next(self._ids)}"

    # ── What comes back ────────────────────────────────────────────

    def answered(self, extension_id: str, answer: HookAnswer) -> bool:
        """An answer arrived. ``False`` when it answers nothing this extension was asked."""
        waiting = self._waiting.get(answer.hook)
        if waiting is None or waiting[0] != extension_id:
            return False
        future = waiting[1]
        if not future.done():
            future.set_result(answer)
        return True

    async def await_answer(
        self,
        extension_id: str,
        hook_id: str,
        frame: str,
        *,
        timeout: float,
        connect_timeout: float | None = None,
    ) -> HookAnswer:
        """Send the frame and wait for the answer it owes.

        A process that has not connected yet is waited for at most
        ``connect_timeout``; the answer is awaited for at most
        ``timeout``, the wait for the connection included. Raises
        :class:`HookFailure` when there is no answer.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        connected = self._connected.setdefault(extension_id, asyncio.Event())
        if extension_id not in self._senders:
            wait = timeout if connect_timeout is None else min(timeout, connect_timeout)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(connected.wait(), wait)
        sender = self._senders.get(extension_id)
        if sender is None:
            raise HookFailure("its process is not connected", unanswered=True)
        future: asyncio.Future[HookAnswer] = loop.create_future()
        self._waiting[hook_id] = (extension_id, future)
        try:
            sender(frame)
            remaining = max(0.0, deadline - loop.time())
            return await asyncio.wait_for(future, remaining)
        except TimeoutError as exc:
            raise HookFailure(f"no answer within {timeout:g}s", unanswered=True) from exc
        finally:
            self._waiting.pop(hook_id, None)


__all__ = ["HookConnections", "HookFailure"]
