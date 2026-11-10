"""BE→FE push-notification wire schema.

:class:`PushNotification` carries a :class:`PushChannel` discriminator
+ a ``payload: dict[str, Any]``. The payload is kept as a dict (not
a discriminated union of Pydantic models) because push channels are
declared across many producer sites in the backend that each own
their own typed payload model in :mod:`schemas_push` /
:mod:`schemas_scheduler` / etc. — those producers already dump their
typed models to ``dict`` before construction, and forcing a
discriminated union here would require every producer to know the
push channel's payload model type at the push seam (they don't;
each callsite owns its own typed model and dumps at the seam).

What this module DOES give:

* :class:`PushChannel` — a catalogue of the channels we know about,
  so the producer sites we control reference a symbol instead of a
  literal. It is NOT a closed set: ``channel`` stays a raw ``str``
  on the wire (see the field comment) because ``push_bridge`` and
  the session broadcast bus forward channel strings this module
  never sees.
* Factory helpers :func:`push_scheduler_started`,
  :func:`push_login_status`, :func:`push_permission_mode_changed`,
  etc. — collapse the ``PushNotification(channel="foo",
  payload=X.model_dump())`` boilerplate at every producer callsite
  into a one-liner that takes the typed payload directly.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from igni_core.protocol.json_value import WireJson
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.envelope import Direction, Message


class PushNotification(Message):
    """Something happened that a client may want to show, sent without being asked.

    Scheduled tasks, login status, permission-mode changes, background
    processes, file edits and more each have a ``channel``; the shape of
    ``payload`` depends on it. Ignore a channel you do not know.
    """

    # Producers should prefer the ``push_*`` factory helpers over
    # hand-building the class.

    # ``channel`` stays a raw ``str`` on the wire (not a strict
    # :class:`PushChannel` enum field) so producer sites that emit
    # a channel value we haven't yet enumerated in the enum survive
    # verbatim through Pydantic — a strict enum + ``_missing_`` →
    # ``UNKNOWN`` would rewrite the value to ``"unknown"`` and lose
    # the routing info the FE keys on. Producers SHOULD use the
    # :func:`push_*` factory helpers (which pass a :class:`PushChannel`
    # member); the raw-string path is the compatibility bridge.
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


def _push(channel: PushChannel, payload: Any) -> PushNotification:
    """Internal factory: coerce a typed payload model (or a raw
    dict for edge-case producers) into a :class:`PushNotification`
    on ``channel``.

    Extracted so every ``push_*`` public helper has the same
    ``.model_dump()`` fallback path.
    """
    if hasattr(payload, "model_dump"):
        data = payload.model_dump()
    elif isinstance(payload, dict):
        data = payload
    else:  # pragma: no cover - defensive; producers should pass models/dicts
        data = {"value": payload}
    return PushNotification(channel=channel, payload=data)


# ── Public factory helpers ────────────────────────────────────────
# One per channel; each takes a typed payload model (or dict for
# the freeform channels) and returns a fully-built ``PushNotification``.
# The producer callsite reads as one line instead of
# ``msg.PushNotification(channel="…", payload=X.model_dump())``.


def push_scheduler_started(payload: Any) -> PushNotification:
    """``SchedulerStartedPayload`` → wire push."""
    return _push(PushChannel.SCHEDULER_STARTED, payload)


def push_scheduler_completed(payload: Any) -> PushNotification:
    """``SchedulerCompletedPayload`` → wire push."""
    return _push(PushChannel.SCHEDULER_COMPLETED, payload)


def push_login_status(payload: Any) -> PushNotification:
    """``LoginStatusPayload`` → wire push."""
    return _push(PushChannel.LOGIN_STATUS, payload)


def push_login_result(payload: Any) -> PushNotification:
    """``LoginResultPayload`` → wire push."""
    return _push(PushChannel.LOGIN_RESULT, payload)


def push_session_named(payload: Any) -> PushNotification:
    """``SessionNamedPayload`` → wire push."""
    return _push(PushChannel.SESSION_NAMED, payload)


def push_permission_mode_changed(payload: Any) -> PushNotification:
    """``PermissionModeChangedPayload`` → wire push."""
    return _push(PushChannel.PERMISSION_MODE_CHANGED, payload)


def push_file_edited(payload: Any) -> PushNotification:
    """``FileEditedPayload`` → wire push."""
    return _push(PushChannel.FILE_EDITED, payload)


def push_process_started(payload: Any) -> PushNotification:
    """``ProcessStartedPayload`` → wire push."""
    return _push(PushChannel.PROCESS_STARTED, payload)


def push_process_line(payload: Any) -> PushNotification:
    """``ProcessLinePayload`` → wire push."""
    return _push(PushChannel.PROCESS_LINE, payload)


def push_process_exited(payload: Any) -> PushNotification:
    """``ProcessExitedPayload`` → wire push."""
    return _push(PushChannel.PROCESS_EXITED, payload)


def push_background_process_done(payload: Any) -> PushNotification:
    """``BackgroundProcessDonePayload`` → wire push."""
    return _push(PushChannel.BACKGROUND_PROCESS_DONE, payload)


def push_orchestrate_event(payload: dict) -> PushNotification:
    """Freeform orchestrate event dict → wire push."""
    return _push(PushChannel.ORCHESTRATE_EVENT, payload)


__all__ = [
    "PushNotification",
    "push_scheduler_started",
    "push_scheduler_completed",
    "push_login_status",
    "push_login_result",
    "push_session_named",
    "push_permission_mode_changed",
    "push_file_edited",
    "push_process_started",
    "push_process_line",
    "push_process_exited",
    "push_background_process_done",
    "push_orchestrate_event",
]
