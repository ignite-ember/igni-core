"""``/schedule`` through the slash router, against a real task store.

The session is only a project folder: the command builds its own store
there, so what a person sees after ``/schedule ...`` is checked against
what was stored.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.backend.command_handler import CommandHandler
from igni_core.core.scheduler.models import TaskStatus
from igni_core.core.scheduler.store import TaskStore
from igni_core.protocol.messages import CommandAction, CommandResultKind


def _session(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(project_dir=tmp_path)


async def _schedule(tmp_path: Path, args: str):
    return await CommandHandler(_session(tmp_path)).handle(f"/schedule {args}".rstrip())  # type: ignore[arg-type]


async def _stored(tmp_path: Path):
    return await TaskStore(project_dir=tmp_path).get_all(include_done=True)


def _task_id(result) -> str:
    return result.content.split("`")[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["", "   ", "list", "LIST"])
async def test_no_argument_or_list_opens_the_task_panel(tmp_path, args):
    result = await _schedule(tmp_path, args)
    assert result.action == CommandAction.SCHEDULE
    assert result.content == ""
    assert await _stored(tmp_path) == []


@pytest.mark.asyncio
async def test_a_one_shot_task_is_stored_and_reported_with_its_time(tmp_path):
    result = await _schedule(tmp_path, "add review the codebase at 5pm")
    (task,) = await _stored(tmp_path)
    assert task.description == "review the codebase"
    assert task.recurrence == ""
    assert task.status == TaskStatus.pending
    assert result.kind == CommandResultKind.INFO
    assert result.action == CommandAction.SCHEDULE
    assert result.content == (
        f'Scheduled `{task.id}`: "review the codebase" at '
        f"{task.scheduled_at.strftime('%Y-%m-%d %H:%M')}"
    )
    assert task.scheduled_at.hour == 17


@pytest.mark.asyncio
async def test_the_add_verb_is_optional(tmp_path):
    result = await _schedule(tmp_path, "run tests in 30 minutes")
    (task,) = await _stored(tmp_path)
    assert task.description == "run tests"
    assert _task_id(result) == task.id


@pytest.mark.asyncio
async def test_the_add_verb_is_case_insensitive(tmp_path):
    await _schedule(tmp_path, "ADD run tests in 30 minutes")
    assert len(await _stored(tmp_path)) == 1


@pytest.mark.asyncio
async def test_a_recurring_phrase_with_a_clock_time_keeps_its_recurrence(tmp_path):
    """``every`` and ``at`` both appear; the recurring reading must win."""
    result = await _schedule(tmp_path, "add run tests every 2 hours at 5pm")
    (task,) = await _stored(tmp_path)
    assert task.recurrence != ""
    assert task.description == "run tests"
    assert f"({task.recurrence}, first at " in result.content


@pytest.mark.asyncio
async def test_a_unicode_description_is_stored_whole(tmp_path):
    await _schedule(tmp_path, "prüfe die Abhängigkeiten 依存関係 daily")
    (task,) = await _stored(tmp_path)
    assert "Abhängigkeiten 依存関係" in task.description


@pytest.mark.asyncio
async def test_a_time_word_inside_another_word_does_not_schedule(tmp_path):
    for phrase in ("ping the host", "format the disk", "daily-ish".replace("daily", "dai")):
        result = await _schedule(tmp_path, phrase)
        assert result.action == CommandAction.SCHEDULE and result.content == ""
    assert await _stored(tmp_path) == []


@pytest.mark.asyncio
async def test_a_phrase_without_a_time_marker_opens_the_panel_and_stores_nothing(tmp_path):
    result = await _schedule(tmp_path, "something unrelated")
    assert result.action == CommandAction.SCHEDULE and result.content == ""
    assert await _stored(tmp_path) == []


@pytest.mark.asyncio
async def test_add_with_no_phrase_opens_the_panel(tmp_path):
    result = await _schedule(tmp_path, "add")
    assert result.action == CommandAction.SCHEDULE and result.content == ""
    assert await _stored(tmp_path) == []


@pytest.mark.asyncio
async def test_a_time_clause_that_does_not_parse_answers_with_usage_and_stores_nothing(tmp_path):
    result = await _schedule(tmp_path, "add task at xyzzy")
    assert result.is_error()
    assert result.content.startswith("Could not parse the time clause.")
    assert await _stored(tmp_path) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["rm", "remove", "cancel", "CANCEL"])
async def test_an_active_task_is_cancelled_by_every_cancel_verb(tmp_path, verb):
    task_id = _task_id(await _schedule(tmp_path, "add run tests in 30 minutes"))
    result = await _schedule(tmp_path, f"{verb}  {task_id} ")
    assert result.content == f"Cancelled task {task_id}"
    task = await TaskStore(project_dir=tmp_path).get(task_id)
    assert task.status == TaskStatus.cancelled


@pytest.mark.asyncio
async def test_cancelling_a_finished_task_says_what_state_it_is_in(tmp_path):
    task_id = _task_id(await _schedule(tmp_path, "add run tests in 30 minutes"))
    await _schedule(tmp_path, f"cancel {task_id}")
    again = await _schedule(tmp_path, f"cancel {task_id}")
    assert again.content == f"Task {task_id} is already cancelled"
    task = await TaskStore(project_dir=tmp_path).get(task_id)
    assert task.status == TaskStatus.cancelled


@pytest.mark.asyncio
async def test_cancelling_or_showing_an_unknown_id_is_not_found(tmp_path):
    for verb in ("cancel", "show"):
        result = await _schedule(tmp_path, f"{verb} nope0000")
        assert result.is_error()
        assert result.content == "Task not found: nope0000"


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["rm", "remove", "cancel", "show", "Show"])
async def test_a_verb_with_no_id_opens_the_panel(tmp_path, verb):
    result = await _schedule(tmp_path, verb)
    assert result.action == CommandAction.SCHEDULE and result.content == ""


@pytest.mark.asyncio
async def test_show_renders_the_stored_task(tmp_path):
    task_id = _task_id(await _schedule(tmp_path, "add review the codebase at 5pm"))
    result = await _schedule(tmp_path, f"show {task_id}")
    assert result.kind == CommandResultKind.MARKDOWN
    assert f"## Task {task_id}" in result.content
    assert "- **Description:** review the codebase" in result.content
    assert "- **Status:** pending" in result.content
