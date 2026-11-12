"""The two sets every run-starting call declares, in the contract the SDKs read.

``RUN_EVENTS`` is what a call that starts or resumes a run streams to the
windows; ``TOOL_PUSHES`` is what the agent's tools may push while that work
goes on. A member dropped from either leaves the SDK-generated client unable
to follow the run (or the push) it will in fact receive, so both are held by
wire name and by the methods that declare them.
"""

from __future__ import annotations

import pytest

from igni_core.backend.rpc_streams import RUN_EVENTS, TOOL_PUSHES
from igni_core.protocol import rpc_contract
from igni_core.protocol.schemas.enums import PushChannel


def _wire(stream: object) -> str:
    if isinstance(stream, PushChannel):
        return f"push_notification:{stream.value}"
    return rpc_contract._wire_type(stream)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def contract():
    return rpc_contract.build()


def test_the_run_events_are_the_messages_a_run_streams_from_start_to_end() -> None:
    assert [_wire(s) for s in RUN_EVENTS] == [
        "run_started",
        "content_delta",
        "reasoning_started",
        "tool_started",
        "tool_completed",
        "tool_error",
        "model_completed",
        "task_created",
        "task_updated",
        "task_iteration",
        "task_state_updated",
        "run_paused",
        "requirement_details",
        "run_completed",
        "run_error",
        "streaming_done",
        "info",
        "error",
        "stream_end",
    ]


def test_the_tool_pushes_are_the_channels_a_tool_may_push_during_work() -> None:
    assert [_wire(s) for s in TOOL_PUSHES] == [
        "push_notification:todos_updated",
        "push_notification:file_edited",
        "push_notification:process_started",
        "push_notification:process_line",
        "push_notification:process_exited",
        "push_notification:background_process_done",
        "push_notification:orchestrate_event",
        "push_notification:http_retry_attempt",
        "push_notification:http_retry_succeeded",
        "push_notification:permission_mode_changed",
        "push_notification:plan_submitted",
    ]


def test_neither_set_names_a_member_twice() -> None:
    assert len(set(RUN_EVENTS)) == len(RUN_EVENTS)
    assert len(set(TOOL_PUSHES)) == len(TOOL_PUSHES)


def test_attaching_a_session_streams_exactly_the_run_events_and_may_push_the_tools() -> None:
    entry = rpc_contract.build()["methods"]["attach_session"]

    assert set(entry["streams"]) == {_wire(s) for s in RUN_EVENTS}
    assert set(entry["may_stream"]) == {_wire(s) for s in TOOL_PUSHES}


def test_hosting_a_page_streams_the_run_events_and_may_push_the_tools(contract) -> None:
    entry = contract["methods"]["host_model"]

    assert {_wire(s) for s in RUN_EVENTS} <= set(entry["streams"])
    assert {_wire(s) for s in TOOL_PUSHES} == set(entry["may_stream"])


def test_the_scheduler_may_push_what_a_tool_pushes(contract) -> None:
    pushes = {_wire(s) for s in TOOL_PUSHES}
    assert set(contract["methods"]["execute_scheduled_task"]["may_stream"]) == pushes
    assert set(contract["methods"]["start_scheduler"]["may_stream"]) == pushes
