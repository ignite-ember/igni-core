"""The background-process row and result models.

The store hands rows out as immutable snapshots and refuses to write
what is not a row: a pid that is not a number never reaches the table.
``from_model`` maps a duck-typed ORM object, including one that predates
the history columns.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from igni_core.core.tools.process_store_schemas import (
    BackgroundProcessRow,
    RemoveResult,
    UpsertResult,
)


def test_a_pid_that_is_not_a_number_is_refused():
    with pytest.raises(ValidationError):
        BackgroundProcessRow(pid="abc", cmd="x", started_at=1)  # type: ignore[arg-type]


def test_a_fractional_start_time_is_refused():
    with pytest.raises(ValidationError):
        BackgroundProcessRow(pid=1, cmd="x", started_at=1.5)  # type: ignore[arg-type]


def test_a_row_cannot_be_changed_after_it_is_made():
    row = BackgroundProcessRow(pid=1, cmd="x", started_at=1)

    with pytest.raises(ValidationError):
        row.exit_code = 0  # type: ignore[misc]


def test_a_new_row_is_running_and_stamped_with_the_clock_it_is_given():
    row = BackgroundProcessRow.new(5, "sleep 9", None, now=0)

    assert row.started_at == 0, "a clock of zero is a clock, not an absent one"
    assert row.pgid is None
    assert (row.exit_code, row.finished_at) == (None, None)
    assert not row.is_finished


def test_a_new_row_without_a_clock_is_stamped_now():
    import time

    before = int(time.time())
    row = BackgroundProcessRow.new(5, "x", 5)

    assert before <= row.started_at <= int(time.time())


def test_a_row_is_finished_by_its_finish_time_not_its_exit_code():
    unobserved = BackgroundProcessRow(pid=1, cmd="x", started_at=1, finished_at=9)
    coded_but_open = BackgroundProcessRow(pid=1, cmd="x", started_at=1, exit_code=0)
    ended_at_zero = BackgroundProcessRow(pid=1, cmd="x", started_at=1, finished_at=0)

    assert unobserved.is_finished
    assert not coded_but_open.is_finished
    assert ended_at_zero.is_finished


def test_a_double_that_predates_the_history_columns_still_maps():
    legacy = SimpleNamespace(pid=3, cmd="npm start", pgid=None, started_at=40)

    row = BackgroundProcessRow.from_model(legacy)

    assert row == BackgroundProcessRow(pid=3, cmd="npm start", started_at=40)
    assert (row.exit_code, row.finished_at) == (None, None)


def test_the_upsert_values_are_the_six_columns():
    row = BackgroundProcessRow(pid=3, cmd="é", pgid=3, started_at=40, exit_code=2, finished_at=50)

    assert row.to_upsert_values() == {
        "pid": 3,
        "cmd": "é",
        "pgid": 3,
        "started_at": 40,
        "exit_code": 2,
        "finished_at": 50,
    }


def test_the_results_default_to_success_with_nothing_removed():
    assert (UpsertResult().ok, UpsertResult().reason) == (True, "")
    default = RemoveResult()
    assert (default.ok, default.removed, default.reason) == (True, False, "")
