"""The collaborators an orchestrate run talks to.

Four of them, and they are all structural types rather than concrete
classes: the progress callback, the session event-log appender, the
HITL bridge, and the Agno runnable being driven. A run reaches the
outside world only through these, and each of them has more than one
implementation — the real one and the fakes the tests use — so
naming the shape is what makes the dependency legible.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from igni_core.core.tools.orchestrate_events import OrchestrateEvent, VisualizationDeltaEvent

OnProgress = Callable[[OrchestrateEvent], None]
"""Structural type for the ``on_progress`` callback.

The callback receives the typed event, not a dict: the event's own
class says what it is, and the frontend's wire shape is produced once
at the push boundary rather than at each emit site.
"""


EventAppender = Callable[[VisualizationDeltaEvent, str], Awaitable[None]]
"""Structural type for the session event-log appender.

Signature: ``async (delta, run_id) -> None`` — the viz card to log
and the run that emitted it. The delta travels as the typed wire
event, not as a free-form ``(type, payload)`` pair, so the log
receives a shape it can validate. Wired at session bootstrap;
``None`` in unit-test contexts where the session broadcast plumbing
isn't wired up.
"""


@runtime_checkable
class HitlCoordinatorProtocol(Protocol):
    """Structural type for the sub-agent HITL bridge.

    ``push_requirement`` returns a ``req_id`` the handler holds to
    later ``wait_resolved`` / ``cleanup``. Implemented by
    :class:`igni_core.core.sub_agent_hitl.SubAgentHITLCoordinator`.
    """

    async def push_requirement(
        self, req: Any, *, run_id: str, agent_path: list[str], session_id: str = ""
    ) -> str: ...

    async def wait_resolved(self, req_id: str) -> Any:
        """Wait until the person answers. What it answers with is the
        implementation's business — the handler only waits."""
        ...

    def cleanup(self, req_id: str) -> None: ...

    def forget_spawn(self, run_id: str) -> None: ...


@runtime_checkable
class AgnoRunnable(Protocol):
    """Structural type for the ``agent`` / ``team`` argument passed
    into the stream handlers.

    Every Agno ``Agent`` and ``Team`` satisfies this — plus the test
    fakes in ``tests/test_tool_arg_streaming.py`` and
    ``tests/test_subagent_hitl_e2e.py``.

    ``acontinue_run`` covers both ways a paused run is picked up
    again: by the id Agno stored it under (``run_id``) and by the
    run object itself (``run_response``). Agno's own signature takes
    both as optional keywords, so one method describes the shape.
    """

    def arun(self, task: str, *, stream: bool = True) -> Any: ...

    def acontinue_run(
        self,
        run_response: Any = None,
        *,
        run_id: str | None = None,
        requirements: Any = None,
        session_id: Any = None,
        stream: bool = True,
    ) -> Any: ...

    async def aget_run_output(self, *, run_id: str, session_id: str) -> Any: ...

    async def aget_last_run_output(self, *, session_id: str) -> Any: ...


__all__ = [
    "AgnoRunnable",
    "EventAppender",
    "HitlCoordinatorProtocol",
    "OnProgress",
]
