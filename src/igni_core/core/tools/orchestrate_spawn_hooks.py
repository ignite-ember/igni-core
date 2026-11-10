"""The hooks and progress events one spawn fires.

A spawn announces itself twice — ``SubagentStart`` when it begins,
``SubagentStop`` when it ends — and emits one ``agent_started``
progress event so the frontend can open the spawn's card.

:class:`SpawnHooks` owns that, including the "``SubagentStop`` fires
exactly once" invariant: the stop flag lives here rather than in the
runner, so a subclass that fires it from its own cleanup path cannot
double-fire it.

The progress callback is read from the sinks at the moment the event
is emitted, not captured when the hooks are built: the session wires
it on after the toolkit is constructed, and a spawn started before
that wiring still reports.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

from igni_core.core.hooks.events import HookEvent
from igni_core.core.tools.orchestrate_events import AgentStartedEvent
from igni_core.core.tools.orchestrate_spawn_types import (
    SubagentStartPayload,
    SubagentStopPayload,
)
from igni_core.core.tools.orchestrate_stream_context import StreamSinks

if TYPE_CHECKING:
    from igni_core.core.hooks.executor import HookExecutor

_spawn_log = logging.getLogger("igni_core.llm_calls")


class SpawnHooks:
    """Fires one spawn's hooks and its ``agent_started`` event."""

    __slots__ = ("_card_id", "_executor", "_sinks", "_stop_fired")

    def __init__(
        self,
        executor: HookExecutor | None,
        sinks: StreamSinks,
        card_id: str,
    ) -> None:
        self._executor = executor
        self._sinks = sinks
        self._card_id = card_id
        self._stop_fired = False

    @property
    def card_id(self) -> str:
        """The progress card every event of this spawn belongs to."""
        return self._card_id

    @property
    def stop_fired(self) -> bool:
        """Whether this spawn has already announced its end."""
        return self._stop_fired

    async def fire_start(self, payload: SubagentStartPayload) -> None:
        """Fire the ``SubagentStart`` hook."""
        await self.fire_hook(HookEvent.SUBAGENT_START, payload)

    async def fire_stop_once(self, payload: SubagentStopPayload) -> None:
        """Fire ``SubagentStop`` at most once per spawn.

        The flag is set before the hook runs, so a hook that fails
        still consumes the one stop this spawn has.
        """
        if self._stop_fired:
            return
        self._stop_fired = True
        await self.fire_hook(HookEvent.SUBAGENT_STOP, payload)

    async def fire_hook(self, event: HookEvent, payload: BaseModel) -> None:
        """Hand one hook event to the executor, if there is one.

        A hook that raises must not take the spawn down with it: a
        hook observes the spawn, it does not participate in it. The
        failure is logged, not swallowed.
        """
        if self._executor is None:
            return
        try:
            await self._executor.execute(event=event, payload=payload)
        except Exception as exc:
            _spawn_log.warning("spawn hook %s failed: %s", event.value, exc)

    def emit_agent_started(self, agent_name: str, task: str) -> None:
        """Emit the ``agent_started`` progress event for this spawn.

        The event travels as the model it is — the frontend's wire
        shape is produced once, at the push boundary.
        """
        callback = self._sinks.on_progress
        if callback is None:
            return
        event = AgentStartedEvent(
            agent_path=agent_name,
            agent=agent_name,
            parent=None,
            task=task,
            card_id=self._card_id,
        )
        try:
            callback(event)
        except Exception as exc:
            _spawn_log.warning("spawn progress callback failed: %s", exc)


__all__ = ["SpawnHooks"]
