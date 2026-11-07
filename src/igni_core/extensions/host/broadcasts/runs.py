"""Each session's current top-level run, and the files it has edited."""

from __future__ import annotations

from igni_core.protocol import messages as msg


class RunLedger:
    """The last top-level run of each session, and what its current one touched."""

    def __init__(self) -> None:
        self._last_run: dict[str, str] = {}
        self._files: dict[str, set[str]] = {}

    def started(self, m: msg.RunStarted) -> None:
        """A sub-agent's run belongs to its parent's: only a top-level one resets."""
        if not m.parent_run_id:
            self._last_run[m.session_id] = m.run_id
            self._files[m.session_id] = set()

    def finished(self, session_id: str) -> frozenset[str]:
        """The files the session's top-level run edited; forgotten as they are given."""
        return frozenset(self._files.pop(session_id, set()))

    def dropped(self, session_id: str) -> None:
        self._files.pop(session_id, None)

    def edited(self, session_id: str, relative: str) -> None:
        self._files.setdefault(session_id, set()).add(relative)

    def last_run_of(self, session_id: str) -> str:
        return self._last_run.get(session_id, "")


__all__ = ["RunLedger"]
