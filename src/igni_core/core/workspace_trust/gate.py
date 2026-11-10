"""The trust service a consumer asks.

A loader that gates a folder's own code needs a trust service, and
usually the one this process uses. Handing it one explicitly is how a
caller says "ask this one instead" — a test with its own store, a run
with its own policy. This class holds that choice: the service it was
given, or the process's.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.core.workspace_trust.process import process_trust
from igni_core.core.workspace_trust.service import WorkspaceTrust


class TrustGate:
    """The trust service a consumer asks, or the one this process uses."""

    def __init__(self, trust: WorkspaceTrust | None = None) -> None:
        self._trust = trust

    def service(self) -> WorkspaceTrust:
        """The service to ask — the one it was given, or the process's."""
        return self._trust if self._trust is not None else process_trust()

    def is_trusted(self, project_dir: str | Path, *, what: str = "") -> bool:
        """Whether ``project_dir`` may run its own code."""
        return self.service().is_trusted(project_dir, what=what)


__all__ = ["TrustGate"]
