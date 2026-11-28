"""One approval the client was asked and has not answered."""

from __future__ import annotations

from pydantic import BaseModel


class PendingApproval(BaseModel):
    """An approval request as the client holds it: its id, its tool and the run it pauses."""

    requirement_id: str
    tool_name: str = ""
    run_id: str = ""
