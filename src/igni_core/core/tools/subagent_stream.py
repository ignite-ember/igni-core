"""Sub-agent stream state — the Pydantic models that own the mutation
logic for the two streaming handlers.

Why this exists: before this file, ``_run_agent_streaming``'s nested
``_handle`` closure declared 11 nonlocals to track content buffers,
run-id capture, visualizer streaming throttle state, and completion
markers. Every new feature added another nonlocal. Adding a nonlocal
is a two-line change (``nonlocal foo; ... foo = ...``) with no schema
enforcement — the ceiling for that pattern is a bug per addition
because typos become new locals silently.

The refactor is model-first, per CODE_STANDARDS.md Pattern 4
(composition over god-classes) + AP2 (>5 nonlocals in a function is
a smell). One Pydantic model holds every piece of per-stream state
AND owns the mutation methods that used to be inline in the handler
closure. Adding a new field is a one-line change on the model + a
call site; adding new behaviour is a new method on the model, not a
new free function taking ``state`` as its first arg.

Both states extend :class:`StreamState`, which holds what the two
handlers have in common — the card id, the activity log, the running
tool, the run identity, the completion flag. What is left here is
what makes a specialist different from a team.
"""

from __future__ import annotations

import time
import uuid

from pydantic import ConfigDict, Field

from igni_core.core.tools.orchestrate_preview import PREVIEWS
from igni_core.core.tools.orchestrate_stream_state import StreamState


class SubAgentStreamState(StreamState):
    """Per-invocation state for the sub-agent stream handler.

    One instance is constructed at the top of every sub-agent stream
    and mutated across the event loop. On top of what
    :class:`StreamState` holds, this adds:

    - **Identity**: what agent this is, how the FE identifies it.
    - **Content preview**: token-buffered lines for the FE tree view.
    - **Visualizer streaming**: partial-JSON throttle state.
    - **Parent run identity**: the top-level run that spawned this one.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ── Identity ────────────────────────────────────────────────────
    #: Dot-joined agent path, stable across events for one run.
    #: ``"root"`` for top-level runs; ``"architect.editor"`` for
    #: nested sub-agent runs. FE uses this as the tree-node id.
    agent_path_id: str

    # ── Content preview state ───────────────────────────────────────
    #: Last content-preview emission time (``time.monotonic``).
    #: Throttles the ``content_preview`` event to ~2 Hz so the FE
    #: doesn't drown in token-sized deltas.
    last_update: float = 0.0
    #: Last preview text we emitted — dedup so identical previews
    #: don't spam the wire when the tail hasn't advanced.
    last_preview: str = ""
    #: Accumulated content stream. Agno yields deltas that are often
    #: single tokens; we buffer here and slice the tail for the
    #: multi-line preview.
    content_buf: str = ""

    # ── Visualizer streaming state ──────────────────────────────────
    #: Stable spec_id for THIS sub-agent's visualize call — used by
    #: the FE to dedupe multiple progressive deltas onto one card.
    #: Generated fresh per stream so parallel visualizer calls don't
    #: collide. Constructor default assigns a hex UUID prefix.
    vis_spec_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    #: Length of the last-emitted ``arguments_partial`` string, to
    #: dedup emissions when the accumulator advanced by zero bytes.
    vis_last_emitted_len: int = 0
    #: Last visualization-delta emission time (``time.monotonic``).
    #: 50 ms throttle prevents wire spam on fast models.
    vis_last_emit_at: float = 0.0

    # ── Parent run identity ─────────────────────────────────────────
    #: ``RunStartedEvent.parent_run_id`` — the TOP-LEVEL run that
    #: spawned this sub-agent. This is what ``get_chat_history``
    #: splicing keys on when placing visualizer cards next to their
    #: originating ``spawn_agent`` tool call. Using
    #: ``current_run_id`` here would fall through to the tail-append
    #: fallback and lose the correct positioning.
    parent_top_run_id: str | None = None

    # ── Behaviour ──────────────────────────────────────────────────
    def latch_ids(self, event: object) -> bool:
        """Latch the run's ids, and the parent's run id too.

        Beyond the shared run/session latch, a specialist also keeps
        the top-level run that spawned it — see
        :attr:`parent_top_run_id`.

        Returns ``True`` iff this call latched ``current_run_id`` for
        the first time — signal for the caller to register with the
        cancellation registry.
        """
        newly_run = super().latch_ids(event)
        if not self.parent_top_run_id:
            ev_parent = getattr(event, "parent_run_id", None)
            if ev_parent:
                self.parent_top_run_id = ev_parent
        return newly_run

    def can_emit_vis_delta(self, now_s: float) -> bool:
        """50ms throttle for visualizer partial-JSON deltas.

        First delta always emits so the FE mounts the card early.
        Subsequent deltas need at least 50ms of wall-clock separation
        from the previous emission — keeps the wire quiet on fast
        models while still feeling live.
        """
        return self.vis_last_emitted_len == 0 or (now_s - self.vis_last_emit_at >= 0.05)

    def record_vis_emission(self, now_s: float, partial_len: int) -> None:
        """Note that we just emitted a delta at ``now_s`` for a
        partial-args string of length ``partial_len``."""
        self.vis_last_emitted_len = partial_len
        self.vis_last_emit_at = now_s

    def append_content_delta(self, chunk: str) -> str | None:
        """Buffer a streaming content chunk and return a preview
        string when the ~2 Hz throttle fires AND the preview text
        actually changed.

        Returns ``None`` when the throttle window hasn't elapsed or
        the preview text matches the last one we emitted.
        """
        if not chunk:
            return None
        self.content_buf += str(chunk)
        now = time.monotonic()
        if now - self.last_update <= 0.5:
            return None
        self.last_update = now
        preview = PREVIEWS.format_content_buffer(self.content_buf)
        if not preview or preview == self.last_preview:
            return None
        self.last_preview = preview
        return preview


class TeamStreamState(StreamState):
    """Per-invocation state for the team stream handler.

    Sibling of :class:`SubAgentStreamState` — same "model instead of
    nonlocals" refactor, but shaped for the team case. Two things
    differ from the single-agent state:

    1. Throttle/dedup state is keyed by ``agent_path_id`` (dict per
       agent) because in ``broadcast`` and ``coordinate`` modes
       multiple members emit interleaved deltas; a shared last-
       update would let one chatty specialist starve the others.
    2. There's a ``team_path_id`` — the dot-joined base identity of
       the whole team run, used as the tree-node id for the
       coordinator itself and as a namespace prefix for member
       events.

    A team does not report its own completion: each member reports
    its own, and a blanket team completion would say the whole team
    finished when one member did.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ── Identity ────────────────────────────────────────────────────
    #: Dot-joined base identity for this team run. ``"team"`` for
    #: top-level teams; ``"architect.review_team"`` when the team is
    #: itself a member of a parent team.
    team_path_id: str

    # ── Content preview state (per-member keyed) ────────────────────
    #: Currently-executing agent name, latched onto the first event
    #: from a fresh specialist so tool events without an explicit
    #: agent_name (rare, but Agno's team coordinator occasionally
    #: drops it) route to the right node.
    current_agent: str = ""
    #: Last content-preview emission time per member (throttled to
    #: ~2 Hz per agent). Keyed by agent_path_id so one chatty member
    #: can't starve the others.
    last_update_by_agent: dict[str, float] = Field(default_factory=dict)
    #: Last preview text per member (dedup key).
    last_preview_by_agent: dict[str, str] = Field(default_factory=dict)
    #: Content buffer per member — Agno yields deltas that are often
    #: single tokens; we buffer here and slice the tail for the
    #: multi-line preview.
    content_buf_by_agent: dict[str, str] = Field(default_factory=dict)

    # ── Completion tracking ─────────────────────────────────────────
    #: A team's members report their own completion.
    emits_agent_completed: bool = False

    # ── Behaviour ──────────────────────────────────────────────────
    def append_content_delta(self, agent_path: str, chunk: str) -> str | None:
        """Per-agent variant of
        :meth:`SubAgentStreamState.append_content_delta`. The throttle
        + dedup are keyed by ``agent_path`` so one chatty member
        can't starve the others."""
        if not chunk:
            return None
        buf = self.content_buf_by_agent.get(agent_path, "") + str(chunk)
        self.content_buf_by_agent[agent_path] = buf
        now = time.monotonic()
        if now - self.last_update_by_agent.get(agent_path, 0.0) <= 0.5:
            return None
        self.last_update_by_agent[agent_path] = now
        preview = PREVIEWS.format_content_buffer(buf)
        if not preview or preview == self.last_preview_by_agent.get(agent_path):
            return None
        self.last_preview_by_agent[agent_path] = preview
        return preview


__all__ = ["SubAgentStreamState", "TeamStreamState"]
