"""What the agent's work sends to the windows, for the calls that start or end some.

A call declares two sets in its binding: ``streams``, what the call itself
causes igni to send, and ``may_stream``, what the agent's tools may push
while work the call started goes on. Two sets recur:

* :data:`RUN_EVENTS` — a run streamed to the windows: its events from
  ``run_started`` to ``stream_end``, and its pauses for approval
  (``run_paused``, each carrying its ``hitl_request`` items).
* :data:`TOOL_PUSHES` — what the agent's tools may push while they run:
  the to-do list, a file edited, a background process's life, a
  sub-agent's progress, an HTTP retry, a permission-mode switch, a plan
  submitted.
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
    PushChannel.PLAN_SUBMITTED,
)

RUN_EVENTS: tuple[Stream, ...] = (
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
)

__all__ = ["RUN_EVENTS", "TOOL_PUSHES"]
