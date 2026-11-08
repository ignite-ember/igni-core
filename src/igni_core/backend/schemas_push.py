"""Typed payloads for every ``PushNotification`` the BE sends the FE.

Extracted out of :mod:`igni_core.backend.__main__` where dozens of
raw ``dict`` literals used to be constructed inline. Every push
payload flowing through :class:`PushNotificationBridge` is a Pydantic
model here so the wire shape can be validated at the seam.

Naming mirrors the ``PushNotification.channel`` value one-for-one so
the FE contract stays greppable.
"""

from __future__ import annotations

from pydantic import BaseModel


class LoginStatusPayload(BaseModel):
    """Channel ``login_status``. Human-readable status text streamed
    while a login attempt is in flight."""

    text: str


class LoginResultPayload(BaseModel):
    """Channel ``login_result``. Terminal result of a login attempt
    (success + reason string). Pushed after the RPC ack, once the
    Anthropic OAuth flow completes."""

    success: bool
    result: str


class SchedulerStartedPayload(BaseModel):
    """Channel ``scheduler_started``. The scheduled-task runner just
    picked up a task and started executing it."""

    task_id: str
    description: str


class SchedulerCompletedPayload(BaseModel):
    """Channel ``scheduler_completed``. Terminal result of a
    scheduled task's execution."""

    task_id: str
    description: str
    result: str


class BackgroundProcessDonePayload(BaseModel):
    """Channel ``background_process_done``. Best-effort ping so the
    UI can badge the watcher panel when a backgrounded shell command
    finishes. The queue-injected assistant nudge carries the tail
    output; this payload is a lightweight header."""

    pid: int | None
    cmd: str
    exit_code: int | None
    duration_seconds: float


class FileEditedPayload(BaseModel):
    """Channel ``file_edited``. Fired every time an edit tool writes
    to disk. A client with an editor buffer can refresh it; neither
    the desktop app nor the web client acts on it today."""

    path: str


class ProcessStartedPayload(BaseModel):
    """Channel ``process_started``. Emitted when the shell tool
    launches a new background process."""

    pid: int | None
    cmd: str | None
    started_at: float | None


class ProcessLinePayload(BaseModel):
    """Channel ``process_line``. One line of stdout/stderr from a
    running background process (streamed live to the watcher panel)."""

    pid: int | None
    line: str | None


class ProcessExitedPayload(BaseModel):
    """Channel ``process_exited``. A background process finished —
    the watcher panel flips its status pill."""

    pid: int | None
    cmd: str | None
    exit_code: int | None
    duration_seconds: float | None


class SessionNamedPayload(BaseModel):
    """Channel ``session_named``. The BE picked an auto-generated
    display name for a freshly-created session."""

    session_id: str
    name: str
