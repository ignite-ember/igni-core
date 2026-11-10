"""The approval journal's three tables, in the project's database.

``waiting_approvals`` holds one row per approval waiting on a person
(or kept for the resume of its run), ``waiting_spawns`` one per paused
sub-agent run with the calls waiting above it, ``turn_results`` the
finished calls of a turn whose spawn waits on a person.
"""

from __future__ import annotations

from sqlalchemy import Index, Integer, PrimaryKeyConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from igni_core.core.db.base import Base
from igni_core.core.db.scope import ScopedColumns


class WaitingApprovalModel(ScopedColumns, Base):
    __tablename__ = "waiting_approvals"
    __table_args__ = (
        PrimaryKeyConstraint(
            "org", "producer", "project", "requirement_id", name="pk_waiting_approvals"
        ),
        Index("ix_waiting_approvals_session", "org", "producer", "project", "session_id"),
    )

    requirement_id: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    origin: Mapped[str] = mapped_column(String, nullable=False)
    #: ``RunRequirement.to_dict()`` as JSON.
    requirement: Mapped[str] = mapped_column(Text, nullable=False)
    #: The :class:`msg.HITLRequest` the window was shown, as JSON; empty
    #: for an auto-resolved requirement, which nobody is shown.
    request: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class WaitingSpawnModel(ScopedColumns, Base):
    """A paused sub-agent run, and the calls waiting above it."""

    __tablename__ = "waiting_spawns"
    __table_args__ = (
        PrimaryKeyConstraint("org", "producer", "project", "run_id", name="pk_waiting_spawns"),
    )

    #: The sub-agent's paused run.
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    #: The igni session the approval belongs to.
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    #: The sub-agent run's own Agno session.
    agent_session_id: Mapped[str] = mapped_column(String, nullable=False)
    agent_name: Mapped[str] = mapped_column(String, nullable=False)
    #: ``list[ParentCall]`` as JSON, nearest first.
    parents: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class TurnResultModel(ScopedColumns, Base):
    """A finished call's result, in a turn whose spawn waits on a person."""

    __tablename__ = "turn_results"
    __table_args__ = (
        PrimaryKeyConstraint(
            "org", "producer", "project", "run_id", "tool_call_id", name="pk_turn_results"
        ),
    )

    run_id: Mapped[str] = mapped_column(String, nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String, nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


__all__ = ["TurnResultModel", "WaitingApprovalModel", "WaitingSpawnModel"]
