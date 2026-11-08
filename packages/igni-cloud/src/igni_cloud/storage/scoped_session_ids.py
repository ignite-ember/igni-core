"""A session's id as the database holds it: the scope, then the plain id.

A session's primary key in Agno's table is its ``session_id`` alone, so
the scope is part of the key: ``<org>/<producer>/<project>#<id>``. Two
producers' sessions with the same id are then two rows. What Agno reads
back is handed on with the plain id.
"""

from __future__ import annotations

from typing import Any

from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.run.workflow import WorkflowRunOutput
from agno.session import Session

AgnoRun = RunOutput | TeamRunOutput | WorkflowRunOutput
#: What Agno reads back when asked not to deserialize.
AgnoRow = dict[str, Any]


class ScopedSessionIds:
    """Turns plain session ids into stored ones and back, for one scope."""

    def __init__(self, scoped_user_id: str) -> None:
        self._prefix = f"{scoped_user_id}#"

    @property
    def prefix(self) -> str:
        return self._prefix

    def stored(self, session_id: str) -> str:
        """The key a session is stored under: the scope, then the id."""
        return f"{self._prefix}{session_id}"

    def plain(self, stored_id: str) -> str:
        return stored_id.removeprefix(self._prefix)

    def plain_row(self, row: AgnoRow) -> AgnoRow:
        session_id = row.get("session_id")
        if not isinstance(session_id, str):
            return row
        return {**row, "session_id": self.plain(session_id)}

    def plain_session(self, session: Session) -> Session:
        session.session_id = self.plain(session.session_id)
        return session

    def plain_run(self, run: AgnoRun) -> AgnoRun:
        if run.session_id is not None:
            run.session_id = self.plain(run.session_id)
        return run

    def plain_found_session(self, found: Session | AgnoRow | None) -> Session | AgnoRow | None:
        if found is None:
            return None
        if isinstance(found, dict):
            return self.plain_row(found)
        return self.plain_session(found)

    def plain_found_run(self, found: AgnoRun | AgnoRow | None) -> AgnoRun | AgnoRow | None:
        if found is None:
            return None
        if isinstance(found, dict):
            return self.plain_row(found)
        return self.plain_run(found)

    def plain_rows_page(self, page: tuple[list[AgnoRow], int]) -> tuple[list[AgnoRow], int]:
        rows, total = page
        return [self.plain_row(row) for row in rows], total


__all__ = ["AgnoRow", "AgnoRun", "ScopedSessionIds"]
