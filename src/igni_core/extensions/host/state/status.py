"""The last status each session reported."""

from __future__ import annotations

from igni_core.protocol import messages as msg


class StatusMemory:
    """The last status seen, by session."""

    def __init__(self) -> None:
        self._by_session: dict[str, msg.StatusUpdate] = {}

    def remember(self, status: msg.StatusUpdate) -> msg.StatusUpdate | None:
        """Keep the session's new status; the one it replaces, if any."""
        previous = self._by_session.get(status.session_id)
        self._by_session[status.session_id] = status
        return previous


__all__ = ["StatusMemory"]
