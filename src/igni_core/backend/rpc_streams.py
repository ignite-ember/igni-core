"""What the agent's work sends to the windows, for the calls that start or end some.

An RPC that sets the agent working declares, in its binding's ``streams``,
what igni sends while the work goes on. Two sets recur:

* :data:`TOOL_PUSHES` — what the agent's tools push while they run,
  however the run was started: the to-do list, a file edited, a background
  process's life, a sub-agent's progress, an HTTP retry, a permission-mode
  switch (``enter_plan_mode``).
* :data:`RUN_STREAM` — a run streamed to the windows, as a typed message
  streams it: the run's events from ``run_started`` to ``stream_end``,
  its pauses for approval (``run_paused``, each carrying its
  ``hitl_request`` items), and :data:`TOOL_PUSHES`.
"""

from __future__ import annotations

from igni_core.backend.rpc_binding import Stream
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.enums import PushChannel

TOOL_PUSHES: tuple[Stream, ...] = (
    PushChannel.TODOS_UPDATED,
    PushChannel.FILE_EDITED,
    PushChannel.PROCESS_STARTED,
    PushChannel.PROCESS_LINE,
    PushChannel.PROCESS_EXITED,
    PushChannel.BACKGROUND_PROCESS_DONE,
    PushChannel.ORCHESTRATE_EVENT,
    PushChannel.HTTP_RETRY_ATTEMPT,
    PushChannel.HTTP_RETRY_SUCCEEDED,
    PushChannel.PERMISSION_MODE_CHANGED,
)

RUN_STREAM: tuple[Stream, ...] = (
    msg.RunStarted,
    msg.ContentDelta,
    msg.ReasoningStarted,
    msg.ToolStarted,
    msg.ToolCompleted,
    msg.ToolError,
    msg.ModelCompleted,
    msg.TaskCreated,
    msg.TaskUpdated,
    msg.TaskIteration,
    msg.TaskStateUpdated,
    msg.RunPaused,
    msg.RequirementDetails,
    msg.RunCompleted,
    msg.RunError,
    msg.StreamingDone,
    msg.Info,
    msg.Error,
    msg.StreamEnd,
    PushChannel.PLAN_SUBMITTED,
    *TOOL_PUSHES,
)

__all__ = ["RUN_STREAM", "TOOL_PUSHES"]
