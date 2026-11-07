"""Values a change implies but does not carry: read again, then told.

A sign-in, an output style, a loop write says *that* the account, the
display or the loop moved, not what they are now. Each is read again in
the background and told — and only when an extension listens, so a
change nobody hears costs nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from igni_core.extensions.host.broadcasts.session_values import SessionValueSource
from igni_core.extensions.host.display import Display
from igni_core.extensions.host.signal_values import Emitter

ACCOUNT_CHANGED = "account.changed"
DISPLAY_CHANGED = Display.WIRE
LOOP_CHANGED = "loop.changed"


class DeferredReads:
    """Reads a value again in the background and tells it, when anyone listens."""

    def __init__(
        self,
        wanted: Callable[[str], bool],
        spawn: Callable[[Coroutine[Any, Any, None]], None],
        emitter: Emitter,
        values: SessionValueSource,
    ) -> None:
        self._wanted = wanted
        self._spawn = spawn
        self._emitter = emitter
        self._values = values

    def refresh(self, wire: str, read: Callable[[], Awaitable[Any]]) -> None:
        if not self._wanted(wire):
            return

        async def tell() -> None:
            self._emitter.value(wire, await read())

        self._spawn(tell())

    def account(self, session_id: str) -> None:
        self.refresh(ACCOUNT_CHANGED, lambda: self._values.account(self._or_default(session_id)))

    def display(self, session_id: str) -> None:
        self.refresh(DISPLAY_CHANGED, lambda: self._values.display(self._or_default(session_id)))

    def loop(self, session_id: str) -> None:
        self.refresh(LOOP_CHANGED, lambda: self._values.loop(session_id))

    def _or_default(self, session_id: str) -> str:
        return session_id or self._values.default_session()


__all__ = ["ACCOUNT_CHANGED", "DISPLAY_CHANGED", "LOOP_CHANGED", "DeferredReads"]
