"""The live main team and the ids its stored conversation is filed under.

Compaction, the ``/ctx`` breakdown and the fallback summariser all read
the same three things: the team (rebuilt on ``/model``, plugin reload and
compaction itself, so it is looked up on every call), and the session and
user ids Agno keys the stored conversation on (``/clear`` rotates the
first). This class is what they are handed instead of the session.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class TeamSessionSource:
    """Reads the main team's stored conversation for the live session id."""

    def __init__(
        self,
        team_ref: Callable[[], Any],
        session_id_ref: Callable[[], str],
        user_id_ref: Callable[[], str],
    ) -> None:
        self._team_ref = team_ref
        self._session_id_ref = session_id_ref
        self._user_id_ref = user_id_ref

    @property
    def team(self) -> Any:
        """The main team as it is now."""
        return self._team_ref()

    @property
    def session_id(self) -> str:
        """The live session id."""
        return self._session_id_ref()

    async def load(self) -> Any:
        """The team's stored conversation for this session, or ``None``."""
        return await self.team.aget_session(
            session_id=self._session_id_ref(),
            user_id=self._user_id_ref(),
        )
