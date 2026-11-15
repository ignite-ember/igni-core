"""Team-wiring collaborator — extracted from :class:`BackendServer`.

The previous ``BackendServer.wire_queue_hook`` and
``wire_orchestrate_progress`` methods each reached into
``session.main_team`` to mutate its ``tool_hooks`` / ``post_hooks``
lists or the ``OrchestrateTools`` callbacks. Both are one-shot
wiring concerns that fire at pool-runtime construction — not part
of the run lifecycle, not part of the session core, but also not
something ``BackendServer`` should own the details of.

Extracting to :class:`TeamWiring` gives us:

* One place to grep when the team's hook attributes change shape.
* A tests-friendly seam (``TeamWiring(session).wire_queue_hook(q)``)
  that doesn't need a full ``BackendServer`` fixture.
"""

from __future__ import annotations

from collections.abc import Callable, MutableSequence
from typing import TYPE_CHECKING

from igni_core.core.hooks.queue import QueueBridge
from igni_core.core.session.event_log_schema import (
    VisualizationDeltaDraft,
    VisualizationDeltaPayload,
)
from igni_core.core.tools.orchestrate import OrchestrateTools
from igni_core.core.tools.orchestrate_contracts import EventAppender, OnProgress
from igni_core.core.tools.orchestrate_events import VisualizationDeltaEvent

if TYPE_CHECKING:
    from igni_core.core.session import Session


class TeamWiring:
    """One-shot wiring of queue hooks + orchestrate-progress
    callbacks onto :attr:`Session.main_team`.

    Every wiring is applied to the session's live ``main_team`` and
    registered with the session, which applies it again each time it
    builds a fresh team (the workspace-trust decision, ``/model``, a
    plugin or group-pack reload, a cloud sign-out). It was applied once:
    a rebuilt team had no progress callback, no event-log appender and
    no queue hooks, so a window saw no sub-agent at all after the first
    Trust click.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _keep(self, apply: Callable[[], None]) -> None:
        """Apply ``apply`` now and again on every fresh main team."""
        apply()
        self._session.main_team_wirings.append(apply)

    def wire_queue_hook(self, queue: MutableSequence[str]) -> None:
        """Wire queue hooks onto the team.

        - Tool-hook (injector) drains the queue after each tool call
          so the model sees queued text on its next iteration.
        - Post-hook (persister) records those drained items as
          proper user-role history entries before the session is
          saved.
        """
        self._keep(lambda: QueueBridge(queue=queue).register_on(self._session.main_team))

    def wire_orchestrate_progress(self, callback: OnProgress) -> None:
        """Set a progress callback on the orchestrate tool."""

        def apply() -> None:
            for tool in self._orchestrate_tools():
                tool.wiring.sinks.on_progress = callback

        self._keep(apply)

    def wire_orchestrate_event_appender(self, appender: EventAppender) -> None:
        """Set the session event-log appender on the orchestrate
        tool so the visualizer final-delta lands in the persisted
        event log — the ``get_chat_history`` splicer uses that
        entry to place the viz card next to its originating
        ``spawn_agent`` tool turn on reload."""

        def apply() -> None:
            for tool in self._orchestrate_tools():
                tool.wiring.sinks.event_appender = appender

        self._keep(apply)

    def wire_event_log(self) -> None:
        """Keep the visualizer's final card in the session's own event log.

        Nothing called :meth:`wire_orchestrate_event_appender`, so the
        card was pushed live and never kept: ``get_chat_history`` had
        no ``visualization`` turn, and a reload lost the view.
        """
        session = self._session

        async def append(event: VisualizationDeltaEvent, run_id: str) -> None:
            payload = VisualizationDeltaPayload.model_validate(event.model_dump(by_alias=True))
            await session.append_event(VisualizationDeltaDraft(payload=payload, run_id=run_id))

        self.wire_orchestrate_event_appender(append)

    def active_subagent_runs(self) -> int:
        """Sub-agent runs the main agent has going (its spawns' registry)."""
        registries = {
            id(t.wiring.sinks.subagent_registry): t.wiring.sinks.subagent_registry
            for t in self._orchestrate_tools()
        }
        return sum(len(r) for r in registries.values())

    def _orchestrate_tools(self):
        """Iterate every :class:`OrchestrateTools` instance attached
        to the team (there's usually one, but be defensive for
        tests / plugins that inject more)."""
        for tool in self._session.main_team.tools or []:
            if isinstance(tool, OrchestrateTools):
                yield tool
