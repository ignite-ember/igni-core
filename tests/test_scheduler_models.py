"""The scheduler's domain types and the row that holds them."""

import logging
import re
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from igni_core.core.scheduler.db_models import ScheduledTaskModel
from igni_core.core.scheduler.models import ScheduledTask, TaskStatus
from igni_core.core.scheduler.recurrence import RecurrenceUnit

_WHEN = datetime(2030, 1, 2, 3, 4, 5)


class TestTaskStatus:
    def test_the_five_statuses_and_their_wire_values(self):
        assert {s.name: s.value for s in TaskStatus} == {
            "pending": "pending",
            "running": "running",
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
        }

    @pytest.mark.parametrize(
        ("status", "active"),
        [
            (TaskStatus.pending, True),
            (TaskStatus.running, True),
            (TaskStatus.completed, False),
            (TaskStatus.failed, False),
            (TaskStatus.cancelled, False),
        ],
    )
    def test_only_pending_and_running_are_active(self, status, active):
        assert status.is_active is active

    def test_an_unknown_status_string_is_refused(self):
        with pytest.raises(ValueError):
            TaskStatus("paused")


class TestScheduledTask:
    def test_a_new_task_is_pending_with_an_eight_hex_id(self):
        task = ScheduledTask.new("do a thing", _WHEN)

        assert re.fullmatch(r"[0-9a-f]{8}", task.id)
        assert (task.description, task.scheduled_at) == ("do a thing", _WHEN)
        assert task.status is TaskStatus.pending
        assert (task.result, task.error, task.recurrence) == ("", "", "")
        assert task.recurrence_obj is None
        assert abs(task.created_at - datetime.now()) < timedelta(seconds=5)

    def test_two_new_tasks_get_different_ids(self):
        assert len({ScheduledTask.new("x", _WHEN).id for _ in range(50)}) == 50

    def test_an_empty_description_is_accepted(self):
        assert ScheduledTask.new("", _WHEN).description == ""

    def test_a_canonical_recurrence_has_a_typed_view(self):
        r = ScheduledTask.new("x", _WHEN, recurrence="every 2 hours").recurrence_obj
        assert r is not None
        assert (r.amount, r.unit) == (2, RecurrenceUnit.HOURS)

    def test_a_singular_unit_is_accepted(self):
        assert ScheduledTask.new("x", _WHEN, recurrence="every 1 day").recurrence_obj is not None

    @pytest.mark.parametrize(
        "recurrence",
        [
            "daily",
            "every minute",
            "every 0 days",
            "EVERY 2 HOURS",
            "every 2 fortnights",
            f"every {'9' * 5000} days",
            "every 99999999999 weeks",
        ],
    )
    def test_a_recurrence_that_is_not_canonical_is_refused(self, recurrence):
        with pytest.raises(ValidationError):
            ScheduledTask.new("x", _WHEN, recurrence=recurrence)

    def test_every_field_describes_itself_for_the_sdks(self):
        """The SDKs are generated from this schema: a field without a
        description is a field an outside developer has to guess at."""
        schema = ScheduledTask.model_json_schema()

        assert schema["description"]
        assert set(schema["properties"]) == {
            "id",
            "description",
            "scheduled_at",
            "created_at",
            "status",
            "result",
            "error",
            "recurrence",
        }
        for name, prop in schema["properties"].items():
            assert prop.get("description"), f"{name} has no description"

    def test_the_status_enum_describes_every_state(self):
        assert TaskStatus.__doc__
        for state in TaskStatus:
            assert state.value in TaskStatus.__doc__


class TestTheRow:
    def test_a_task_goes_into_a_row_field_for_field(self):
        task = ScheduledTask(
            id="abc12345",
            description="d",
            scheduled_at=_WHEN,
            created_at=_WHEN - timedelta(days=1),
            status=TaskStatus.failed,
            result="r",
            error="e",
            recurrence="every 3 days",
        )

        row = ScheduledTaskModel.from_task(task)

        assert (row.id, row.description, row.scheduled_at, row.created_at) == (
            "abc12345",
            "d",
            _WHEN,
            _WHEN - timedelta(days=1),
        )
        assert (row.status, row.result, row.error, row.recurrence) == (
            "failed",
            "r",
            "e",
            "every 3 days",
        )
        # The scope is the writer's to stamp.
        assert (row.org, row.producer, row.project) == (None, None, None)

    def test_a_row_comes_back_as_the_same_task(self):
        task = ScheduledTask.new("d", _WHEN, recurrence="every 1 weeks")
        assert _round_trip(task) == task

    def test_empty_result_and_error_columns_read_as_empty_strings(self):
        row = ScheduledTaskModel.from_task(ScheduledTask.new("d", _WHEN))
        row.result = None  # type: ignore[assignment]
        row.error = None  # type: ignore[assignment]

        task = row.to_task()

        assert task is not None
        assert (task.result, task.error) == ("", "")

    def test_an_unknown_status_reads_as_no_task_and_says_so(self, caplog):
        row = ScheduledTaskModel.from_task(ScheduledTask.new("d", _WHEN))
        row.status = "paused"

        with caplog.at_level(logging.WARNING):
            assert row.to_task() is None

        assert row.id in caplog.text
        assert "unreadable" in caplog.text

    @pytest.mark.parametrize("stored", ["daily", "every fortnight", f"every {'9' * 5000} days"])
    def test_an_unrecognised_recurrence_reads_as_one_shot_with_a_warning(self, caplog, stored):
        row = ScheduledTaskModel.from_task(ScheduledTask.new("d", _WHEN))
        row.recurrence = stored

        with caplog.at_level(logging.WARNING):
            task = row.to_task()

        assert task is not None
        assert task.recurrence == ""
        assert row.id in caplog.text
        assert "one-shot" in caplog.text

    def test_an_empty_recurrence_needs_no_warning(self, caplog):
        row = ScheduledTaskModel.from_task(ScheduledTask.new("d", _WHEN))
        with caplog.at_level(logging.WARNING):
            task = row.to_task()
        assert task is not None
        assert caplog.text == ""

    def test_the_table_and_its_scope_index(self):
        assert ScheduledTaskModel.__tablename__ == "scheduler_tasks"
        indexes = {
            i.name: [c.name for c in i.columns] for i in ScheduledTaskModel.__table__.indexes
        }
        assert indexes["ix_scheduler_tasks_scope"] == ["org", "producer", "project"]


def _round_trip(task: ScheduledTask) -> ScheduledTask | None:
    return ScheduledTaskModel.from_task(task).to_task()
