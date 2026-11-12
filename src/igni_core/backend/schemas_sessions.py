"""Typed schemas for session-lifecycle result envelopes.

Sibling convention: mirrors :mod:`igni_core.backend.schemas_pause` /
:mod:`igni_core.backend.schemas_run` — one ``schemas_*.py`` file
per top-level BE pipeline. This module owns the Pattern-3 result
envelopes for the session-management RPCs on
:class:`~igni_core.backend.server_sessions.SessionsController`.

Consumers:

* :class:`AutoNameResult` — replaces the ``str | None`` return of
  :meth:`SessionsController.maybe_auto_name_session`. The wire push
  the dispatcher forwards to the FE reads ``.name`` when ``.ok`` is
  true, so the FE contract stays byte-identical to the previous
  ``str | None`` shape (empty string / falsy → no push).
* :class:`ShutdownReport` — pool-level shutdown / eviction envelope.
  Surfaces per-runtime shutdown outcomes so callers see failures
  instead of them being swallowed into a DEBUG log line.
* :class:`EvictionReport` — richer result for the periodic evictor
  sweep, carrying per-runtime idle age + a shutdown outcome each.
* :class:`DispatchedBackend` — the wider slice the message
  dispatcher drives.
* :class:`BackendLike` / :class:`TransportLike` — structural typing
  Protocols so :class:`~igni_core.backend.session_pool.SessionPool`
  and :class:`~igni_core.backend.session_stamping_transport.SessionStampingTransport`
  can stop annotating their most critical seams as ``Any``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel

from igni_core.protocol import messages as msg

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from igni_core.backend.activity import SessionActivity
    from igni_core.backend.controller_registry import Controllers
    from igni_core.backend.session_pool import SessionRuntime
    from igni_core.core.session.event_log_schema import (
        SessionEvent,
        SessionEventAppendResult,
        SessionEventDraft,
        SessionEventKind,
    )
    from igni_core.core.utils.media_schemas import MediaAttachments


class AutoNameResult(BaseModel):
    """Result envelope for
    :meth:`~igni_core.backend.server_sessions.SessionsController.maybe_auto_name_session`.

    Replaces the previous ``str | None`` return: ``ok=True`` and a
    non-empty ``name`` when a fresh name was generated;
    ``ok=False`` (with ``reason`` explaining why) otherwise. The
    dispatcher reads ``.name`` when ``.ok`` is true and swallows
    the push otherwise — the FE contract stays identical to the
    previous truthy-string check.
    """

    ok: bool = False
    name: str = ""
    reason: Literal[
        "generated",
        "already_named",
        "no_name_produced",
        "error",
    ] = "error"


class ShutdownReport(BaseModel):
    """Per-runtime shutdown outcome.

    Returned by
    :meth:`~igni_core.backend.session_pool.SessionRuntime.shutdown_safely`
    and by
    :meth:`~igni_core.backend.session_pool.SessionPool.shutdown` so
    callers can surface failures instead of watching them disappear
    into a DEBUG log line.
    """

    ok: bool
    session_id: str
    error: str = ""


class EvictedRuntimeReport(BaseModel):
    """One evicted runtime's line in an :class:`EvictionReport`.

    Carries the runtime's session id + how many seconds it had been
    idle at the time of eviction, plus a :class:`ShutdownReport` for
    its shutdown outcome. Lifts the ``now - rt.last_used_at`` log-side
    computation into the typed envelope where it belongs.
    """

    session_id: str
    idle_seconds: float
    shutdown: ShutdownReport


class EvictionReport(BaseModel):
    """Result envelope for
    :meth:`~igni_core.backend.session_pool.SessionPool.evict_idle`.

    Replaces the previous ``list[str]`` return so the evictor sweep
    surfaces per-runtime shutdown failures and idle ages in a typed
    shape.
    """

    evicted: list[EvictedRuntimeReport] = []
    kept: int = 0

    @property
    def evicted_ids(self) -> list[str]:
        """Session ids of every evicted runtime.

        Convenience for the log line in
        :meth:`BackendSupervisor._evictor_loop` — keeps the wire
        model rich while giving the log site a flat ``list[str]``.
        """
        return [e.session_id for e in self.evicted]


# ── Structural typing for pool seams ─────────────────────────────


class BackendLike(Protocol):
    """Structural Protocol describing the slice of ``BackendServer``
    that :class:`~igni_core.backend.session_pool.SessionPool` and
    :class:`~igni_core.backend.session_stamping_transport.SessionStampingTransport`
    actually reach for.

    Codifying the surface lets the pool + stamping transport type
    their backend field as ``BackendLike`` instead of ``Any``, so
    mypy catches the moment a rename on ``BackendServer`` breaks a
    pool-side call.

    The Protocol is structural (no ``runtime_checkable`` decorator)
    — sub-classing is not required, matching duck-typing suffices.
    ``attach_runtime`` uses a forward reference so the
    ``SessionRuntime`` import stays under ``TYPE_CHECKING`` and no
    runtime import cycle appears.
    """

    session_id: str
    project_dir: str | Path
    processing: bool
    controllers: Controllers

    async def shutdown(self) -> None: ...
    def wire_queue_hook(self, queue: list[str]) -> None: ...
    def attach_runtime(self, runtime: SessionRuntime) -> None: ...
    def reload_agents(self) -> bool: ...
    def activity(self) -> SessionActivity: ...
    async def record_event(self, event: SessionEventDraft) -> SessionEventAppendResult: ...
    def session_events(self, event_type: SessionEventKind) -> list[SessionEvent]: ...


class DispatchedBackend(BackendLike, Protocol):
    """The slice of ``BackendServer`` that
    :class:`~igni_core.backend.message_dispatcher.MessageDispatcher`
    drives one wire message into: a turn, a HITL decision, a command,
    a session / model / MCP switch, a cancel, and the post-run naming.
    """

    @property
    def runtime(self) -> SessionRuntime | None: ...
    def run_message(
        self, text: str, media: MediaAttachments | None = None
    ) -> AsyncIterator[msg.Message]: ...
    def resolve_hitl(
        self, requirement_id: str, action: str, choice: str = "once"
    ) -> AsyncIterator[msg.Message]: ...
    def resolve_hitl_batch(
        self, decisions: list[msg.HITLDecision]
    ) -> AsyncIterator[msg.Message]: ...
    async def handle_command(self, text: str) -> msg.CommandResult: ...
    async def list_sessions(self) -> msg.SessionListResult: ...
    async def switch_session(self, session_id: str) -> msg.Info: ...
    def switch_model(self, model_name: str) -> msg.Info | msg.Error: ...
    async def toggle_mcp(self, server_name: str, connect: bool) -> msg.Info | msg.Error: ...
    def cancel_run(self) -> None: ...
    def consume_plan_research_flag(self) -> bool: ...
    async def maybe_auto_name_session(self) -> AutoNameResult: ...


class TransportLike(Protocol):
    """Structural Protocol describing the ``send``-only slice of a
    transport that
    :class:`~igni_core.backend.session_stamping_transport.SessionStampingTransport`
    actually calls.

    The stamping wrapper still delegates unknown attribute lookups
    to its inner transport via ``__getattr__`` so richer transport
    surfaces (``receive``, ``close``) remain reachable — this
    Protocol only pins the piece the wrapper itself uses.
    """

    async def send(self, message: msg.Message) -> None: ...


__all__ = [
    "AutoNameResult",
    "BackendLike",
    "DispatchedBackend",
    "EvictedRuntimeReport",
    "EvictionReport",
    "ShutdownReport",
    "TransportLike",
]
