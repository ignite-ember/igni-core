"""Pending-reminder queue for asyncRewake hooks.

The queue of texts fired by background ``asyncRewake`` hooks
(exit-code 2), drained on the next turn.

Composed once by :class:`Session` as ``session.reminders``;
:meth:`queue` and :meth:`drain` are handed to :class:`HookExecutor`'s
``rewake_callback`` and :class:`SessionMessageHandler`'s
``pending_reminders_drain`` so neither touches the buffer directly.
"""

from __future__ import annotations


class PendingReminderQueue:
    """FIFO buffer of pending reminder texts.

    asyncio is single-threaded — no lock needed. Callers should
    treat instances as private state on the Session; the queue is
    drained one-shot on every :meth:`SessionMessageHandler.handle`
    turn and does not persist across restarts.
    """

    def __init__(self) -> None:
        self._reminders: list[str] = []

    def queue(self, text: str) -> None:
        """Push ``text`` onto the buffer. Empty strings are ignored
        so a hook that exits with code 2 but writes nothing to
        stderr doesn't inject a blank system reminder."""
        if not text:
            return
        self._reminders.append(text)

    def drain(self) -> list[str]:
        """Return a snapshot of the buffer and clear it in one shot.

        The one-shot semantics match the pre-refactor Session
        behaviour: two consecutive calls to
        :meth:`SessionMessageHandler.handle` see disjoint reminder
        lists.
        """
        drained = list(self._reminders)
        self._reminders.clear()
        return drained

    @property
    def pending(self) -> list[str]:
        """Read-only view of the current buffer; production callers use
        :meth:`drain`."""
        return self._reminders

    def replace(self, reminders: list[str]) -> None:
        """Replace the buffer wholesale.

        Used by :class:`SessionHooks` whenever it rebuilds the executor,
        so a stray reminder from the previous one can't leak into the
        new one.
        """
        self._reminders = list(reminders)
