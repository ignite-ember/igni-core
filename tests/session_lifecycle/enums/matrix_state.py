"""The states a session is put in before the action under test."""

from __future__ import annotations

from enum import StrEnum


class MatrixState(StrEnum):
    """Where the session is when the person acts."""

    IDLE = "idle"
    STREAMING = "streaming"
    TOOL = "tool"
    PAUSED = "paused"
    CONTINUING = "continuing"
    SUBAGENT = "subagent"
    BACKGROUND = "background"
    ERRORED = "errored"
    CANCELLED = "cancelled"
    RESTARTED = "restarted"
