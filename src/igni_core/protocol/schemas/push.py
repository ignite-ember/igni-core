"""BE→FE push notifications — one wire message, one channel per push.

A push is a *channel* (which kind of event) and a *payload* (that
event's own fields). Every push travels as the same wire message,
``type: "push_notification"``, and the frontend routes on ``channel``.

Why the payload is a mapping and not a union of models
--------------------------------------------------------
The obvious shape — one message class per channel, the payload typed —
is not available here, and the reasons are structural rather than
stylistic:

* **The wire contract has one entry for every push.** The contract
  (:mod:`igni_core.protocol.schemas.wire`) is a ``type``-discriminated
  union over the message classes, and every push carries the same
  ``type`` value. A second class with the same discriminator cannot
  join that union, so a per-channel class would need a protocol change
  before it could be published at all.
* **The channel set is open.** :class:`PushChannel` is a catalogue,
  not a closed set: a new tool adds a channel by subclassing
  :class:`~igni_core.core.broadcast_schema.BroadcastEvent`, and the
  push carrying it has to go out without a protocol change.
* **The payload must stay a mapping.** The extension host parses a
  push's payload with ``model_validate`` against deliberately lenient
  readers
  (:mod:`igni_core.extensions.host.broadcasts.push_payloads`), which
  accept a producer's partial payload and keep its extra fields. A
  model instance would refuse both.

So the payload travels as a JSON object, and the typing moves to where
it can still do work: **one factory per channel igni produces**, each
taking that channel's payload model, so a producer cannot pair the
wrong payload with the wrong channel. A channel whose payload no model
of ours names — a broadcast event's, an extension's, a workflow
record's — goes out through :func:`push_on`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from igni_core.protocol.json_value import WireJson
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.envelope import Direction, Message
from igni_core.protocol.schemas.push_payloads import (
    BackgroundProcessDonePayload,
    FileEditedPayload,
    LoginResultPayload,
    LoginStatusPayload,
    ProcessExitedPayload,
    ProcessLinePayload,
    ProcessStartedPayload,
    SchedulerCompletedPayload,
    SchedulerStartedPayload,
    SessionNamedPayload,
)

#: The payload of a push: a JSON object whose shape the channel
#: names. A mapping rather than a model — see the module docstring
#: for why.
PushPayload = Mapping[str, Any]


class PushNotification(Message):
    """A push — the channel, and the payload that channel carries.

    Something happened that a client may want to show, sent without
    being asked. Scheduled tasks, login status, permission-mode
    changes, background processes, file edits and more each have a
    ``channel``; the shape of ``payload`` depends on it. Ignore a
    channel you do not know.

    The ``channel`` field says which kind of event this is; a client
    routes on it to render or dispatch. See :class:`PushChannel` for
    the catalogue of known channels — the producer set is open, so
    treat anything unlisted as a channel this module simply hasn't
    catalogued yet, not as invalid.

    ``channel`` stays a raw ``str`` on the wire (not a strict
    :class:`PushChannel` enum field) so a producer emitting a channel
    the catalogue does not name yet survives verbatim — a strict enum
    with ``_missing_`` → ``UNKNOWN`` would rewrite the value to
    ``"unknown"`` and lose the routing information a client keys on.

    Producers should build their pushes with the ``push_*`` factories
    below rather than by hand: each takes that channel's payload
    model, so the channel and the payload cannot drift apart.
    """

    type: Literal["push_notification"] = "push_notification"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    channel: str = Field(
        default="", description="What kind of push this is; it decides the shape of `payload`."
    )
    payload: dict[str, WireJson] = Field(
        default_factory=dict, description="The push's data, shaped by `channel`."
    )
    # Today: a scheduled task run (``execute_scheduled_task``'s ``run_id``).
    run_id: str = Field(
        default="",
        description="The run whose work sent this push, when igni chose that run's id; empty otherwise.",
    )


def _push(channel: PushChannel, payload: BaseModel) -> PushNotification:
    """A push on ``channel`` carrying ``payload``.

    The payload model is dumped here, at the wire seam, so every
    producer's payload reaches the wire in the same shape and no
    call site has to remember to dump it.
    """
    return PushNotification(channel=channel.value, payload=payload.model_dump())


def push_on(channel: str, payload: PushPayload) -> PushNotification:
    """A push on ``channel`` carrying ``payload`` as it stands.

    The open path — for a channel whose payload shape no model of ours
    names: a broadcast event's own payload, an extension's, a workflow
    record's. The channel may be a :class:`PushChannel` member or a
    plain string.
    """
    return PushNotification(channel=str(channel), payload=payload)


# ── One factory per channel ───────────────────────────────────────
# Each takes that channel's payload model and returns a fully-built
# push, so a producer reads as one line and cannot pair the wrong
# payload with the wrong channel.


def push_login_status(payload: LoginStatusPayload) -> PushNotification:
    """The ``login_status`` push — the login flow's status text."""
    return _push(PushChannel.LOGIN_STATUS, payload)


def push_login_result(payload: LoginResultPayload) -> PushNotification:
    """The ``login_result`` push — how the login attempt ended."""
    return _push(PushChannel.LOGIN_RESULT, payload)


def push_scheduler_started(payload: SchedulerStartedPayload) -> PushNotification:
    """The ``scheduler_started`` push — a scheduled task began."""
    return _push(PushChannel.SCHEDULER_STARTED, payload)


def push_scheduler_completed(payload: SchedulerCompletedPayload) -> PushNotification:
    """The ``scheduler_completed`` push — a scheduled task ended."""
    return _push(PushChannel.SCHEDULER_COMPLETED, payload)


def push_session_named(payload: SessionNamedPayload) -> PushNotification:
    """The ``session_named`` push — a session got its display name."""
    return _push(PushChannel.SESSION_NAMED, payload)


def push_process_started(payload: ProcessStartedPayload) -> PushNotification:
    """The ``process_started`` push — a background process launched."""
    return _push(PushChannel.PROCESS_STARTED, payload)


def push_process_line(payload: ProcessLinePayload) -> PushNotification:
    """The ``process_line`` push — one line of a process's output."""
    return _push(PushChannel.PROCESS_LINE, payload)


def push_process_exited(payload: ProcessExitedPayload) -> PushNotification:
    """The ``process_exited`` push — a background process finished."""
    return _push(PushChannel.PROCESS_EXITED, payload)


def push_background_process_done(payload: BackgroundProcessDonePayload) -> PushNotification:
    """The ``background_process_done`` push — the watcher's completion
    notice."""
    return _push(PushChannel.BACKGROUND_PROCESS_DONE, payload)


def push_file_edited(payload: FileEditedPayload) -> PushNotification:
    """The ``file_edited`` push — a file on disk changed."""
    return _push(PushChannel.FILE_EDITED, payload)


__all__ = [
    "PushNotification",
    "PushPayload",
    "push_background_process_done",
    "push_file_edited",
    "push_login_result",
    "push_login_status",
    "push_on",
    "push_process_exited",
    "push_process_line",
    "push_process_started",
    "push_scheduler_completed",
    "push_scheduler_started",
    "push_session_named",
]
