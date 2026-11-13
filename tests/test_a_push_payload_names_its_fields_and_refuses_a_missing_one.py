"""The push payload models: which fields a producer must give, which may be
null, and how a push carries a payload it is handed."""

from __future__ import annotations

from types import MappingProxyType

import pytest
from pydantic import ValidationError

from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.push import (
    PushNotification,
    push_background_process_done,
    push_file_edited,
    push_on,
    push_process_exited,
    push_process_line,
    push_process_started,
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

REQUIRED = {
    LoginResultPayload: {"success", "result"},
    LoginStatusPayload: {"text"},
    SchedulerStartedPayload: {"task_id", "description"},
    SchedulerCompletedPayload: {"task_id", "description", "result"},
    BackgroundProcessDonePayload: {"pid", "cmd", "exit_code", "duration_seconds"},
    FileEditedPayload: {"path"},
    ProcessStartedPayload: {"pid", "cmd", "started_at"},
    ProcessLinePayload: {"pid", "line"},
    ProcessExitedPayload: {"pid", "cmd", "exit_code", "duration_seconds"},
    SessionNamedPayload: {"session_id", "name"},
}


_VALID = {
    "success": True,
    "result": "r",
    "text": "t",
    "task_id": "t1",
    "description": "d",
    "pid": 1,
    "cmd": "c",
    "exit_code": 0,
    "duration_seconds": 1.0,
    "path": "/p",
    "started_at": 1.0,
    "line": "l",
    "session_id": "s",
    "name": "n",
}


@pytest.mark.parametrize("model", list(REQUIRED), ids=lambda m: m.__name__)
def test_a_producer_that_leaves_a_field_out_is_refused(model):
    """Even a field that may be null must be given: leaving it out is a
    producer's mistake, not an unknown."""
    full = {name: _VALID[name] for name in REQUIRED[model]}
    model.model_validate(full)
    for missing in REQUIRED[model]:
        with pytest.raises(ValidationError):
            model.model_validate({k: v for k, v in full.items() if k != missing})


def test_each_model_asks_for_exactly_the_fields_it_publishes():
    for model, required in REQUIRED.items():
        needed = {name for name, info in model.model_fields.items() if info.is_required()}
        assert needed - {"flow_id"} == required, model.__name__


class TestTheNullableFieldsAcceptNullAndWriteIt:
    def test_a_process_with_nothing_known_is_all_null(self):
        push = push_process_started(ProcessStartedPayload(pid=None, cmd=None, started_at=None))

        assert push.payload == {"pid": None, "cmd": None, "started_at": None}

    def test_a_line_may_be_null(self):
        push = push_process_line(ProcessLinePayload(pid=None, line=None))

        assert push.payload == {"pid": None, "line": None}

    def test_an_exit_may_be_unknown_in_every_field(self):
        push = push_process_exited(
            ProcessExitedPayload(pid=None, cmd=None, exit_code=None, duration_seconds=None)
        )

        assert push.payload == {
            "pid": None,
            "cmd": None,
            "exit_code": None,
            "duration_seconds": None,
        }

    def test_the_watcher_notice_may_not_have_a_null_command_or_duration(self):
        with pytest.raises(ValidationError):
            BackgroundProcessDonePayload(pid=1, cmd=None, exit_code=0, duration_seconds=1.0)
        with pytest.raises(ValidationError):
            BackgroundProcessDonePayload(pid=1, cmd="c", exit_code=0, duration_seconds=None)

    def test_the_watcher_notice_may_not_know_its_pid_or_exit_code(self):
        push = push_background_process_done(
            BackgroundProcessDonePayload(pid=None, cmd="c", exit_code=None, duration_seconds=0)
        )

        assert push.payload == {
            "pid": None,
            "cmd": "c",
            "exit_code": None,
            "duration_seconds": 0.0,
        }


class TestTheValuesAreTypedAsTheClientReadsThem:
    def test_an_exit_code_that_is_not_a_number_is_refused(self):
        with pytest.raises(ValidationError):
            ProcessExitedPayload(pid=1, cmd="c", exit_code="zero", duration_seconds=1)

    def test_a_whole_number_start_time_is_carried_as_a_float(self):
        push = push_process_started(ProcessStartedPayload(pid=1, cmd="c", started_at=3))

        assert push.payload["started_at"] == 3.0
        assert isinstance(push.payload["started_at"], float)

    def test_unicode_paths_and_lines_survive(self):
        assert push_file_edited(FileEditedPayload(path="/p/é☃.py")).payload == {"path": "/p/é☃.py"}
        assert push_process_line(ProcessLinePayload(pid=1, line="ok ✓")).payload["line"] == ("ok ✓")

    def test_a_field_the_model_does_not_name_is_left_out_of_the_push(self):
        push = push_file_edited(FileEditedPayload(path="/p", surprise=1))  # type: ignore[call-arg]

        assert push.payload == {"path": "/p"}

    def test_a_login_flow_id_defaults_to_empty(self):
        assert LoginStatusPayload(text="t").flow_id == ""
        assert LoginResultPayload(success=False, result="why").flow_id == ""


class TestAPushCarriesWhatItIsHanded:
    def test_a_read_only_mapping_goes_out_as_a_plain_dict(self):
        push = push_on("c", MappingProxyType({"a": 1}))  # type: ignore[arg-type]

        assert type(push.payload) is dict
        assert push.payload == {"a": 1}

    def test_a_payload_that_is_not_an_object_is_refused(self):
        with pytest.raises(ValidationError):
            push_on("c", [1, 2])  # type: ignore[arg-type]

    def test_an_empty_payload_is_an_empty_object(self):
        assert push_on(PushChannel.FILE_EDITED, {}).payload == {}

    def test_nested_values_and_nulls_survive(self):
        payload = {"a": {"b": [1, None, "é"]}, "c": None}

        assert push_on("c", payload).payload == payload

    def test_the_run_a_push_belongs_to_survives_the_wire(self):
        push = PushNotification(channel="scheduler_started", run_id="run-9")

        again = PushNotification.model_validate_json(push.model_dump_json())

        assert again.run_id == "run-9"
        assert PushNotification().run_id == ""

    def test_a_catalogue_member_is_sent_as_its_word(self):
        push = push_on(PushChannel.WORKFLOW_EVENT, {})

        assert type(push.channel) is str
        assert push.channel == "workflow_event"
