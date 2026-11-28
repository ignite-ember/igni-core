"""What the backend wrote to the project's ``state.db``, read back without touching it."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tests.session_lifecycle.schemas.persisted_message import PersistedMessage
from tests.session_lifecycle.schemas.persisted_run import PersistedRun


class StateDb:
    """Read-only queries over one ``state.db``."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _rows(self, sql: str, args: tuple[object, ...]) -> list[tuple[object, ...]]:
        if not self.path.exists():
            return []
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def received(self, session_id: str) -> list[PersistedMessage]:
        rows = self._rows(
            "SELECT message_id, text, status, interrupted_reason FROM ember_received_messages "
            "WHERE session_id = ? ORDER BY received_at, rowid",
            (session_id,),
        )
        return [
            PersistedMessage(
                message_id=str(r[0]),
                text=str(r[1]),
                status=str(r[2]),
                interrupted_reason=r[3],  # type: ignore[arg-type]
            )
            for r in rows
        ]

    def waiting_approvals(self, session_id: str) -> list[str]:
        """The approvals that wait on a person (the main agent's and sub-agents')."""
        rows = self._rows(
            "SELECT requirement_id FROM waiting_approvals "
            "WHERE session_id = ? AND origin IN ('main', 'sub_agent')",
            (session_id,),
        )
        return [str(r[0]) for r in rows]

    def journal_rows(self, session_id: str) -> list[str]:
        """Every row the approvals journal holds for the session, as ``origin:id``."""
        rows = self._rows(
            "SELECT origin, requirement_id FROM waiting_approvals WHERE session_id = ?",
            (session_id,),
        )
        return [f"{r[0]}:{r[1]}" for r in rows]

    def runs(self, session_id: str) -> list[PersistedRun]:
        rows = self._rows(
            "SELECT run_id, parent_run_id, status, run_data FROM ember_sessions_runs "
            "WHERE session_id = ? ORDER BY created_at, run_index",
            (session_id,),
        )
        runs: list[PersistedRun] = []
        for run_id, parent, status, data in rows:
            try:
                parsed = json.loads(str(data))
            except ValueError:
                parsed = {}
            metrics = parsed.get("metrics") or {}
            runs.append(
                PersistedRun(
                    run_id=str(run_id),
                    parent_run_id=str(parent) if parent else None,
                    status=str(status or ""),
                    input_text=str((parsed.get("input") or {}).get("input_content") or ""),
                    input_tokens=int(metrics.get("input_tokens") or 0),
                )
            )
        return runs

    def session_input_tokens(self, session_id: str) -> int | None:
        """``session_data.session_metrics.input_tokens``: the session's total, as Agno keeps it."""
        rows = self._rows(
            "SELECT session_data FROM ember_sessions WHERE session_id = ?", (session_id,)
        )
        if not rows:
            return None
        try:
            data = json.loads(str(rows[0][0] or "{}"))
        except ValueError:
            return None
        metrics = (data or {}).get("session_metrics")
        if not isinstance(metrics, dict):
            return None
        return int(metrics.get("input_tokens") or 0)
