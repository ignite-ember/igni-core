"""Run one non-interactive session to completion, then actually exit.

``igni -m`` and ``igni -p`` run a whole session inside this process and have
no teardown of their own: the ``ShutdownPipeline`` that closes shells, MCP,
LSP and monitors is reached only from ``backend/server.py``. Measured before
this existed, sessions whose every model call had finished in seconds sat
until an external timeout, and without one, indefinitely. The work was done;
the process could not get through interpreter shutdown.

Two things keep it from hanging, both on :class:`HeadlessRun`:

* :meth:`HeadlessRun.drain_after` cancels whatever the session left running
  (auto-backgrounded shell commands, ``background: true`` hooks) inside the
  loop, with a deadline, so none of it reaches ``asyncio.run``'s own close
  path, which gathers leftovers with no timeout.
* :meth:`HeadlessRun.run` skips interpreter shutdown when non-daemon threads
  survive, because the interpreter joins those with no timeout.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import traceback
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

#: A session coroutine, as ``run_single_message`` returns it.
SessionCoroutine = Coroutine[Any, Any, None]


class HeadlessRun:
    """Runs a session coroutine and exits with its status code."""

    def __init__(self, drain_timeout_seconds: float = 5.0) -> None:
        #: How long :meth:`drain_after` waits for leftover tasks before
        #: abandoning them.
        self._drain_timeout_seconds = drain_timeout_seconds

    async def drain_after(self, session: SessionCoroutine) -> None:
        """Await ``session``, then cancel and drain whatever it left behind.

        The wait is bounded on purpose: a task that ignores cancellation
        must not be able to hold the process open.
        """
        try:
            await session
        finally:
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.wait(pending, timeout=self._drain_timeout_seconds)

    def run(self, session: SessionCoroutine, *, cleanup: Callable[[], None]) -> None:
        """Run ``session``, call ``cleanup``, flush, and exit with its code.

        ``cleanup`` failing is logged, never raised: it must not keep the
        process alive.
        """
        code = 0
        try:
            asyncio.run(self.drain_after(session))
        except SystemExit as exc:  # a hook or handler asked for a specific code
            code = int(exc.code or 0)
        except BaseException:  # noqa: BLE001 — report, then still exit cleanly
            traceback.print_exc()
            code = 1

        try:
            cleanup()
        except Exception:  # noqa: BLE001 — cleanup must not keep the process alive
            logger.debug("cleanup failed on exit", exc_info=True)

        sys.stdout.flush()
        sys.stderr.flush()

        lingering = self._lingering_threads()
        if lingering:
            # The interpreter would join these with no timeout; the work is
            # finished and flushed, so leave without waiting for them.
            logger.debug("hard-exiting past %d non-daemon thread(s)", len(lingering))
            os._exit(code)
        if code:
            raise SystemExit(code)

    @staticmethod
    def _lingering_threads() -> list[threading.Thread]:
        """Non-daemon threads other than main: the ones a normal exit joins
        forever, most often ``executor.shutdown(wait=True)``'s
        ``_do_shutdown``. With none, the normal exit is safe, which also keeps
        the in-process CLI tests from being killed mid-suite."""
        return [
            thread
            for thread in threading.enumerate()
            if thread is not threading.main_thread() and not thread.daemon
        ]
