"""What each ``/schedule`` answer says, built from the view models directly,
and what the task hooks are handed."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from igni_core.backend.schemas_scheduler import (
    ScheduleUsageView,
    TaskAlreadyDoneView,
    TaskCancelledView,
    TaskCompletedPayload,
    TaskCreatedPayload,
    TaskDetailsView,
    TaskNotFoundView,
    TaskScheduledView,
)
from igni_core.core.scheduler.models import ScheduledTask, TaskStatus
from igni_core.protocol.messages import CommandAction, CommandResultKind


def _task(**overrides) -> ScheduledTask:
    fields = {
        "id": "ab12cd34",
        "description": "run tests",
        "scheduled_at": datetime(2030, 1, 2, 17, 5, 59),
        "created_at": datetime(2029, 12, 31, 8, 9, 10),
        "status": TaskStatus.completed,
    }
    fields.update(overrides)
    return ScheduledTask(**fields)


def test_a_one_shot_task_is_reported_with_its_time_and_opens_the_panel():
    result = TaskScheduledView(
        task_id="t1", description="run tests", scheduled_at="2030-01-02 17:05"
    ).to_command_result()
    assert result.kind == CommandResultKind.INFO
    assert result.action == CommandAction.SCHEDULE
    assert result.content == 'Scheduled `t1`: "run tests" at 2030-01-02 17:05'


def test_a_recurring_task_is_reported_with_its_recurrence_and_first_run():
    result = TaskScheduledView(
        task_id="t1",
        description="run tests",
        scheduled_at="2030-01-02 17:05",
        recurrence="every 2 hours",
    ).to_command_result()
    assert result.kind == CommandResultKind.INFO
    assert result.action == CommandAction.SCHEDULE
    assert result.content == (
        'Scheduled `t1`: "run tests" (every 2 hours, first at 2030-01-02 17:05)'
    )


def test_a_description_with_markdown_and_quotes_is_quoted_verbatim():
    result = TaskScheduledView(
        task_id="t1", description='say "héllo" `x` *y*', scheduled_at="2030-01-02 17:05"
    ).to_command_result()
    assert result.content == 'Scheduled `t1`: "say "héllo" `x` *y*" at 2030-01-02 17:05'


def test_a_cancelled_task_is_acknowledged_without_an_action():
    result = TaskCancelledView(task_id="t1").to_command_result()
    assert result.kind == CommandResultKind.INFO
    assert result.action == CommandAction.NONE
    assert result.content == "Cancelled task t1"


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_a_finished_task_says_which_state_it_is_already_in(status):
    result = TaskAlreadyDoneView(task_id="t1", status=TaskStatus(status)).to_command_result()
    assert result.kind == CommandResultKind.INFO
    assert result.content == f"Task t1 is already {status}"


def test_an_unknown_task_is_an_error_naming_the_id():
    result = TaskNotFoundView(task_id="nope").to_command_result()
    assert result.is_error()
    assert result.content == "Task not found: nope"


def test_the_task_card_lists_its_fields_and_no_tails_when_there_is_no_outcome():
    result = TaskDetailsView(task=_task()).to_command_result()
    assert result.kind == CommandResultKind.MARKDOWN
    assert result.content == (
        "## Task ab12cd34\n"
        "- **Description:** run tests\n"
        "- **Scheduled:** 2030-01-02 17:05\n"
        "- **Status:** completed\n"
        "- **Created:** 2029-12-31 08:09\n"
    )


def test_the_task_card_appends_a_result_block_only_when_there_is_a_result():
    result = TaskDetailsView(task=_task(result="all green")).to_command_result()
    assert result.content.endswith("\n\n**Result:**\nall green\n")
    assert "**Error:**" not in result.content


def test_the_task_card_appends_an_error_block_only_when_there_is_an_error():
    task = _task(status=TaskStatus.failed, error="boom")
    result = TaskDetailsView(task=task).to_command_result()
    assert "- **Status:** failed\n" in result.content
    assert result.content.endswith("\n\n**Error:**\nboom\n")
    assert "**Result:**" not in result.content


def test_the_task_card_shows_result_then_error_when_both_exist():
    result = TaskDetailsView(task=_task(result="partial", error="then boom")).to_command_result()
    assert result.content.endswith("\n\n**Result:**\npartial\n\n**Error:**\nthen boom\n")


def test_an_unparsable_phrase_answers_with_usage_and_every_example():
    result = ScheduleUsageView.to_command_result()
    assert result.is_error()
    assert result.content.startswith("Could not parse the time clause.")
    for example in (
        "/schedule review the codebase at 5pm",
        "/schedule run tests in 30 minutes",
        "/schedule audit security tomorrow",
        "/schedule run tests every 2 hours",
        "/schedule check dependencies daily",
    ):
        assert f"  {example}" in result.content


def test_a_task_start_hook_payload_needs_all_three_fields():
    payload = TaskCreatedPayload(session_id="s", task_id="t", description="d")
    assert payload.model_dump() == {"session_id": "s", "task_id": "t", "description": "d"}
    with pytest.raises(ValidationError):
        TaskCreatedPayload.model_validate({"session_id": "s", "task_id": "t"})


@pytest.mark.parametrize("status", ["completed", "error"])
def test_a_task_end_hook_payload_carries_its_outcome(status):
    payload = TaskCompletedPayload(session_id="s", task_id="t", description="d", status=status)
    assert payload.model_dump()["status"] == status


@pytest.mark.parametrize("status", ["failed", "cancelled", "", "Completed"])
def test_a_task_end_hook_payload_refuses_any_other_status(status):
    with pytest.raises(ValidationError):
        TaskCompletedPayload(session_id="s", task_id="t", description="d", status=status)
