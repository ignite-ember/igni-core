"""The run the current work belongs to, for what it sends while it runs.

Set around a run igni starts with an id it chose (a scheduled task run
now); read where a push is made, so the push says which run it came
from. Pushes made outside such a run, or from a thread the run did not
start (a background process's reader), carry none.
"""

from __future__ import annotations

from contextvars import ContextVar

CURRENT_RUN_ID: ContextVar[str] = ContextVar("igni_current_run_id", default="")

__all__ = ["CURRENT_RUN_ID"]
