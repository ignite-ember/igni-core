"""The two modes a spawn can be in.

A sub-team runs in one of Agno's four team modes; a single agent runs
in one of two isolation modes. The two have deliberately different
policies for a value nobody recognises, and the names say which is
which: a team mode falls back to the default, an isolation mode
refuses.

Both live here, apart from the spawn machinery, because both the
sandbox (which isolation is in force) and the spawn builders (which
mode a team runs in) need them and neither should have to import the
other.
"""

from __future__ import annotations

from enum import Enum


class TeamMode(str, Enum):
    """The Agno team modes a sub-team spawn understands."""

    ROUTE = "route"
    COORDINATE = "coordinate"
    BROADCAST = "broadcast"
    TASKS = "tasks"

    @classmethod
    def parse(cls, raw: str | None) -> TeamMode:
        """The mode ``raw`` names; the default when it names none.

        An unknown team mode is not a refusal: the caller asked for a
        team, and a coordinate team is what a team is.
        """
        if raw is None:
            return cls.COORDINATE
        try:
            return cls(raw)
        except ValueError:
            return cls.COORDINATE

    @classmethod
    def valid_names(cls) -> list[str]:
        """The mode names, for a message that lists them."""
        return [mode.value for mode in cls]


class IsolationMode(str, Enum):
    """The isolation modes a single-agent spawn understands.

    ``NONE`` is the empty string — no isolation — which is what
    ``spawn_agent`` is called with when the caller wants none.
    ``WORKTREE`` forks a git worktree.
    """

    NONE = ""
    WORKTREE = "worktree"

    @classmethod
    def parse(cls, raw: str | None) -> IsolationMode | None:
        """The mode ``raw`` names, or ``None`` when it names none.

        Unlike a team mode, an unknown isolation mode is an error
        rather than a default: the caller asked for a sandbox and
        would otherwise silently get none.
        """
        if raw is None:
            return cls.NONE
        try:
            return cls(raw)
        except ValueError:
            return None

    @classmethod
    def valid_names(cls) -> list[str]:
        """The mode names, for a message that lists them."""
        return [mode.value for mode in cls if mode.value]

    @classmethod
    def unknown_message(cls, raw: str | None) -> str:
        """The message a spawn answers an unknown mode with."""
        return f"Error: unknown isolation mode {raw!r}. Valid: {cls.valid_names()}."


__all__ = ["IsolationMode", "TeamMode"]
