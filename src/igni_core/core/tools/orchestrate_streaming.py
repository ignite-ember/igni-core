"""The two streamers, as functions.

The work is in the handlers —
:class:`SubAgentStreamHandler`
(``orchestrate_agent_stream.py``) and
:class:`TeamStreamHandler` (``orchestrate_team_stream.py``), both on
top of :class:`BaseStreamHandler`. This module holds only the flat
entry points a spawn calls.

Both take a :class:`StreamContext` and return ``(response, log)`` so
the parent agent's tool return contains the sub-agent's final text
plus the per-tool-call activity log line.
"""

from __future__ import annotations

from igni_core.core.tools.orchestrate_agent_stream import SubAgentStreamHandler
from igni_core.core.tools.orchestrate_stream_context import StreamContext
from igni_core.core.tools.orchestrate_team_stream import TeamStreamHandler


async def run_agent_streaming(context: StreamContext) -> tuple[str, list[str]]:
    """Stream one specialist agent's run. Returns ``(response, log)``.

    A ``RunPausedEvent`` from the sub-agent is surfaced through the
    context's HITL coordinator so the user can confirm/deny in the
    TUI; the run is then resumed via ``acontinue_run`` and the handler
    keeps iterating its events. Without a coordinator, pauses are
    ignored and the sub-agent's tools will return empty results.
    """
    handler = SubAgentStreamHandler(context)
    return await handler.run()


async def run_team_streaming(context: StreamContext) -> tuple[str, list[str]]:
    """Stream one coordinated team's run. Returns ``(response, log)``.

    Mirrors :func:`run_agent_streaming`'s pause-handling, and threads
    the per-member ``agent_path`` through the FE event so the card
    shows ``team → member`` not just ``team``.
    """
    handler = TeamStreamHandler(context)
    return await handler.run()


__all__ = ["run_agent_streaming", "run_team_streaming"]
