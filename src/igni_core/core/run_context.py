"""The run the current work belongs to, for what it sends while it runs.

Set around a run igni starts with an id it chose (a scheduled task run
now); read where a push is made, so the push says which run it came
from. Pushes made outside such a run, or from a thread the run did not
start (a background process's reader), carry none.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

CURRENT_RUN_ID: ContextVar[str] = ContextVar("igni_current_run_id", default="")

#: The chat the current run belongs to, set for the run's whole life. A
#: push made while it runs is that chat's, even once New chat or a sidebar
#: switch has moved the session on to another id.
CURRENT_RUN_SESSION_ID: ContextVar[str] = ContextVar("igni_current_run_session_id", default="")


@contextlib.contextmanager
def run_session(session_id: str) -> Iterator[None]:
    """What runs inside belongs to ``session_id``'s chat."""
    token = CURRENT_RUN_SESSION_ID.set(session_id)
    try:
        yield
    finally:
        # A generator closed from another context cannot reset its token;
        # the context it was set in is gone with it then.
        with contextlib.suppress(ValueError):
            CURRENT_RUN_SESSION_ID.reset(token)


__all__ = ["CURRENT_RUN_ID", "CURRENT_RUN_SESSION_ID", "run_session"]
