"""Approvals waiting on the person, written as they happen.

An approval used to live only in the backend's memory — the main
agent's paused requirements in ``PendingRequirementsStore``, a
sub-agent's in ``SubAgentHITLCoordinator`` — so a backend that
restarted while a dialog was open lost the question: the window kept
showing it, and answering it said "Unknown requirement". In the cloud a
producer's VM is a cache that can be lost at any time (``docs/CLOUD.md``
section 9), which makes that the common case rather than a rare one.

:class:`ApprovalJournal` keeps one row per approval in the project's
database (the desktop's ``state.db``, the shared PostgreSQL database in
the cloud, scoped like every other row): the Agno requirement as Agno
serialises it, and the request the window was shown. A row is written
when the approval is raised and deleted when it is answered or its run
ends; a backend that starts reads them back, so the approval is shown
again and can be answered.

The requirements the permission policy decided by itself while others
in the same pause waited on the person (``auto_resolved``) are kept
too: the run cannot be resumed without them.

A sub-agent's approval has more to keep, in the journal's
:attr:`~ApprovalJournal.spawns` (:mod:`.spawn_links`) and
:attr:`~ApprovalJournal.turns` (:mod:`.turn_results`).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement
from pydantic import ValidationError
from sqlalchemy import ColumnElement, delete, select, update
from sqlalchemy.orm import Session

from igni_core.core.approvals.events import ApprovalChange, ApprovalState, approval_events
from igni_core.core.approvals.journal_table import JournalTable, JournalWrite
from igni_core.core.approvals.records import ApprovalOrigin, StoredApproval
from igni_core.core.approvals.spawn_links import SpawnLinks
from igni_core.core.approvals.tables import WaitingApprovalModel
from igni_core.core.approvals.turn_results import TurnResults
from igni_core.core.db.database import Database
from igni_core.protocol import messages as msg

logger = logging.getLogger(__name__)


class ApprovalJournal(JournalTable):
    """The ``waiting_approvals`` table of one project's database, with
    the sub-agent runs (:attr:`spawns`) and call results (:attr:`turns`)
    kept beside it.

    Every write is best-effort (:class:`JournalWrite`): a failed one is
    logged and the approval still works in memory — it just would not
    survive a restart.
    """

    def __init__(self, db: Database | str | Path) -> None:
        super().__init__(Database.of(db))
        self.spawns = SpawnLinks(self._db)
        self.turns = TurnResults(self._db)

    def record(
        self,
        *,
        requirement_id: str,
        session_id: str,
        run_id: str | None,
        origin: ApprovalOrigin,
        requirement: RunRequirement,
        request: msg.HITLRequest | None = None,
    ) -> JournalWrite:
        serialised = json.dumps(requirement.to_dict(), default=str)

        def change(session: Session) -> None:
            session.execute(
                delete(WaitingApprovalModel).where(
                    self._db.scope.where(WaitingApprovalModel),
                    WaitingApprovalModel.requirement_id == requirement_id,
                )
            )
            session.add(
                WaitingApprovalModel(
                    **self._db.scope.stamp(),
                    requirement_id=requirement_id,
                    session_id=session_id,
                    run_id=run_id or "",
                    origin=origin.value,
                    requirement=serialised,
                    request=request.model_dump_json() if request is not None else "",
                    created_at=int(time.time()),
                )
            )

        written = self._write(f"record {requirement_id}", change)
        if origin.asks_a_person:
            approval_events.announce(
                ApprovalChange(
                    session_id=session_id,
                    requirement_id=requirement_id,
                    run_id=run_id or "",
                    state=ApprovalState.WAITING,
                    origin=origin.value,
                    tool_name=request.tool_name if request is not None else "",
                )
            )
        return written

    def record_continuing(self, *, session_id: str, run_id: str) -> JournalWrite:
        """The main agent's run ``run_id`` is being continued after its
        approvals were answered — until :meth:`forget_continuing`."""
        return self.record(
            requirement_id=f"continuing-{run_id}",
            session_id=session_id,
            run_id=run_id,
            origin=ApprovalOrigin.CONTINUING,
            requirement=RunRequirement(tool_execution=ToolExecution(tool_name="continue")),
        )

    def forget_continuing(self, run_id: str) -> JournalWrite:
        return self.forget(f"continuing-{run_id}")

    def record_auto_resolved(
        self, *, session_id: str, run_id: str, requirement: RunRequirement
    ) -> JournalWrite:
        return self.record(
            requirement_id=f"auto-{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            run_id=run_id,
            origin=ApprovalOrigin.AUTO_RESOLVED,
            requirement=requirement,
        )

    def update_request(self, requirement_id: str, request: msg.HITLRequest) -> JournalWrite:
        """What the window is shown changed (a better explanation)."""
        return self._write(
            f"update {requirement_id}",
            lambda session: session.execute(
                update(WaitingApprovalModel)
                .where(
                    self._db.scope.where(WaitingApprovalModel),
                    WaitingApprovalModel.requirement_id == requirement_id,
                )
                .values(request=request.model_dump_json())
            ),
        )

    def forget(self, requirement_id: str) -> JournalWrite:
        """Answered: the approval is no longer waiting."""
        return self._delete(WaitingApprovalModel.requirement_id == requirement_id)

    def forget_run(self, run_id: str, *, origin: ApprovalOrigin | None = None) -> JournalWrite:
        """The run ended or resumed: nothing of it is waiting any more."""
        if origin is None:
            return self._delete(WaitingApprovalModel.run_id == run_id)
        return self._delete(
            WaitingApprovalModel.run_id == run_id,
            WaitingApprovalModel.origin == origin.value,
        )

    def waiting(self, session_id: str) -> list[StoredApproval]:
        """Everything the session's approvals were, oldest first."""
        rows: list[WaitingApprovalModel] = self._read(
            f"read for {session_id}",
            lambda session: list(
                session.scalars(
                    select(WaitingApprovalModel)
                    .where(
                        self._db.scope.where(WaitingApprovalModel),
                        WaitingApprovalModel.session_id == session_id,
                    )
                    .order_by(WaitingApprovalModel.created_at, WaitingApprovalModel.requirement_id)
                ).all()
            ),
            [],
        )
        return [stored for row in rows if (stored := self._stored(row)) is not None]

    def _delete(self, *conditions: ColumnElement[bool]) -> JournalWrite:
        mine = (self._db.scope.where(WaitingApprovalModel), *conditions)
        gone: list[tuple[str, str, str, str]] = []

        def change(session: Session) -> None:
            gone.extend(
                session.execute(
                    select(
                        WaitingApprovalModel.requirement_id,
                        WaitingApprovalModel.session_id,
                        WaitingApprovalModel.run_id,
                        WaitingApprovalModel.origin,
                    ).where(*mine)
                ).all()
            )
            session.execute(delete(WaitingApprovalModel).where(*mine))

        written = self._write("delete", change)
        if not written.ok:
            return written
        asks_a_person = {o.value for o in ApprovalOrigin if o.asks_a_person}
        for requirement_id, session_id, run_id, origin in gone:
            if origin not in asks_a_person:
                continue
            approval_events.announce(
                ApprovalChange(
                    session_id=session_id,
                    requirement_id=requirement_id,
                    run_id=run_id,
                    state=ApprovalState.RESOLVED,
                    origin=origin,
                )
            )
        return written

    @staticmethod
    def _stored(row: WaitingApprovalModel) -> StoredApproval | None:
        try:
            requirement = RunRequirement.from_dict(json.loads(row.requirement))
            request = msg.HITLRequest.model_validate_json(row.request) if row.request else None
            origin = ApprovalOrigin(row.origin)
        except (ValueError, KeyError, TypeError, AttributeError, ValidationError) as exc:
            logger.warning("approval journal: unreadable row %s: %s", row.requirement_id, exc)
            return None
        return StoredApproval(
            requirement_id=row.requirement_id,
            session_id=row.session_id,
            run_id=row.run_id,
            origin=origin,
            requirement=requirement,
            request=request,
            agent_path=list(request.agent_path) if request is not None else [],
        )


__all__ = ["ApprovalJournal"]
