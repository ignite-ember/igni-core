"""A push is one channel and one payload — and the two are bound together.

The wire message behind every push used to be built by hand:
``PushNotification(channel="login_status", payload=X.model_dump())``,
with a helper that duck-typed the payload (``hasattr(payload,
"model_dump")``) and wrapped anything else as ``{"value": payload}``.
A producer could name the wrong channel, or pass a payload whose shape
did not fit it, and nothing said so until the frontend read it.

The module now has **one factory per channel igni produces**, each
taking that channel's payload model, so a producer cannot pair the
wrong payload with the wrong channel. The open channels — a broadcast
event's, a workflow record's — go out through ``push_on``.

What the payload *is* stays a mapping, and that is deliberate: the
extension host parses a push's payload with ``model_validate`` against
deliberately lenient readers, which refuse a model instance. The
tests below pin that, because the day someone "improves" the payload
into a union of models, the extension host breaks in a way no unit
test of the producer would notice.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from igni_core.extensions.host.broadcasts.push_payloads import (
    NotificationsPush,
    SessionNamedPush,
)
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.push import (
    PushNotification,
    push_background_process_done,
    push_file_edited,
    push_login_result,
    push_login_status,
    push_on,
    push_process_exited,
    push_process_line,
    push_process_started,
    push_scheduler_completed,
    push_scheduler_started,
    push_session_named,
)
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
from igni_core.protocol.schemas.wire import WireMessage

# ── One factory per channel ───────────────────────────────────────
# (factory, payload model, channel, payload keyword arguments)


class _Case:
    """One factory and the channel it must produce."""

    def __init__(
        self,
        factory,
        payload_model,
        channel: PushChannel,
        payload_kwargs: dict,
    ) -> None:
        self.factory = factory
        self.payload_model = payload_model
        self.channel = channel
        self.payload_kwargs = payload_kwargs


CASES = [
    _Case(
        push_login_status,
        LoginStatusPayload,
        PushChannel.LOGIN_STATUS,
        {"text": "checking your credentials"},
    ),
    _Case(
        push_login_result,
        LoginResultPayload,
        PushChannel.LOGIN_RESULT,
        {"success": True, "result": "logged in"},
    ),
    _Case(
        push_scheduler_started,
        SchedulerStartedPayload,
        PushChannel.SCHEDULER_STARTED,
        {"task_id": "t-1", "description": "nightly build"},
    ),
    _Case(
        push_scheduler_completed,
        SchedulerCompletedPayload,
        PushChannel.SCHEDULER_COMPLETED,
        {"task_id": "t-1", "description": "nightly build", "result": "completed"},
    ),
    _Case(
        push_session_named,
        SessionNamedPayload,
        PushChannel.SESSION_NAMED,
        {"session_id": "s-1", "name": "Refactor the parser"},
    ),
    _Case(
        push_process_started,
        ProcessStartedPayload,
        PushChannel.PROCESS_STARTED,
        {"pid": 42, "cmd": "pytest", "started_at": 12.5},
    ),
    _Case(
        push_process_line,
        ProcessLinePayload,
        PushChannel.PROCESS_LINE,
        {"pid": 42, "line": "1 passed"},
    ),
    _Case(
        push_process_exited,
        ProcessExitedPayload,
        PushChannel.PROCESS_EXITED,
        {"pid": 42, "cmd": "pytest", "exit_code": 0, "duration_seconds": 1.25},
    ),
    _Case(
        push_background_process_done,
        BackgroundProcessDonePayload,
        PushChannel.BACKGROUND_PROCESS_DONE,
        {"pid": 42, "cmd": "pytest", "exit_code": 0, "duration_seconds": 1.25},
    ),
    _Case(
        push_file_edited,
        FileEditedPayload,
        PushChannel.FILE_EDITED,
        {"path": "/project/main.py"},
    ),
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.channel.value)
class TestEachFactoryBindsItsChannelToItsPayload:
    def test_the_channel_is_the_one_the_factory_names(self, case: _Case):
        push = case.factory(case.payload_model(**case.payload_kwargs))

        assert push.channel == case.channel.value

    def test_the_payload_is_the_model_dumped(self, case: _Case):
        push = case.factory(case.payload_model(**case.payload_kwargs))

        assert push.payload == case.payload_model(**case.payload_kwargs).model_dump()

    def test_the_payload_is_a_plain_dict(self, case: _Case):
        """Not a model instance — see the module docstring."""
        push = case.factory(case.payload_model(**case.payload_kwargs))

        assert type(push.payload) is dict

    def test_the_push_is_a_push_notification(self, case: _Case):
        push = case.factory(case.payload_model(**case.payload_kwargs))

        assert isinstance(push, PushNotification)
        assert push.type == "push_notification"


def test_every_channel_with_a_payload_model_has_a_factory():
    """A producer set that grows a payload model grows a factory.

    The ten models in ``push_payloads`` are the channels igni produces
    with a shape of its own; each one has a factory that takes it, so
    the pairing is spelled out once and cannot drift.
    """
    from igni_core.protocol.schemas import push as push_module
    from igni_core.protocol.schemas import push_payloads as payload_module

    models = set(payload_module.__all__)
    factories = {
        name for name in dir(push_module) if name.startswith("push_") and name != "push_on"
    }

    assert models == {
        "BackgroundProcessDonePayload",
        "FileEditedPayload",
        "LoginResultPayload",
        "LoginStatusPayload",
        "ProcessExitedPayload",
        "ProcessLinePayload",
        "ProcessStartedPayload",
        "SchedulerCompletedPayload",
        "SchedulerStartedPayload",
        "SessionNamedPayload",
    }
    assert factories == {
        "push_background_process_done",
        "push_file_edited",
        "push_login_result",
        "push_login_status",
        "push_process_exited",
        "push_process_line",
        "push_process_started",
        "push_scheduler_completed",
        "push_scheduler_started",
        "push_session_named",
    }


# ── The open channel ──────────────────────────────────────────────


class TestTheOpenChannel:
    def test_a_plain_channel_name_survives_verbatim(self):
        """A channel igni has not catalogued yet must not be rewritten."""
        push = push_on("a_channel_nobody_catalogued", {"anything": 1})

        assert push.channel == "a_channel_nobody_catalogued"

    def test_a_catalogue_member_may_be_passed(self):
        push = push_on(PushChannel.WORKFLOW_EVENT, {"type": "step"})

        assert push.channel == "workflow_event"

    def test_the_payload_travels_as_it_stands(self):
        push = push_on(PushChannel.WORKFLOW_EVENT, {"type": "step", "n": 3})

        assert push.payload == {"type": "step", "n": 3}

    def test_an_uncatalogued_channel_is_not_rewritten_to_unknown(self):
        """``PushChannel`` maps an unknown value to ``UNKNOWN`` — but the
        wire field is a plain string, so the name survives. The frontend
        routes on it; rewriting it would lose the routing."""
        push = push_on("brand_new_channel", {})

        assert push.channel != PushChannel.UNKNOWN.value
        assert push.channel == "brand_new_channel"


# ── The message itself ────────────────────────────────────────────


class TestTheMessage:
    def test_the_defaults_are_empty(self):
        push = PushNotification()

        assert push.channel == ""
        assert push.payload == {}

    def test_the_payload_defaults_to_a_fresh_dict(self):
        """Two pushes must not share one payload dict."""
        first = PushNotification()
        second = PushNotification()

        first.payload["a"] = 1

        assert second.payload == {}

    def test_the_push_survives_the_wire_round_trip(self):
        push = push_session_named(SessionNamedPayload(session_id="s-1", name="n"))

        restored = PushNotification.model_validate(push.model_dump())

        assert restored == push

    def test_the_push_is_on_the_wire_contract(self):
        adapter = TypeAdapter(WireMessage)
        push = push_login_status(LoginStatusPayload(text="hi"))

        restored = adapter.validate_python(push.model_dump(mode="json"))

        assert isinstance(restored, PushNotification)
        assert restored.channel == "login_status"


# ── The extension host can still read it ──────────────────────────


class TestTheExtensionHostCanStillReadThePayload:
    """The reason the payload stays a mapping.

    The host's readers are deliberately lenient — they accept a
    producer's partial payload and keep its extra fields — but they
    are still *models*, and ``model_validate`` refuses a model
    instance. A payload typed as a union of producer models would
    arrive at the host as a model, and every host reader would raise.
    """

    def test_the_host_reads_a_producers_payload(self):
        push = push_session_named(SessionNamedPayload(session_id="s-1", name="Refactor"))

        read = SessionNamedPush.model_validate(push.payload)

        assert read.session_id == "s-1"
        assert read.name == "Refactor"

    def test_the_host_reads_a_payload_it_was_never_told_about(self):
        """A channel whose shape no producer model names."""
        push = push_on(PushChannel.NOTIFICATIONS_CHANGED, {"counts": {"unread": 2}})

        read = NotificationsPush.model_validate(push.payload)

        assert read.counts is not None
