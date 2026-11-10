"""What a stream run remembers while it runs.

The two stream handlers — one for a specialist agent, one for a
coordinated team — keep the same kind of state: the run's identity,
the activity log the parent will recap, the tool currently running,
and whether the run has already said it was done.

:class:`StreamState` holds the parts they share, so the base handler
can read them without probing for their presence. It asks
``state.current_run_id`` rather than ``getattr(state,
"current_run_id", None)``, and the answer is typed: a state that
cannot answer is a mistake the type checker sees, not a ``None`` that
turns up later as a missing final answer.

This is a bundle, deliberately: the fields travel together and are
mutated together, and splitting them across two objects would mean
two things to keep in step. It is the shape CODE_STANDARDS.md calls
for under Pattern 4 (composition over god-classes) — one object that
owns a concern, rather than a handler that owns everything.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class StreamState(BaseModel):
    """The state every stream run carries."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ── Identity ────────────────────────────────────────────────────
    #: Sticky card identifier stamped on every emitted event so the
    #: frontend routes them all to the same progress card. Empty when
    #: the caller didn't provide one.
    card_id: str = ""

    # ── Activity log ────────────────────────────────────────────────
    #: One line per notable event (tool call, HITL pause, error).
    #: Concatenated into the parent agent's tool-return so the model
    #: can recap what the sub-agent did. NOT sent to the FE.
    log: list[str] = Field(default_factory=list)

    # ── Tool state ──────────────────────────────────────────────────
    #: Currently-executing tool name, for the "└─ result" pairing.
    current_tool: str | None = None

    # ── Agno run identity ───────────────────────────────────────────
    #: The run's own run_id (Agno UUID). Latched onto the first event
    #: that carries it — Agno only yields ``RunStartedEvent`` when
    #: ``stream_events=True``, and the specialist streams deliberately
    #: don't pass that, so a RunStarted-only capture would leave this
    #: ``None`` and the ``aget_run_output`` fallback would silently
    #: miss.
    current_run_id: str | None = None
    #: The run's session_id. Same latch-first-non-empty rule. Needed
    #: for the ``aget_run_output(run_id, session_id)`` lookup that
    #: reads the final answer after the stream ends.
    current_session_id: str | None = None

    # ── Completion tracking ─────────────────────────────────────────
    #: Backup capture of the final answer from ``RunCompletedEvent``.
    #: ``aget_run_output`` is the canonical source, but the async DB
    #: write that backs it sometimes hasn't flushed by the time the
    #: stream ends — the streamed RunCompletedEvent already carries
    #: the full content, so we hold it as a fallback.
    completed_content: str = ""
    #: True once this stream has said the run was done.
    agent_completed_emitted: bool = False
    #: Whether this stream says the run was done on its own behalf. A
    #: single specialist does; a team does not — each member reports
    #: its own completion, and a blanket team completion would say
    #: the whole team finished when one member did.
    emits_agent_completed: bool = True

    # ── Behaviour ──────────────────────────────────────────────────
    def latch_ids(self, event: object) -> bool:
        """Latch ``run_id`` / ``session_id`` from the first event that
        carries each.

        Agno only yields ``RunStartedEvent`` when
        ``stream_events=True``, which the specialist streams don't
        pass to keep them quiet, so a ``RunStartedEvent``-only capture
        would leave both fields ``None`` and the
        ``aget_run_output(run_id, session_id)`` lookup would silently
        return ``None``. Every Agno run event carries these fields, so
        latching onto the first non-empty value we see is sufficient
        and stable across pause/resume.

        Returns ``True`` iff this call latched ``current_run_id`` for
        the first time — the signal for the caller to register the run
        with the cancellation registry.
        """
        newly_run = False
        if not self.current_run_id:
            ev_run_id = getattr(event, "run_id", None)
            if ev_run_id:
                self.current_run_id = ev_run_id
                newly_run = True
        if not self.current_session_id:
            ev_session_id = getattr(event, "session_id", None)
            if ev_session_id:
                self.current_session_id = ev_session_id
        return newly_run

    def record_completion(self, content: str | None, metrics: object = None) -> None:
        """Keep the streamed ``RunCompletedEvent.content`` as a
        fallback in case the DB-backed lookup comes up empty.

        ``metrics`` is accepted and ignored: it is carried by the
        completion event, but the state keeps no metrics of its own —
        the token totals go on the wire with the completion event
        itself.
        """
        del metrics
        if content:
            self.completed_content = str(content)
        if self.emits_agent_completed:
            self.agent_completed_emitted = True


__all__ = ["StreamState"]
