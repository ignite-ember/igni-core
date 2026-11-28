"""One row of ``ember_sessions_runs``: a run Agno saved."""

from __future__ import annotations

from pydantic import BaseModel


class PersistedRun(BaseModel):
    """A saved run's id, its parent, its status and its input."""

    run_id: str
    parent_run_id: str | None = None
    status: str = ""
    input_text: str = ""
    input_tokens: int = 0
