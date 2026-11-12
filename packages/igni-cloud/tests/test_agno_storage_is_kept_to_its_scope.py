"""Everything Agno's storage does for one scope, and nothing for another's.

``igni_cloud.storage.scoped_agno`` — the calls ``test_postgresql_storage``
leaves open: renaming and deleting sessions, runs read, cancelled and
deleted by id, memories (by id, topics, stats, clear), learnings, a
foreign ``user_id`` passed by the caller, and the table lookups.
Each test needs ``IGNI_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from agno.db.base import SessionType
from agno.db.postgres import AsyncPostgresDb
from agno.db.schemas.memory import UserMemory
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session import AgentSession
from igni_cloud.storage.agno_table_cache import AgnoTableCache
from igni_cloud.storage.postgres import PostgresStorage
from igni_cloud.storage.scoped_agno import ScopedAsyncPostgresDb
from sqlalchemy import select

from igni_core.core.db.database import Database
from igni_core.core.db.engine import engines
from igni_core.core.db.urls import DatabaseUrl
from igni_core.core.memory.manager import StorageManager
from tests.storage_backends import Scopes, ThrowawayPostgres

SESSIONS = StorageManager.SESSION_TABLE
MEMORIES = StorageManager.MEMORY_TABLE


def _db(url: DatabaseUrl, tables: AgnoTableCache | None = None) -> ScopedAsyncPostgresDb:
    return ScopedAsyncPostgresDb(
        scope=Scopes.fresh(),
        tables=tables or AgnoTableCache(),
        db_engine=engines.get_async_engine(url),
        session_table=SESSIONS,
        memory_table=MEMORIES,
        create_schema=False,
    )


@pytest.fixture(scope="module")
def prepared(postgres_url: str) -> DatabaseUrl:
    url = DatabaseUrl.parse(postgres_url)
    Database(url)
    PostgresStorage().prepare(url)
    return url


@pytest_asyncio.fixture
async def pair(
    prepared: DatabaseUrl,
) -> AsyncIterator[tuple[ScopedAsyncPostgresDb, ScopedAsyncPostgresDb]]:
    yield _db(prepared), _db(prepared)


def _session(session_id: str, **fields: Any) -> AgentSession:
    now = int(time.time())
    fields.setdefault("session_data", {})
    return AgentSession(session_id=session_id, agent_id="igni", created_at=now, **fields)


def _id() -> str:
    return uuid.uuid4().hex[:8]


async def test_a_session_is_renamed_listed_and_deleted_within_its_scope(pair):
    mine, theirs = pair
    session_id = _id()
    for db in pair:
        await db.upsert_session(_session(session_id))

    renamed = await mine.rename_session(session_id, SessionType.AGENT, "Mine")
    assert renamed is not None and renamed.session_id == session_id  # type: ignore[union-attr]
    assert renamed.session_data["session_name"] == "Mine"  # type: ignore[union-attr]
    other = await theirs.get_session(session_id, SessionType.AGENT)
    assert other is not None and not (other.session_data or {}).get("session_name")  # type: ignore[union-attr]

    other_id = _id()
    await theirs.upsert_session(_session(other_id))
    assert await mine.rename_session(other_id, SessionType.AGENT, "Stolen") is None
    kept = await theirs.get_session(other_id, SessionType.AGENT)
    assert not (kept.session_data or {}).get("session_name")  # type: ignore[union-attr]

    await mine.delete_sessions([session_id, other_id])
    assert await mine.get_session(session_id, SessionType.AGENT) is None
    assert await theirs.get_session(session_id, SessionType.AGENT) is not None
    assert await theirs.get_session(other_id, SessionType.AGENT) is not None


async def test_the_callers_session_keeps_its_plain_id_and_the_scope_wins_on_reads(pair):
    mine, theirs = pair
    session_id = _id()
    session = _session(session_id, user_id="somebody")
    returned = await mine.upsert_session(session)
    assert session.session_id == session_id and session.user_id == "somebody"
    assert returned is not None and returned.session_id == session_id  # type: ignore[union-attr]
    assert returned.user_id == mine.scoped_user_id  # type: ignore[union-attr]
    await theirs.upsert_session(_session(_id()))

    # A caller naming the other scope's user changes nothing.
    got = await mine.get_session(session_id, SessionType.AGENT, user_id=theirs.scoped_user_id)
    assert got is not None
    listed = await mine.get_sessions(session_type=SessionType.AGENT, user_id=theirs.scoped_user_id)
    assert [s.session_id for s in listed] == [session_id]  # type: ignore[union-attr]
    assert (
        await theirs.get_session(session_id, SessionType.AGENT, user_id=mine.scoped_user_id) is None
    )


async def test_unserialised_sessions_come_back_with_plain_ids_and_a_total(pair):
    mine, theirs = pair
    ids = sorted([_id(), _id()])
    for session_id in ids:
        await mine.upsert_session(_session(session_id))
    await theirs.upsert_session(_session(_id()))
    rows, total = await mine.get_sessions(session_type=SessionType.AGENT, deserialize=False)  # type: ignore[misc]
    assert total == 2
    assert sorted(r["session_id"] for r in rows) == ids
    row = await mine.get_session(ids[0], SessionType.AGENT, deserialize=False)
    assert isinstance(row, dict) and row["session_id"] == ids[0]
    page, total = await mine.get_sessions(  # type: ignore[misc]
        session_type=SessionType.AGENT, deserialize=False, limit=1, page=1
    )
    assert len(page) == 1 and total == 2


async def _run(db: ScopedAsyncPostgresDb, session_id: str, **fields: Any) -> str:
    run_id = uuid.uuid4().hex
    await db.upsert_run(
        RunOutput(run_id=run_id, session_id=session_id, agent_id="igni", **fields),
        session_id=session_id,
        run_index=0,
    )
    return run_id


async def test_a_scopes_own_runs_are_read_listed_and_deleted(pair):
    mine, theirs = pair
    session_id = _id()
    for db in pair:
        await db.upsert_session(_session(session_id))
    first, second = await _run(mine, session_id, content="a"), await _run(mine, session_id)
    foreign = await _run(theirs, session_id, content="theirs")

    got = await mine.get_run(first)
    assert got is not None and got.content == "a" and got.session_id == session_id  # type: ignore[union-attr]
    row = await mine.get_run(first, deserialize=False)
    assert isinstance(row, dict) and row["session_id"] == session_id
    assert await mine.get_run(foreign) is None
    assert await mine.delete_run(foreign) is False
    assert await theirs.get_run(foreign) is not None
    runs = await mine.get_runs(user_id=theirs.scoped_user_id)
    assert {r.run_id for r in runs} == {first, second}  # type: ignore[union-attr]
    assert all(r.session_id == session_id for r in runs)  # type: ignore[union-attr]
    rows, total = await mine.get_runs(session_id=session_id, deserialize=False)  # type: ignore[misc]
    assert total == 2 and {r["session_id"] for r in rows} == {session_id}

    # Delete: a mixed list removes only what is this scope's.
    await mine.delete_runs([first, foreign])
    assert await mine.get_run(first) is None
    assert await theirs.get_run(foreign) is not None
    await mine.delete_runs([])
    assert await mine.get_run(second) is not None
    assert await mine.delete_run(second) is True
    assert await mine.delete_run(second) is False


async def test_only_the_owner_cancels_a_pending_run(pair):
    mine, theirs = pair
    session_id = _id()
    for db in pair:
        await db.upsert_session(_session(session_id))
    run_id = await _run(mine, session_id, status=RunStatus.pending)

    assert await theirs.cancel_pending_run(run_id, session_id=session_id, reason="no") is False
    still = await mine.get_run(run_id)
    assert still is not None and still.status == RunStatus.pending  # type: ignore[union-attr]
    assert await mine.cancel_pending_run(run_id, session_id=session_id, reason="stop") is True
    done = await mine.get_run(run_id)
    assert done is not None and done.status == RunStatus.cancelled  # type: ignore[union-attr]


async def test_memories_are_read_counted_and_deleted_within_their_scope(pair):
    mine, theirs = pair
    a = await mine.upsert_user_memory(UserMemory(memory="tea", topics=["drink"], user_id="x"))
    b = await mine.upsert_user_memory(UserMemory(memory="cats", topics=["pets"]))
    c = await theirs.upsert_user_memory(UserMemory(memory="coffee", topics=["secret"]))
    assert a is not None and b is not None and c is not None
    assert a.user_id == mine.scoped_user_id  # type: ignore[union-attr]

    assert (await mine.get_user_memory(a.memory_id)).memory == "tea"  # type: ignore[union-attr]
    assert await mine.get_user_memory(c.memory_id) is None
    assert await mine.get_user_memory(c.memory_id, user_id=theirs.scoped_user_id) is None
    assert sorted(await mine.get_all_memory_topics()) == ["drink", "pets"]
    assert sorted(await mine.get_all_memory_topics(user_id=theirs.scoped_user_id)) == [
        "drink",
        "pets",
    ]
    stats, total = await mine.get_user_memory_stats(user_id=theirs.scoped_user_id)
    assert total == 1 and stats[0]["user_id"] == mine.scoped_user_id
    assert stats[0]["total_memories"] == 2

    await mine.delete_user_memory(c.memory_id)
    await mine.delete_user_memories([c.memory_id])
    assert (await theirs.get_user_memory(c.memory_id)).memory == "coffee"  # type: ignore[union-attr]
    await mine.delete_user_memory(a.memory_id)
    assert [m.memory for m in await mine.get_user_memories()] == ["cats"]  # type: ignore[union-attr]
    await mine.delete_user_memories([b.memory_id, c.memory_id])
    assert await mine.get_user_memories() == []
    assert (await theirs.get_user_memory(c.memory_id)).memory == "coffee"  # type: ignore[union-attr]


async def test_clearing_memories_clears_only_this_scopes(pair):
    mine, theirs = pair
    await mine.clear_memories()  # nothing to clear is no error
    await mine.upsert_user_memory(UserMemory(memory="one"))
    await mine.upsert_user_memory(UserMemory(memory="two"))
    await theirs.upsert_user_memory(UserMemory(memory="theirs"))
    await mine.clear_memories()
    assert await mine.get_user_memories() == []
    assert [m.memory for m in await theirs.get_user_memories()] == ["theirs"]  # type: ignore[union-attr]


async def test_learnings_are_stamped_and_filtered_by_scope_and_no_global_row_leaks(
    pair, prepared: DatabaseUrl
):
    mine, theirs = pair
    await mine.upsert_learning(
        "l-mine", "user_profile", {"likes": "tea"}, user_id="x", namespace="n"
    )
    await theirs.upsert_learning("l-theirs", "user_profile", {"likes": "coffee"}, namespace="n")
    # A row that belongs to nobody: what ``include_global`` would reach.
    plain = AsyncPostgresDb(
        db_engine=engines.get_async_engine(prepared), session_table=SESSIONS, memory_table=MEMORIES
    )
    global_id = f"global-{_id()}"
    await plain.upsert_learning(global_id, "user_profile", {"likes": "everyone"}, namespace="n")

    found = await mine.get_learning("user_profile", namespace="n", user_id=theirs.scoped_user_id)
    assert found is not None and found["content"] == {"likes": "tea"}
    table = await mine._get_table("learnings")
    async with mine.async_session_factory() as sess:
        stamped = (
            await sess.execute(select(table.c.user_id).where(table.c.learning_id == "l-mine"))
        ).scalar_one()
    assert stamped == mine.scoped_user_id
    other = await theirs.get_learning("user_profile", namespace="n", user_id=mine.scoped_user_id)
    assert other is not None and other["content"] == {"likes": "coffee"}
    assert [r["content"] for r in await mine.get_learnings(user_id=theirs.scoped_user_id)] == [
        {"likes": "tea"}
    ]
    rows, total = await mine.list_learnings(include_global=True, user_id=theirs.scoped_user_id)
    assert total == 1 and [r["content"] for r in rows] == [{"likes": "tea"}]
    assert global_id not in {r["learning_id"] for r in rows}
    rows, total = await mine.list_learnings(limit=1, page=1)
    assert len(rows) == 1 and total == 1


async def test_a_table_not_yet_made_is_not_remembered_and_other_kinds_fall_to_agno():
    server = os.environ.get("IGNI_TEST_DATABASE_URL", "").strip()
    if not server:
        pytest.skip("IGNI_TEST_DATABASE_URL is unset")
    database = ThrowawayPostgres(server)
    url = DatabaseUrl.parse(database.create())
    try:
        Database(url)
        tables = AgnoTableCache()
        db = _db(url, tables)
        assert await db._get_table("sessions") is None
        assert await db.get_run("nope") is None, "no runs table: nothing is owned"
        await db.delete_runs(["nope"])
        assert await db.delete_run("nope") is False
        await asyncio.to_thread(PostgresStorage().prepare, url)
        assert await db._get_table("sessions") is not None, "asked again, not cached as missing"
        reference = AsyncPostgresDb(
            db_engine=engines.get_async_engine(url), session_table=SESSIONS, memory_table=MEMORIES
        )
        ours, theirs = await db._get_table("metrics"), await reference._get_table("metrics")
        assert (ours is None) == (theirs is None)
        if ours is not None:
            assert ours.name == theirs.name
    finally:
        await engines.reset_pools()
        database.drop()


async def test_a_shared_cache_keeps_tables_of_other_names_apart(prepared: DatabaseUrl):
    tables = AgnoTableCache()
    known = _db(prepared, tables)
    assert await known._get_table("sessions") is not None
    elsewhere = ScopedAsyncPostgresDb(
        scope=Scopes.fresh(),
        tables=tables,
        db_engine=engines.get_async_engine(prepared),
        session_table="never_made_sessions",
        memory_table=MEMORIES,
        create_schema=False,
    )
    assert await elsewhere._get_table("sessions") is None
