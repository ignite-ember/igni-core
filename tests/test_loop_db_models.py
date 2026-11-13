"""What the loop tables themselves refuse and default, below the stores.

Raw inserts, so the CHECK, the key, the unique constraint and the
server defaults are the database's own answer. Both dialects.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from igni_core.core.db.database import Database
from igni_core.core.db.urls import DatabaseUrl
from igni_core.core.loop.db_models import LoopProgressModel, LoopStateModel
from tests.storage_backends import Scopes

NOW = datetime(2026, 1, 2, 3, 4, 5)


def _state_row(db: Database, **over) -> dict:
    row = {
        **db.scope.stamp(),
        "id": 1,
        "run_id": "r",
        "prompt": "p",
        "iteration_index": 1,
        "iterations_remaining": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return {**row, **over}


def _progress_row(db: Database, **over) -> dict:
    row = {
        **db.scope.stamp(),
        "run_id": "r",
        "key": "k",
        "value": "v",
        "created_at": NOW,
        "updated_at": NOW,
    }
    return {**row, **over}


@pytest.fixture
def db(database_url: DatabaseUrl) -> Database:
    return Database(database_url, scope=Scopes.fresh())


async def test_the_tables_are_named_loop_state_and_loop_progress():
    assert LoopStateModel.__tablename__ == "loop_state"
    assert LoopProgressModel.__tablename__ == "loop_progress"


async def test_a_loop_state_row_with_an_id_other_than_one_is_refused(db: Database):
    async with db.session() as session, session.begin():
        await session.execute(insert(LoopStateModel).values(_state_row(db)))
    with pytest.raises(IntegrityError):
        async with db.session() as session, session.begin():
            await session.execute(insert(LoopStateModel).values(_state_row(db, id=2)))


async def test_a_second_loop_state_row_in_one_scope_is_refused(db: Database):
    async with db.session() as session, session.begin():
        await session.execute(insert(LoopStateModel).values(_state_row(db)))
    with pytest.raises(IntegrityError):
        async with db.session() as session, session.begin():
            await session.execute(insert(LoopStateModel).values(_state_row(db)))


async def test_two_scopes_each_hold_their_own_loop_state_row(db: Database):
    other = db.in_scope(Scopes.fresh())
    async with db.session() as session, session.begin():
        await session.execute(insert(LoopStateModel).values(_state_row(db)))
        await session.execute(insert(LoopStateModel).values(_state_row(other)))


async def test_cap_explicit_defaults_to_false_in_the_database(db: Database):
    async with db.session() as session, session.begin():
        await session.execute(insert(LoopStateModel).values(_state_row(db)))
        row = (
            await session.execute(select(LoopStateModel).where(db.scope.where(LoopStateModel)))
        ).scalar_one()
        assert row.cap_explicit is False


async def test_a_duplicate_progress_key_in_one_run_and_scope_is_refused(db: Database):
    async with db.session() as session, session.begin():
        await session.execute(insert(LoopProgressModel).values(_progress_row(db)))
    with pytest.raises(IntegrityError):
        async with db.session() as session, session.begin():
            await session.execute(insert(LoopProgressModel).values(_progress_row(db)))


async def test_the_same_run_and_key_coexist_in_two_scopes_and_two_keys_in_one_run(db: Database):
    other = db.in_scope(Scopes.fresh())
    async with db.session() as session, session.begin():
        await session.execute(insert(LoopProgressModel).values(_progress_row(db)))
        await session.execute(insert(LoopProgressModel).values(_progress_row(other)))
        await session.execute(insert(LoopProgressModel).values(_progress_row(db, run_id="r2")))
        await session.execute(insert(LoopProgressModel).values(_progress_row(db, key="k2")))


async def test_a_progress_value_left_out_is_the_empty_string(db: Database):
    values = _progress_row(db)
    del values["value"]
    async with db.session() as session, session.begin():
        await session.execute(insert(LoopProgressModel).values(values))
        row = (
            await session.execute(
                select(LoopProgressModel).where(db.scope.where(LoopProgressModel))
            )
        ).scalar_one()
        assert row.value == ""


async def test_datetimes_come_back_naive_as_written(db: Database):
    async with db.session() as session, session.begin():
        await session.execute(insert(LoopStateModel).values(_state_row(db)))
        row = (
            await session.execute(select(LoopStateModel).where(db.scope.where(LoopStateModel)))
        ).scalar_one()
        assert row.created_at == NOW
        assert row.created_at.tzinfo is None
