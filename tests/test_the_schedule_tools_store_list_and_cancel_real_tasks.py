"""``ScheduleTools`` against a real task store: what each tool answers and
what it leaves in the store."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from igni_core.core.scheduler.models import TaskStatus
from igni_core.core.scheduler.store import TaskStore
from igni_core.core.tools import schedule as schedule_module
from igni_core.core.tools.schedule import ScheduleTools


@pytest.fixture
def store(tmp_path):
    return TaskStore(db_path=tmp_path / "state.db")


@pytest.fixture
def tools(store):
    return ScheduleTools(store=store)


def _id_in(reply: str) -> str:
    match = re.search(r"`([0-9a-f]{8})`", reply)
    assert match, reply
    return match.group(1)


async def test_a_recurring_phrase_stores_a_recurring_task_and_says_when_it_first_runs(tools, store):
    reply = await tools.schedule_task("water the plants", "every 2 hours")

    task = await store.get(_id_in(reply))
    assert task is not None
    assert task.recurrence == "every 2 hours"
    assert task.description == "water the plants"
    assert task.status is TaskStatus.pending
    assert reply == (
        f'Scheduled recurring task `{task.id}`: "water the plants" '
        f"(every 2 hours, first run at {task.scheduled_at.strftime('%Y-%m-%d %H:%M')})."
    )


async def test_daily_at_a_time_is_recurring_and_pins_the_clock(tools, store):
    reply = await tools.schedule_task("standup notes", "daily at 9am")

    task = await store.get(_id_in(reply))
    assert task is not None
    assert task.recurrence == "every 1 days"
    assert (task.scheduled_at.hour, task.scheduled_at.minute) == (9, 0)
    assert "(every 1 days, first run at " in reply


async def test_a_one_shot_phrase_stores_a_one_shot_task(tools, store):
    before = datetime.now()

    reply = await tools.schedule_task("check the build", "in 30 minutes")

    task = await store.get(_id_in(reply))
    assert task is not None
    assert task.recurrence in (None, "")
    assert abs(task.scheduled_at - (before + timedelta(minutes=30))) < timedelta(minutes=2)
    assert reply == (
        f'Scheduled task `{task.id}`: "check the build" '
        f"at {task.scheduled_at.strftime('%Y-%m-%d %H:%M')}."
    )


async def test_an_iso_date_is_a_one_shot_at_that_time(tools, store):
    reply = await tools.schedule_task("ship it", "2031-03-20 14:00")

    task = await store.get(_id_in(reply))
    assert task is not None
    assert task.scheduled_at == datetime(2031, 3, 20, 14, 0)
    assert reply.endswith("at 2031-03-20 14:00.")


@pytest.mark.parametrize("when", ["", "   ", "whenever you feel like it", "@@@"])
async def test_a_time_that_cannot_be_read_stores_nothing_and_gives_examples(tools, store, when):
    reply = await tools.schedule_task("do a thing", when)

    assert reply.startswith(f"Could not parse time: '{when}'. Try:\n")
    assert "'in 30 minutes'" in reply
    assert "'every 2 hours'" in reply
    assert await store.get_all(include_done=True) == []


async def test_nothing_scheduled_lists_as_none(tools):
    assert await tools.list_scheduled_tasks() == "No scheduled tasks."
    assert await tools.list_scheduled_tasks(include_done=True) == "No scheduled tasks."


async def test_the_list_has_one_line_per_task_with_status_time_and_recurrence(tools, store):
    one = _id_in(await tools.schedule_task("one-off job", "2031-03-20 14:00"))
    rec = _id_in(await tools.schedule_task("regular job", "hourly"))

    lines = (await tools.list_scheduled_tasks()).splitlines()

    by_id = {re.match(r"- \[(\w+)\]", line).group(1): line for line in lines}
    assert by_id[one] == f"- [{one}] pending | 2031-03-20 14:00 | one-off job"
    rec_task = await store.get(rec)
    stamp = rec_task.scheduled_at.strftime("%Y-%m-%d %H:%M")
    assert by_id[rec] == f"- [{rec}] pending | {stamp} (every 1 hours) | regular job"
    assert len(lines) == 2


async def test_finished_tasks_are_listed_only_when_asked_for(tools):
    task_id = _id_in(await tools.schedule_task("old job", "tomorrow"))
    await tools.cancel_scheduled_task(task_id)

    assert await tools.list_scheduled_tasks() == "No scheduled tasks."
    listed = await tools.list_scheduled_tasks(include_done=True)
    assert f"- [{task_id}] cancelled | " in listed


async def test_cancelling_an_active_task_marks_it_cancelled(tools, store):
    task_id = _id_in(await tools.schedule_task("one-off job", "tomorrow"))

    reply = await tools.cancel_scheduled_task(task_id)

    assert reply == f'Cancelled task {task_id}: "one-off job".'
    task = await store.get(task_id)
    assert task is not None and task.status is TaskStatus.cancelled


async def test_cancelling_a_recurring_task_says_the_schedule_stopped(tools, store):
    task_id = _id_in(await tools.schedule_task("regular job", "daily"))

    reply = await tools.cancel_scheduled_task(task_id)

    assert reply == f'Cancelled task {task_id}: "regular job" (recurring schedule stopped).'
    task = await store.get(task_id)
    assert task is not None and task.status is TaskStatus.cancelled


async def test_cancelling_an_unknown_task_says_not_found(tools):
    assert await tools.cancel_scheduled_task("nope1234") == "Task not found: nope1234"


@pytest.mark.parametrize("status", [TaskStatus.cancelled, TaskStatus.completed, TaskStatus.failed])
async def test_cancelling_an_ended_task_says_so_and_changes_nothing(tools, store, status):
    task_id = _id_in(await tools.schedule_task("a job", "tomorrow"))
    await store.update_status(task_id, status)

    reply = await tools.cancel_scheduled_task(task_id)

    assert reply == f"Task {task_id} is already {status.value}."
    task = await store.get(task_id)
    assert task is not None and task.status is status


async def test_the_store_is_opened_on_the_first_call_for_the_given_project(tmp_path, monkeypatch):
    built: list[dict] = []
    real = TaskStore

    def _factory(**kwargs):
        built.append(kwargs)
        return real(db_path=tmp_path / "state.db")

    monkeypatch.setattr(schedule_module, "TaskStore", _factory)
    tools = ScheduleTools(project_dir="/work/project")
    assert built == [], "constructing the toolkit opened a database"

    await tools.list_scheduled_tasks()
    await tools.schedule_task("a job", "tomorrow")

    assert built == [{"project_dir": "/work/project"}]


async def test_an_injected_store_is_used_and_no_other_is_built(store, monkeypatch):
    def _never(**_kwargs):
        raise AssertionError("a store was built although one was injected")

    monkeypatch.setattr(schedule_module, "TaskStore", _never)
    tools = ScheduleTools(store=store, project_dir="/elsewhere")

    reply = await tools.schedule_task("a job", "tomorrow")

    assert await store.get(_id_in(reply)) is not None
