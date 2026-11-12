"""The lifecycle schemas: the interrupted-run note, the Agno run snapshot,
the rehydrate outcomes and their log line, the shutdown result and the
launch record."""

from __future__ import annotations

import enum
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from igni_core.backend.schemas_lifecycle import (
    AgnoRunSnapshot,
    BackendLaunch,
    InterruptedRunSummary,
    RehydrateOutcome,
    RehydrateOutcomeSet,
    SessionEndPayload,
    ShutdownResult,
)


def test_the_recap_alone_ends_by_saying_the_user_has_not_written():
    text = InterruptedRunSummary(recap="It stopped.").render(awaiting_user=True)
    assert text == f"It stopped. {InterruptedRunSummary.AWAITING_USER}"
    assert "has not yet sent a new message" in text


def test_the_recap_with_a_message_never_says_the_user_has_not_written():
    text = InterruptedRunSummary(recap="It stopped.").render(awaiting_user=False)
    assert text == f"It stopped. {InterruptedRunSummary.WITH_USER_MESSAGE}"
    assert "Their message below was sent after that" in text
    assert "has not yet sent" not in text


def test_the_recap_itself_carries_no_closing_line():
    summary = InterruptedRunSummary(recap="r")
    assert summary.recap == "r"
    assert summary.pending_ids_to_drop == []
    other = InterruptedRunSummary(recap="r")
    other.pending_ids_to_drop.append("x")
    assert summary.pending_ids_to_drop == []


class _Status(enum.Enum):
    RUNNING = "RUNNING"


def test_a_snapshot_reads_id_enum_status_tool_names_and_content():
    run = SimpleNamespace(
        run_id=123,
        status=_Status.RUNNING,
        tools=[
            SimpleNamespace(tool_name="shell"),
            SimpleNamespace(tool_name=None),
            SimpleNamespace(),
        ],
        content="hello",
    )
    snap = AgnoRunSnapshot.from_agno_run(run)
    assert snap.run_id == "123"
    assert snap.status == "RUNNING"
    assert snap.tool_names == ["shell", "?", "?"]
    assert snap.content_preview == "hello"


def test_a_snapshot_keeps_a_plain_string_status():
    assert AgnoRunSnapshot.from_agno_run(SimpleNamespace(status="paused")).status == "paused"


def test_a_snapshot_of_a_bare_object_is_empty_without_crashing():
    snap = AgnoRunSnapshot.from_agno_run(object())
    assert (snap.run_id, snap.status, snap.tool_names, snap.content_preview) == (
        None,
        None,
        [],
        "",
    )


@pytest.mark.parametrize("tools", [None, []])
def test_a_run_with_no_tools_has_no_tool_names(tools):
    assert AgnoRunSnapshot.from_agno_run(SimpleNamespace(tools=tools)).tool_names == []


@pytest.mark.parametrize(("size", "kept"), [(399, 399), (400, 400), (401, 400)])
def test_the_content_preview_is_cut_at_four_hundred_characters(size, kept):
    snap = AgnoRunSnapshot.from_agno_run(SimpleNamespace(content="x" * size))
    assert len(snap.content_preview) == kept


def test_the_cut_counts_characters_not_bytes():
    snap = AgnoRunSnapshot.from_agno_run(SimpleNamespace(content="é" * 500))
    assert snap.content_preview == "é" * 400


def test_content_that_is_not_text_is_stringified_before_the_cut():
    snap = AgnoRunSnapshot.from_agno_run(SimpleNamespace(content=[1, 2]))
    assert snap.content_preview == "[1, 2]"


def test_the_session_end_payload_carries_the_session_id():
    assert SessionEndPayload(session_id="s1").model_dump() == {"session_id": "s1"}


def test_a_rehydrate_outcome_has_one_of_six_steps():
    steps = [
        "event_log",
        "orphan_processes",
        "plan_decisions",
        "todos",
        "plan_store",
        "queued_messages",
    ]
    for step in steps:
        assert RehydrateOutcome(ok=True, step=step).reason is None
    with pytest.raises(ValidationError):
        RehydrateOutcome(ok=True, step="nonsense")


def test_a_fresh_shutdown_result_has_done_nothing_and_failed_nowhere():
    result = ShutdownResult()
    assert result.model_dump() == {
        "hook_fired": False,
        "pool_cleaned": False,
        "mcp_disconnected": False,
        "scheduler_stopped": False,
        "errors": [],
    }


def test_each_mark_sets_only_its_own_flag():
    cases = {
        "mark_hook_fired": "hook_fired",
        "mark_pool_cleaned": "pool_cleaned",
        "mark_mcp_disconnected": "mcp_disconnected",
        "mark_scheduler_stopped": "scheduler_stopped",
    }
    for method, flag in cases.items():
        result = ShutdownResult()
        getattr(result, method)()
        flags = {k: v for k, v in result.model_dump().items() if k != "errors"}
        assert flags == {k: k == flag for k in cases.values()}


def test_a_shutdown_error_is_recorded_with_its_label():
    result = ShutdownResult()
    result.record_error("pool", RuntimeError("down"))
    result.record_error("mcp", ValueError())
    assert result.errors == ["pool: down", "mcp: "]
    assert ShutdownResult().errors == []


def test_a_shutdown_result_no_longer_counts_killed_shell_processes():
    assert "shell_processes_killed" not in ShutdownResult.model_fields
    assert not hasattr(ShutdownResult(), "record_shell_kills")


def test_the_outcome_set_keeps_only_real_outcomes():
    real = RehydrateOutcome(ok=True, step="todos")
    kept = RehydrateOutcomeSet.of(real, None, object(), {"ok": True}, "x")
    assert kept.outcomes == [real]


def test_a_clean_startup_logs_one_info_line(caplog):
    outcomes = RehydrateOutcomeSet.of(
        RehydrateOutcome(ok=True, step="todos"), RehydrateOutcome(ok=True, step="event_log")
    )
    with caplog.at_level(logging.DEBUG, logger="t.rehydrate"):
        outcomes.log(logging.getLogger("t.rehydrate"))
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.INFO, "Rehydrate: 2 step(s) ok")
    ]


def test_no_outcomes_still_log_an_ok_line(caplog):
    with caplog.at_level(logging.DEBUG, logger="t.rehydrate"):
        RehydrateOutcomeSet.of().log(logging.getLogger("t.rehydrate"))
    assert [r.getMessage() for r in caplog.records] == ["Rehydrate: 0 step(s) ok"]


def test_failed_steps_log_one_warning_naming_each_with_its_reason(caplog):
    outcomes = RehydrateOutcomeSet.of(
        RehydrateOutcome(ok=True, step="todos"),
        RehydrateOutcome(ok=False, step="event_log", reason="disk"),
        RehydrateOutcome(ok=False, step="plan_store"),
    )
    with caplog.at_level(logging.DEBUG, logger="t.rehydrate"):
        outcomes.log(logging.getLogger("t.rehydrate"))
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "Rehydrate: 2/3 step(s) failed — event_log=disk, plan_store=None")
    ]


def test_a_launch_needs_a_project_dir_and_defaults_the_rest():
    with pytest.raises(ValidationError):
        BackendLaunch()
    launch = BackendLaunch(project_dir="/work")
    assert launch.project_dir == Path("/work")
    assert (launch.socket_path, launch.ws_port, launch.ws_token) == (None, None, None)
    assert (launch.resume_session_id, launch.additional_dirs) == (None, None)


def test_a_launch_carries_both_transports_the_token_and_the_extra_dirs():
    launch = BackendLaunch(
        socket_path="/s.sock",
        ws_port=9,
        ws_token="tok",
        project_dir="/w",
        resume_session_id="sess",
        additional_dirs=["/a", "/b"],
    )
    assert (launch.socket_path, launch.ws_port, launch.ws_token) == ("/s.sock", 9, "tok")
    assert launch.additional_dirs == [Path("/a"), Path("/b")]
    assert launch.resume_session_id == "sess"


def test_a_launch_cannot_be_changed():
    launch = BackendLaunch(project_dir="/w")
    with pytest.raises(ValidationError):
        launch.ws_token = "other"
