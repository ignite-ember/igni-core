"""The template every spawn follows.

:class:`SpawnRunner` is the shape a spawn has regardless of what it
spawns: reserve a budget slot, build the runnable, announce the
spawn, run it under a deadline, announce the end, and compose the
parent-facing answer.

The two spawn shapes — one specialist, one sub-team — differ in how
they build their runnable and how they render their answer; they
share everything else, and that shared part lives here.

Concrete subclasses override:

* :meth:`_preflight` — cheap validation *before* the budget is
  reserved. Return an error string to short-circuit or ``None`` to
  proceed.
* :meth:`_reserve_count` — how many budget slots to reserve.
* :meth:`_build` — assemble the runnable (agent or team) and return
  an error string to short-circuit, or ``None`` on success.
* :meth:`_execute` — actually stream the runnable and return
  ``(result_text, activity_lines)``.
* :meth:`_agent_label` — the ``agent_name`` field on the
  SubagentStart / SubagentStop payloads.
* :meth:`format_result` — compose the parent-facing string.
* :meth:`_on_finally` — optional cleanup (worktree finalize).

Everything a spawn needs arrives as one :class:`SpawnContext`, so
the runner holds one reference instead of a dozen fields.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from igni_core.core.tools.orchestrate_spawn_hooks import SpawnHooks
from igni_core.core.tools.orchestrate_spawn_types import (
    SpawnContext,
    SpawnResult,
    SubagentStartPayload,
    SubagentStopPayload,
)
from igni_core.core.tools.spawn_context import SpawnDeadline


class SpawnRunner:
    """Abstract base for a single sub-spawn.

    Owns the template :meth:`run` — depth guard, budget reservation,
    sub-runnable build, hook lifecycle, ``agent_started`` emission,
    :class:`SpawnDeadline` timeout, and the single ``SubagentStop``
    fire site.
    """

    def __init__(self, context: SpawnContext) -> None:
        self._context = context
        self._hooks = SpawnHooks(
            executor=context.wiring.hook_executor,
            sinks=context.wiring.sinks,
            # Stable per-spawn id — stamped on every orchestrate event
            # emitted for this spawn so the FE routes them all into
            # the same team-progress card.
            card_id=uuid.uuid4().hex[:8],
        )
        self._spawn_timeout = context.scope.settings.orchestration.sub_team_timeout

    # ── Template method ──────────────────────────────────────────

    async def run(self) -> str:
        """Template method — orchestrates the whole spawn lifecycle.

        Returns the parent-facing response string (Agno tool return
        contract). Internally routes error branches through
        :class:`SpawnResult` before rendering via
        :meth:`_render_error` so the failure information is typed at
        the boundary even though the outer return is a string.
        """
        scope = self._context.scope
        session_id = self._context.wiring.session_id

        # Live since agents declaring ``can_orchestrate`` receive their own
        # toolkit at ``current_depth + 1`` (see
        # :meth:`AgentSpawn._grant_nested_orchestration`). Reaching it means a
        # chain of delegations ran the config's depth out; the spawn budget
        # caps the same recursion by total count, session-wide.
        if scope.current_depth >= scope.max_depth:
            return f"Error: Maximum nesting depth ({scope.max_depth}) reached."

        if err := self._preflight():
            return err

        reserved = self._reserve_count()
        if limit_err := scope.budget.try_reserve(reserved):
            return limit_err

        build_err = self._build()
        if build_err is not None:
            # Nothing was spawned — an unknown agent name, a worktree
            # that could not be created. The slots were reserved a
            # moment ago only because the cap must be enforced before
            # work starts; holding them now would charge the session
            # for an agent that never ran. See ``SpawnBudget.release``.
            scope.budget.release(reserved)
            return build_err

        label = self._agent_label()
        await self._hooks.fire_start(
            SubagentStartPayload(
                session_id=session_id,
                agent_name=label,
                task=self._task_preview(),
                mode=self._start_mode(),
            )
        )
        self._on_started_progress()

        try:
            start = time.monotonic()
            # The timeout counts the spawn's own work, not the time a
            # person takes over an approval it asked for: approvals do
            # not expire (``SpawnDeadline``).
            async with SpawnDeadline.running(self._spawn_timeout):
                result, activity = await self._execute()
            elapsed = time.monotonic() - start

            await self._hooks.fire_stop_once(
                SubagentStopPayload(
                    session_id=session_id,
                    agent_name=label,
                    result_preview=result[:500],
                )
            )
            return self.format_result(elapsed=elapsed, result=result, activity=activity)
        except asyncio.TimeoutError:
            envelope = SpawnResult(ok=False, message=self._timeout_message())
            await self._hooks.fire_stop_once(
                SubagentStopPayload(
                    session_id=session_id,
                    agent_name=label,
                    error=envelope.message,
                )
            )
            return self._render_error(envelope)
        except asyncio.CancelledError:
            # ``CancelledError`` is a ``BaseException``, so the
            # ``except Exception`` below never saw it and there was no
            # ``finally``: a cancelled isolated spawn — session teardown,
            # a client disconnect, an outer ``wait_for`` — left its git
            # worktree and branch on disk with nothing left holding a
            # reference to them, and SubagentStop never fired so the FE
            # card stayed open forever. Announce the stop, let the
            # ``finally`` clean up, and re-raise so the cancellation
            # still propagates to whoever asked for it.
            await self._hooks.fire_stop_once(
                SubagentStopPayload(
                    session_id=session_id,
                    agent_name=label,
                    error="Sub-spawn was cancelled.",
                )
            )
            raise
        except Exception as exc:
            envelope = SpawnResult(ok=False, message=self._exception_message(exc))
            await self._hooks.fire_stop_once(
                SubagentStopPayload(
                    session_id=session_id,
                    agent_name=label,
                    error=envelope.message,
                )
            )
            return self._render_error(envelope)
        finally:
            # One call site instead of one per exit branch — the three
            # explicit calls this replaces covered every branch that
            # existed and none of the ones that did not (cancellation).
            self._on_finally()

    # ── Extension points ─────────────────────────────────────────

    def _preflight(self) -> str | None:
        """Cheap validation before budget reservation.

        Return an error string to short-circuit, or ``None`` to
        proceed. Base impl: no-op.
        """
        return None

    def _reserve_count(self) -> int:
        """How many budget slots this spawn reserves."""
        return 1

    def _build(self) -> str | None:
        """Assemble the runnable (agent or team). Return an error
        string to short-circuit, or ``None`` on success (subclass
        stashes the runnable on its own state).
        """
        return None

    async def _execute(self) -> tuple[str, list[str]]:
        """Stream the built runnable. Returns ``(result, activity)``."""
        raise NotImplementedError

    def _agent_label(self) -> str:
        """The ``agent_name`` field on the SubagentStart/Stop payloads."""
        raise NotImplementedError

    def _task_preview(self) -> str:
        """Task string clipped to 500 chars for hook payloads."""
        return self._context.task[:500]

    def _start_mode(self) -> str | None:
        """Optional ``mode`` on the SubagentStart payload."""
        return None

    def _on_started_progress(self) -> None:
        """Emit any spawn-specific ``agent_started`` progress event."""

    def format_result(self, *, elapsed: float, result: str, activity: list[str]) -> str:
        """Compose the parent-facing response string."""
        raise NotImplementedError

    def _on_finally(self) -> None:
        """Optional cleanup — always called on the way out."""

    def _timeout_message(self) -> str:
        """Error message for the timeout branch."""
        return f"Sub-spawn exceeded spawn timeout ({self._spawn_timeout}s) and was aborted."

    def _exception_message(self, exc: Exception) -> str:
        """Error message for the generic-exception branch."""
        return f"Error running sub-spawn: {exc}"

    def _render_error(self, envelope: SpawnResult) -> str:
        """Turn a :class:`SpawnResult` failure into the outer string
        return. Subclasses can override to add trailers.
        """
        return envelope.message


__all__ = ["SpawnRunner"]
