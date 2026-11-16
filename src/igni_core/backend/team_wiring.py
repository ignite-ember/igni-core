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
    TeamProgressDraft,
    TeamProgressRecord,
    VisualizationDeltaDraft,
    VisualizationDeltaPayload,
)
from igni_core.core.tools.orchestrate import OrchestrateTools
from igni_core.core.tools.orchestrate_contracts import EventAppender, OnProgress
from igni_core.core.tools.orchestrate_events import OrchestrateEvent, VisualizationDeltaEvent
from igni_core.core.tools.spawn_context import SpawnCalls

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

    #: The progress events that make a card: who ran, what each did, how it
    #: ended. A streaming preview and the visualizer's card (kept on its
    #: own) are left out.
    CARD_EVENTS: frozenset[str] = frozenset(
        {
            "agent_started",
            "agent_paused",
            "agent_completed",
            "run_error",
            "task_created",
            "task_updated",
            "tool_started",
            "tool_completed",
        }
    )

    def wire_orchestrate_progress(self, callback: OnProgress) -> None:
        """Set a progress callback on the orchestrate tool."""

        def apply() -> None:
            for tool in self._orchestrate_tools():
                tool.wiring.sinks.on_progress = callback

        self._keep(apply)

    def keep_card_event(self, event: OrchestrateEvent) -> None:
        """Record one progress-card event on the session's event log.

        Filed under the main agent's run that made the spawn call (the
        outermost call in the chain), as the visualizer's card is.
        """
        if event.type not in self.CARD_EVENTS or not event.card_id:
            return
        chain = SpawnCalls.current()
        outermost = chain[-1] if chain else None
        self._session.event_log_store.record(
            TeamProgressDraft(
                run_id=outermost.run_id if outermost is not None else "",
                payload=TeamProgressRecord(
                    card_id=event.card_id,
                    tool_call_id=outermost.tool_call_id if outermost is not None else "",
                    event=event.model_dump(by_alias=True, exclude_none=True),
                ),
            )
        )

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

        # And each progress-card event, so a reopened chat draws the card
        # again (campaign R665): the history had no turn for a sub-agent or
        # a team, and a reload lost the card.
        def apply() -> None:
            for tool in self._orchestrate_tools():
                tool.wiring.sinks.card_recorder = self.keep_card_event

        self._keep(apply)

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
        # A team that carries no tools (a bare stand-in) has none to wire.
        for tool in getattr(self._session.main_team, "tools", None) or []:
            if isinstance(tool, OrchestrateTools):
                yield tool
