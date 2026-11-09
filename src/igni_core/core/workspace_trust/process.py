"""The trust service this process uses.

Most of igni runs one process per session, and the trust service belongs
to that process: the settings loader hands it the organisation's policy
each time settings load, the CLI records the person's decisions in it,
and every gated loader asks it. Which instance is the process's is one
fact, so it lives in one place — an object that owns it, rather than a
class attribute every caller reaches for.

The decisions themselves live on the :class:`WorkspaceTrust` instance;
this only holds it, so a test can put another one in place for the
length of a test and take it away afterwards.
"""

from __future__ import annotations

import threading

from igni_core.core.workspace_trust.service import WorkspaceTrust


class TrustServiceHolder:
    """Which trust service this process uses."""

    def __init__(self, service: WorkspaceTrust | None = None) -> None:
        self._service = service
        self._lock = threading.Lock()

    def current(self) -> WorkspaceTrust:
        """The trust service this process uses, made on first use."""
        with self._lock:
            if self._service is None:
                self._service = WorkspaceTrust()
            return self._service

    def replace(self, service: WorkspaceTrust | None) -> None:
        """Put ``service`` in place of the current one; ``None`` clears it.

        Clearing leaves the next caller to make a fresh service, which is
        what a new process would have.
        """
        with self._lock:
            self._service = service


#: The holder this process shares.
trust_service = TrustServiceHolder()


def process_trust() -> WorkspaceTrust:
    """The trust service this process uses."""
    return trust_service.current()


__all__ = ["TrustServiceHolder", "process_trust", "trust_service"]
