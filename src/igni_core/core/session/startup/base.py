"""Abstract base for the session-startup phases.

The warmup families under this sub-package (marketplace,
MCP) all share a boot-time
contract:

* each is handed only the collaborators it reads, never the session,
* the background variants must no-op cleanly when no event
  loop is running yet (``asyncio.get_running_loop`` raises),
* transient failures are logged and swallowed so session
  boot doesn't get gated on an offline external dep.

:class:`SessionStartupPhase` factors those three concerns
into a single base class so every subclass focuses on its
own warmup steps instead of duplicating the loop-lookup /
log-and-swallow boilerplate.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine

from igni_core.core.activity import background

logger = logging.getLogger(__name__)


class SessionStartupPhase:
    """Shared behaviour for every session-startup phase.

    Subclasses take their own collaborators in their constructors;
    anything the session reassigns mid-session (``pool``, ``settings``)
    arrives as a zero-arg callable read at call time.

    Subclasses inherit two helpers:

    * :meth:`_schedule_on_loop` — encapsulates the ``try:
      get_running_loop() except RuntimeError: return`` +
      ``loop.create_task`` pattern the three background warmups
      share. Callers pass a zero-arg factory (``lambda: _warmup()``)
      so the coroutine is only constructed when a loop exists.
    * :meth:`_log_swallowed` — uniform DEBUG-level "warmup step
      failed, continuing" logging so every catch-and-continue
      inside a warmup body reads the same shape.
    """

    def _schedule_on_loop(
        self, coro_factory: Callable[[], Coroutine[object, object, None]]
    ) -> bool:
        """Schedule ``coro_factory()`` on the running loop.

        Returns ``True`` when the task was scheduled, ``False``
        when no loop was running (session's caller retries once
        one exists). The coroutine is only constructed on the
        success branch so we don't leak an un-awaited coroutine
        warning in the no-loop early-return case.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        background.start(coro_factory(), holder="startup", reason=type(self).__name__)
        return True

    def _log_swallowed(self, exc: BaseException, action: str) -> None:
        """Uniform DEBUG log for a swallowed warmup-step failure.

        Session boot must not gate on any single warmup step; every
        failure surfaces through this helper so a log-scraping
        tool sees the same pattern (``"<action> failed (<exc>);
        continuing"``) for every phase.
        """
        logger.debug("%s failed (%s); continuing", action, exc)
