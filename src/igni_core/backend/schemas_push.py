"""Typed payloads for every ``PushNotification`` the BE sends the FE.

Extracted out of :mod:`igni_core.backend.__main__` where dozens of
raw ``dict`` literals used to be constructed inline. Every push
payload flowing through :class:`PushNotificationBridge` is a Pydantic
model here so the wire shape can be validated at the seam.

Naming mirrors the ``PushNotification.channel`` value one-for-one so
the FE contract stays greppable.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginStatusPayload(BaseModel):
    """Channel ``login_status``. Progress of a sign-in that is under way,
    as text for a person."""

    text: str = Field(description="The current status, for a person.")
    flow_id: str = Field(
        default="", description="The sign-in it is about: the `flow_id` that `login` returned."
    )


class LoginResultPayload(BaseModel):
    """Channel ``login_result``. How a sign-in ended. Sent once, after
    the ``login`` call has returned and the sign-in has finished."""

    success: bool = Field(description="Whether the user is now signed in.")
    result: str = Field(
        description="On success, the signed-in email address; on failure, why it failed. "
        "May be empty."
    )
    flow_id: str = Field(
        default="", description="The sign-in it ends: the `flow_id` that `login` returned."
    )


class SchedulerStartedPayload(BaseModel):
    """Channel ``scheduler_started``. A scheduled task has started running."""

    task_id: str = Field(description="The scheduled task's id.")
    description: str = Field(description="What the task does, as it was scheduled.")


class SchedulerCompletedPayload(BaseModel):
    """Channel ``scheduler_completed``. A scheduled task has finished."""

    task_id: str = Field(description="The scheduled task's id.")
    description: str = Field(description="What the task does, as it was scheduled.")
    result: str = Field(description="The task's outcome, as text.")


class BackgroundProcessDonePayload(BaseModel):
    """Channel ``background_process_done``. A shell command running in
    the background has finished. Sent alongside ``process_exited``, for
    a client that only wants to show a notice. The agent is told about
    the output separately."""

    pid: int | None = Field(description="The process's id; `null` when unknown.")
    cmd: str = Field(description="The command that ran.")
    exit_code: int | None = Field(description="Its exit code; `null` when unknown.")
    duration_seconds: float = Field(description="How long it ran, in seconds.")


class FileEditedPayload(BaseModel):
    """Channel ``file_edited``. The agent has written a file. A client
    showing that file in an editor can reload it."""

    path: str = Field(description="The file that was written.")


class ProcessStartedPayload(BaseModel):
    """Channel ``process_started``. A background process has started."""

    pid: int | None = Field(
        description="The process's id, which its later `process_*` pushes carry; "
        "`null` when unknown."
    )
    cmd: str | None = Field(description="The command it runs; `null` when unknown.")
    started_at: float | None = Field(
        description="When it started, in seconds since the Unix epoch; `null` when unknown."
    )


class ProcessLinePayload(BaseModel):
    """Channel ``process_line``. One line of output from a running
    background process, sent as it is printed."""

    pid: int | None = Field(description="The process's id; `null` when unknown.")
    line: str | None = Field(
        description="The line, from stdout or stderr, without its line ending; "
        "`null` when there is none."
    )


class ProcessExitedPayload(BaseModel):
    """Channel ``process_exited``. A background process has finished."""

    pid: int | None = Field(description="The process's id; `null` when unknown.")
    cmd: str | None = Field(description="The command it ran; `null` when unknown.")
    exit_code: int | None = Field(description="Its exit code; `null` when unknown.")
    duration_seconds: float | None = Field(
        description="How long it ran, in seconds; `null` when unknown."
    )


class SessionNamedPayload(BaseModel):
    """Channel ``session_named``. igni has given a new session a
    generated display name."""

    session_id: str = Field(description="The session that was named.")
    name: str = Field(description="Its new display name.")
