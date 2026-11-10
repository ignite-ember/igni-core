"""``turn_results``: the finished calls of a turn whose spawn waits on a
person, so a backend that restarts can continue the turn with every
call's result."""

from __future__ import annotations

import time
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from igni_core.core.approvals.journal_table import JournalTable, JournalWrite
from igni_core.core.approvals.tables import TurnResultModel


class TurnResults(JournalTable):
    """The kept call results of one project's database."""

    def record(self, run_id: str, tool_call_id: str, result: str) -> JournalWrite:
        def change(session: Session) -> None:
            session.execute(
                delete(TurnResultModel).where(
                    self._db.scope.where(TurnResultModel),
                    TurnResultModel.run_id == run_id,
                    TurnResultModel.tool_call_id == tool_call_id,
                )
            )
            session.add(
                TurnResultModel(
                    **self._db.scope.stamp(),
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                    result=result,
                    created_at=int(time.time()),
                )
            )

        return self._write(f"turn result {tool_call_id}", change)

    def of_run(self, run_id: str) -> dict[str, str]:
        """``tool_call_id → result`` of the calls of ``run_id`` that finished."""
        rows: Sequence[tuple[str, str]] = self._read(
            f"read turn {run_id}",
            lambda session: session.execute(
                select(TurnResultModel.tool_call_id, TurnResultModel.result).where(
                    self._db.scope.where(TurnResultModel), TurnResultModel.run_id == run_id
                )
            ).all(),
            [],
        )
        return {call_id: result for call_id, result in rows}

    def forget(self, run_id: str) -> JournalWrite:
        return self._write(
            f"forget turn {run_id}",
            lambda session: session.execute(
                delete(TurnResultModel).where(
                    self._db.scope.where(TurnResultModel), TurnResultModel.run_id == run_id
                )
            ),
        )


__all__ = ["TurnResults"]
