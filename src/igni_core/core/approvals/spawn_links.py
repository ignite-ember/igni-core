"""``waiting_spawns``: per paused sub-agent run, which agent it is and
the chain of calls waiting above it
(:class:`~igni_core.core.tools.spawn_context.ParentCall`) — what a
backend that starts cold needs to continue the sub-agent, hand its
answer to the waiting call and continue the run that made it."""

from __future__ import annotations

import json
import logging
import time

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from igni_core.core.approvals.journal_table import JournalTable, JournalWrite
from igni_core.core.approvals.records import SpawnLink
from igni_core.core.approvals.tables import WaitingSpawnModel
from igni_core.core.tools.spawn_context import ParentCall

logger = logging.getLogger(__name__)


class SpawnLinks(JournalTable):
    """The paused sub-agent runs of one project's database."""

    def record(self, link: SpawnLink) -> JournalWrite:
        """Remember a paused sub-agent run (once per run)."""

        def change(session: Session) -> None:
            session.execute(
                delete(WaitingSpawnModel).where(
                    self._db.scope.where(WaitingSpawnModel),
                    WaitingSpawnModel.run_id == link.run_id,
                )
            )
            session.add(
                WaitingSpawnModel(
                    **self._db.scope.stamp(),
                    run_id=link.run_id,
                    session_id=link.session_id,
                    agent_session_id=link.agent_session_id,
                    agent_name=link.agent_name,
                    parents=json.dumps([p.model_dump(mode="json") for p in link.parents]),
                    created_at=int(time.time()),
                )
            )

        return self._write(f"record spawn {link.run_id}", change)

    def get(self, run_id: str) -> SpawnLink | None:
        row = self._read(
            f"read spawn {run_id}",
            lambda session: session.scalar(
                select(WaitingSpawnModel).where(
                    self._db.scope.where(WaitingSpawnModel),
                    WaitingSpawnModel.run_id == run_id,
                )
            ),
            None,
        )
        return None if row is None else self._link(row)

    def of_session(self, session_id: str) -> list[SpawnLink]:
        """Every paused sub-agent run of the session that something waits on."""
        rows: list[WaitingSpawnModel] = self._read(
            f"read spawns of {session_id}",
            lambda session: list(
                session.scalars(
                    select(WaitingSpawnModel).where(
                        self._db.scope.where(WaitingSpawnModel),
                        WaitingSpawnModel.session_id == session_id,
                    )
                ).all()
            ),
            [],
        )
        return [link for row in rows if (link := self._link(row)) is not None]

    def forget(self, run_id: str) -> JournalWrite:
        return self._write(
            f"forget spawn {run_id}",
            lambda session: session.execute(
                delete(WaitingSpawnModel).where(
                    self._db.scope.where(WaitingSpawnModel),
                    WaitingSpawnModel.run_id == run_id,
                )
            ),
        )

    @staticmethod
    def _link(row: WaitingSpawnModel) -> SpawnLink | None:
        try:
            parents = [ParentCall.model_validate(p) for p in json.loads(row.parents)]
        except (ValueError, TypeError, AttributeError, ValidationError) as exc:
            logger.warning("approval journal: unreadable spawn %s: %s", row.run_id, exc)
            return None
        return SpawnLink(
            run_id=row.run_id,
            session_id=row.session_id,
            agent_session_id=row.agent_session_id,
            agent_name=row.agent_name,
            parents=parents,
        )


__all__ = ["SpawnLinks"]
