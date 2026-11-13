"""The startup pass over the persisted rows, against a real store.

A live pid is adopted as an orphan, a dead one becomes history (stamped
interrupted when it never recorded its end), a pid the registry already
tracks is left alone, a second pass changes nothing, and history is
pruned by the seven day window.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from igni_core.core.tools.finished_process import FinishedProcess
from igni_core.core.tools.orphan_process import OrphanProcess
from igni_core.core.tools.orphan_rehydrator import (
    HISTORY_RETENTION_SECONDS,
    OrphanRehydrator,
    RehydrateBuild,
    build_rehydrator,
)
from igni_core.core.tools.orphan_schemas import RehydrateResult
from igni_core.core.tools.process_store import BackgroundProcessStore
from igni_core.core.tools.process_store_schemas import BackgroundProcessRow
from igni_core.core.tools.process_supervisor import ProcessSupervisor

DEAD = 0x7FFFFFFE
DEAD_2 = 0x7FFFFFFD
WEEK = HISTORY_RETENTION_SECONDS


@pytest.fixture
def supervisor() -> ProcessSupervisor:
    return ProcessSupervisor()


@pytest.fixture
def store(tmp_path: Path) -> BackgroundProcessStore:
    return BackgroundProcessStore(db_path=tmp_path / "state.db")


def _row(pid: int, **kw) -> BackgroundProcessRow:
    kw.setdefault("started_at", 100)
    return BackgroundProcessRow(pid=pid, cmd=f"cmd-{pid}", pgid=pid, **kw)


def test_the_retention_window_is_seven_days():
    assert WEEK == 604800


async def test_a_live_pid_is_adopted_as_an_orphan_and_a_dead_one_as_history(
    supervisor, store
) -> None:
    await store.upsert(_row(os.getpid(), started_at=int(time.time())))
    await store.upsert(_row(DEAD))

    result = await OrphanRehydrator(supervisor, store).run()

    assert result == RehydrateResult(ok=True, surfaced=2, pruned=0, reason="")
    assert isinstance(supervisor.registry.get(os.getpid()), OrphanProcess)
    assert isinstance(supervisor.registry.get(DEAD), FinishedProcess)


async def test_the_store_is_attached_to_the_registry_by_the_pass(supervisor, store) -> None:
    assert supervisor.registry.persistence is None

    await OrphanRehydrator(supervisor, store).run()

    assert supervisor.registry.persistence is store


async def test_a_dead_row_that_never_recorded_its_end_is_stamped_interrupted(
    supervisor, store
) -> None:
    await store.upsert(_row(DEAD))
    before = int(time.time())

    await OrphanRehydrator(supervisor, store).run()

    [row] = await store.list_all()
    assert row.is_finished
    assert row.finished_at is not None and row.finished_at >= before
    assert row.exit_code is None
    assert supervisor.registry.get(DEAD).returncode() is None


async def test_a_dead_row_that_recorded_its_end_keeps_its_exit_code(supervisor, store) -> None:
    await store.upsert(_row(DEAD, exit_code=3, finished_at=int(time.time()) - 60))

    await OrphanRehydrator(supervisor, store).run()

    assert supervisor.registry.get(DEAD).returncode() == 3
    [row] = await store.list_all()
    assert row.exit_code == 3


async def test_a_second_pass_surfaces_nothing_twice(supervisor, store) -> None:
    await store.upsert(_row(os.getpid(), started_at=int(time.time())))
    await store.upsert(_row(DEAD))
    rehydrator = OrphanRehydrator(supervisor, store)

    first = await rehydrator.run()
    orphan = supervisor.registry.get(os.getpid())
    history = supervisor.registry.get(DEAD)
    second = await rehydrator.run()

    assert (first.surfaced, second.surfaced) == (2, 0)
    assert supervisor.registry.get(os.getpid()) is orphan
    assert supervisor.registry.get(DEAD) is history


async def test_a_pid_the_registry_already_tracks_is_not_replaced(supervisor, store) -> None:
    mine = OrphanProcess(
        pid=os.getpid(), cmd="mine", started_epoch=0, pgid=None, log_store=supervisor.log_store
    )
    supervisor.registry.add(mine)
    await store.upsert(_row(os.getpid(), started_at=int(time.time())))

    result = await OrphanRehydrator(supervisor, store).run()

    assert result.surfaced == 0
    assert supervisor.registry.get(os.getpid()) is mine


async def test_history_is_pruned_past_the_week_and_kept_inside_it(supervisor, store) -> None:
    now = int(time.time())
    await store.upsert(_row(DEAD, exit_code=0, finished_at=now - WEEK - 120))
    await store.upsert(_row(DEAD_2, exit_code=0, finished_at=now - WEEK + 120))

    result = await OrphanRehydrator(supervisor, store).run()

    assert result.pruned == 1
    assert [r.pid for r in await store.list_all()] == [DEAD_2]


async def test_an_empty_store_surfaces_nothing(supervisor, store) -> None:
    assert await OrphanRehydrator(supervisor, store).run() == RehydrateResult()


def test_a_build_with_no_arguments_has_no_rehydrator_and_a_clean_result():
    build = RehydrateBuild()

    assert build.rehydrator is None
    assert build.result == RehydrateResult()


def test_a_built_rehydrator_comes_with_a_successful_result(supervisor, tmp_path) -> None:
    build = build_rehydrator(supervisor, tmp_path)

    assert isinstance(build.rehydrator, OrphanRehydrator)
    assert build.result.ok is True
    assert build.result.reason == ""
