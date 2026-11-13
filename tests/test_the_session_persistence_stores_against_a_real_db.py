"""The session persistence stores, one by one, against Agno's real SQLite.

``test_session_data_real_db`` drives the stores through the facade for
the happy paths; this file pins what each store does on its own at its
edges: the failure envelopes, the shapes it refuses, the rows it hides,
and how a name, a fork and a history are really written.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from agno.db.base import SessionType
from agno.db.sqlite import AsyncSqliteDb
from agno.models.message import Message
from agno.run.agent import RunInput, RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from igni_core.core.session import persistence as persistence_pkg
from igni_core.core.session.event_log_schema import (
    VisualizationDeltaEntry,
    VisualizationDeltaPayload,
)
from igni_core.core.session.persistence import (
    AgnoSessionDb,
    EventLogStore,
    PlanDecisionsStore,
    ScopedAgnoDb,
    SessionDataService,
    SessionForker,
    SessionListing,
    SessionNamer,
    SessionPersistence,
)
from igni_core.core.session.persistence.run_history import RunHistory
from igni_core.core.tools.plan import PlanDecisionsBlob

SESSION = "sess-stores"


# ── Fixtures and doubles ─────────────────────────────────────────────


@pytest.fixture
def db(tmp_path: Path) -> AsyncSqliteDb:
    return AsyncSqliteDb(
        db_file=str(tmp_path / "state.db"),
        session_table="igni_sessions",
        memory_table="igni_memories",
    )


class _Flaky:
    """A real database whose named methods raise — a storage that fails."""

    def __init__(self, real: AsyncSqliteDb, *failing: str) -> None:
        self._real = real
        self._failing = set(failing)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._real, name)
        if name not in self._failing:
            return attr

        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(f"{name} is down")

        return boom


def _run(index: int, *, session_id: str = SESSION, parent: str | None = None) -> RunOutput:
    return RunOutput(
        run_id=f"run-{index}",
        agent_id="igni",
        session_id=session_id,
        parent_run_id=parent,
        input=RunInput(input_content=f"question {index}"),
        messages=[Message(role="user", content=f"question {index}")],
        content=f"answer {index}",
    )


def _session(
    session_id: str = SESSION,
    *,
    runs: list[RunOutput] | None = None,
    agent_id: str = "igni",
    session_data: dict[str, Any] | None = None,
    updated_at: int | None = None,
) -> AgentSession:
    now = int(time.time())
    return AgentSession(
        session_id=session_id,
        agent_id=agent_id,
        runs=runs or [],
        session_data=session_data if session_data is not None else {},
        created_at=now,
        updated_at=updated_at if updated_at is not None else now,
    )


async def _stored(db: Any, session_id: str = SESSION) -> AgentSession | None:
    return await db.get_session(session_id=session_id, session_type=SessionType.AGENT)


class _Model:
    """A model that records whether it was asked, and answers a title."""

    def __init__(self, answer: str = "Port lookup") -> None:
        self.answer = answer
        self.asked: list[list[Message]] = []
        self.max_tokens: int | None = None

    async def aresponse(self, *, messages: list[Message]) -> Any:
        self.asked.append(messages)

        class _Answer:
            content = self.answer

        return _Answer()


# ── The package and the protocols ────────────────────────────────────


def test_every_name_the_package_lists_resolves() -> None:
    assert sorted(persistence_pkg.__all__) == sorted(
        [
            "AgnoSessionDb",
            "EventLogStore",
            "PlanDecisionsStore",
            "ScopedAgnoDb",
            "SessionDataService",
            "SessionForker",
            "SessionListing",
            "SessionNamer",
            "SessionPersistence",
            "TodoSnapshotStore",
        ]
    )
    for name in persistence_pkg.__all__:
        assert getattr(persistence_pkg, name) is not None


def test_agno_s_real_database_satisfies_the_session_db_protocol(db: AsyncSqliteDb) -> None:
    # Drift detection: the hand-written protocol names what Agno really has.
    assert isinstance(db, AgnoSessionDb)


def test_a_database_without_a_scope_is_not_scoped(db: AsyncSqliteDb) -> None:
    class _Scoped:
        scoped_user_id = "producer-1"

    assert isinstance(_Scoped(), ScopedAgnoDb)
    assert not isinstance(db, ScopedAgnoDb)
    assert not isinstance(object(), ScopedAgnoDb)


# ── The data service ─────────────────────────────────────────────────


class TestDataServiceReads:
    async def test_without_a_db_the_read_is_an_empty_success(self) -> None:
        result = await SessionDataService(None, SESSION).read_key("k", lambda raw: raw)
        assert (result.ok, result.value, result.error) == (True, None, None)

    async def test_a_missing_row_an_empty_blob_and_a_missing_key_are_misses(
        self, db: AsyncSqliteDb
    ) -> None:
        service = SessionDataService(db, SESSION)
        assert (await service.read_key("k", lambda raw: raw)).ok
        assert (await service.read_key("k", lambda raw: raw)).value is None

        await db.upsert_session(_session(session_data={}))
        assert (await service.read_key("k", lambda raw: raw)).value is None

        await db.upsert_session(_session(session_data={"other": 1}))
        assert (await service.read_key("k", lambda raw: raw)).value is None

    async def test_a_key_holding_none_is_a_miss_the_parser_never_sees(
        self, db: AsyncSqliteDb
    ) -> None:
        await db.upsert_session(_session(session_data={"k": None}))
        seen: list[object] = []
        result = await SessionDataService(db, SESSION).read_key(
            "k", lambda raw: seen.append(raw) or raw
        )
        assert result.ok
        assert result.value is None
        assert seen == []

    async def test_a_hit_is_handed_to_the_parser(self, db: AsyncSqliteDb) -> None:
        await db.upsert_session(_session(session_data={"k": [1, 2]}))
        result = await SessionDataService(db, SESSION).read_key("k", lambda raw: sum(raw))
        assert (result.ok, result.value) == (True, 3)

    async def test_a_failing_read_is_reported_with_its_text(self, db: AsyncSqliteDb) -> None:
        service = SessionDataService(_Flaky(db, "get_session"), SESSION)
        result = await service.read_key("k", lambda raw: raw)
        assert result.ok is False
        assert result.value is None
        assert result.error == "get_session is down"

    async def test_a_parser_that_raises_is_reported_not_raised(self, db: AsyncSqliteDb) -> None:
        await db.upsert_session(_session(session_data={"k": "blob"}))

        def parser(_raw: object) -> object:
            raise ValueError("unreadable blob")

        result = await SessionDataService(db, SESSION).read_key("k", parser)
        assert result.ok is False
        assert result.value is None
        assert result.error == "unreadable blob"


class TestDataServiceWrites:
    async def test_without_a_db_the_write_is_an_ok_no_op(self) -> None:
        result = await SessionDataService(None, SESSION).write_key("k", 1)
        assert (result.ok, result.error) == (True, None)

    async def test_a_first_write_creates_the_row_and_a_later_one_keeps_its_siblings(
        self, db: AsyncSqliteDb
    ) -> None:
        service = SessionDataService(db, SESSION)
        assert (await service.write_key("a", 1)).ok
        assert (await _stored(db)).session_data == {"a": 1}
        assert (await service.write_key("b", 2)).ok
        assert (await _stored(db)).session_data == {"a": 1, "b": 2}
        assert (await service.write_key("a", 3)).ok
        assert (await _stored(db)).session_data == {"a": 3, "b": 2}

    async def test_a_failing_write_is_reported_and_leaves_the_lock_free(
        self, db: AsyncSqliteDb
    ) -> None:
        down = SessionDataService(_Flaky(db, "upsert_session"), SESSION)
        result = await down.write_key("a", 1)
        assert result.ok is False
        assert result.error == "upsert_session is down"
        assert await _stored(db) is None

        # The lock was released: a second write is not stranded behind it.
        again = await asyncio.wait_for(down.write_key("a", 1), timeout=5)
        assert again.ok is False

    async def test_a_failing_read_inside_the_write_is_reported(self, db: AsyncSqliteDb) -> None:
        result = await SessionDataService(_Flaky(db, "get_session"), SESSION).write_key("a", 1)
        assert result.ok is False
        assert result.error == "get_session is down"

    async def test_concurrent_writes_to_different_keys_all_land(self, db: AsyncSqliteDb) -> None:
        service = SessionDataService(db, SESSION)
        results = await asyncio.gather(*(service.write_key(f"k{i}", i) for i in range(6)))
        assert all(r.ok for r in results)
        assert (await _stored(db)).session_data == {f"k{i}": i for i in range(6)}

    async def test_retargeting_the_session_id_moves_reads_and_writes(
        self, db: AsyncSqliteDb
    ) -> None:
        service = SessionDataService(db, "first")
        await service.write_key("k", "one")
        service.session_id = "second"
        assert service.session_id == "second"
        assert (await service.read_key("k", lambda raw: raw)).value is None
        await service.write_key("k", "two")
        assert (await _stored(db, "first")).session_data == {"k": "one"}
        assert (await _stored(db, "second")).session_data == {"k": "two"}
        assert service.db is db


# ── The facade ───────────────────────────────────────────────────────


class TestFacade:
    async def test_the_properties_expose_what_was_built_with(self, db: AsyncSqliteDb) -> None:
        facade = SessionPersistence(db=db, session_id=SESSION)
        assert facade.db is db
        assert facade.session_id == SESSION

    async def test_a_rotated_id_retargets_saves_names_and_forks(self, db: AsyncSqliteDb) -> None:
        facade = SessionPersistence(db=db, session_id="old")
        await facade.save_todos([])
        facade.session_id = "new"
        assert facade.session_id == "new"

        await facade.save_event_log([])
        assert "event_log" not in ((await _stored(db, "old")).session_data or {})
        assert "event_log" in (await _stored(db, "new")).session_data

        assert (await facade.title("Rotated")).ok
        assert await facade.get_name() == "Rotated"
        assert await SessionPersistence(db=db, session_id="old").get_name() == ""

        forked = await facade.fork()
        assert forked.ok
        copy = await _stored(db, forked.new_session_id)
        assert copy.session_data["session_name"] == "Rotated"

    async def test_a_session_without_a_row_is_named_by_title(self, db: AsyncSqliteDb) -> None:
        facade = SessionPersistence(db=db, session_id=SESSION)
        assert await _stored(db) is None
        assert (await facade.title("Thread from a service")).ok
        assert (await _stored(db)).session_data["session_name"] == "Thread from a service"
        assert await facade.get_name() == "Thread from a service"
        assert await facade._get_name_raw() == "Thread from a service"

    async def test_failed_saves_are_reported_for_every_store(self, db: AsyncSqliteDb) -> None:
        facade = SessionPersistence(db=_Flaky(db, "upsert_session"), session_id=SESSION)
        plan = await facade.save_plan_decisions(PlanDecisionsBlob(decisions={"r": "approved"}))
        todos = await facade.save_todos([])
        events = await facade.save_event_log([])
        for result in (plan, todos, events):
            assert result.ok is False
            assert result.error == "upsert_session is down"

    async def test_failed_loads_come_back_empty_never_raising(self, db: AsyncSqliteDb) -> None:
        facade = SessionPersistence(
            db=_Flaky(db, "get_session", "get_sessions"), session_id=SESSION
        )
        assert await facade.list_sessions() == []
        assert (await facade.load_plan_decisions()).decisions == {}
        assert await facade.load_todos() == []
        assert await facade.load_event_log() == []
        assert await facade.get_name() == ""
        assert (await facade.fork()).ok is False

    async def test_with_no_db_every_method_is_a_quiet_no_op(self) -> None:
        facade = SessionPersistence(db=None, session_id=SESSION)
        assert await facade.list_sessions() == []
        assert await facade.get_name() == ""
        assert (await facade.rename("x")).ok
        assert (await facade.title("x")).ok is True
        assert (await facade.save_todos([])).ok
        assert (await facade.save_event_log([])).ok
        assert (await facade.save_plan_decisions(PlanDecisionsBlob())).ok
        assert await facade.load_event_log() == []
        fork = await facade.fork()
        assert (fork.ok, fork.error) == (False, "session store unavailable")


# ── The event log and plan decision stores ───────────────────────────


def _viz(seq: int) -> VisualizationDeltaEntry:
    return VisualizationDeltaEntry(
        seq=seq,
        run_id="r",
        timestamp_ms=seq,
        payload=VisualizationDeltaPayload(spec_id=f"s{seq}", spec_json="{}"),
    )


class TestEventLogStore:
    async def test_rows_that_are_not_events_drop_and_their_neighbours_survive(
        self, db: AsyncSqliteDb, caplog: pytest.LogCaptureFixture
    ) -> None:
        service = SessionDataService(db, SESSION)
        store = EventLogStore(service)
        good = [_viz(1).model_dump(), _viz(2).model_dump()]
        await service.write_key(
            "event_log", [good[0], "not a dict", 7, {"kind": "from_the_future"}, good[1]]
        )
        with caplog.at_level("WARNING"):
            loaded = await store.load()
        assert loaded.ok
        assert [e.seq for e in loaded.value] == [1, 2]
        assert "dropping unreadable session event row" in caplog.text

    async def test_a_blob_that_is_not_a_list_loads_empty(self, db: AsyncSqliteDb) -> None:
        service = SessionDataService(db, SESSION)
        await service.write_key("event_log", _viz(1).model_dump())
        loaded = await EventLogStore(service).load()
        assert (loaded.ok, loaded.value) == (True, [])

    async def test_saving_nothing_clears_the_log_and_keeps_the_other_keys(
        self, db: AsyncSqliteDb
    ) -> None:
        service = SessionDataService(db, SESSION)
        store = EventLogStore(service)
        await service.write_key("todos", [])
        assert (await store.save([_viz(1), _viz(2)])).ok
        assert (await store.save([])).ok
        assert (await store.load()).value == []
        assert "todos" in (await _stored(db)).session_data

    async def test_a_failed_save_is_reported(self, db: AsyncSqliteDb) -> None:
        store = EventLogStore(SessionDataService(_Flaky(db, "upsert_session"), SESSION))
        result = await store.save([_viz(1)])
        assert result.ok is False
        assert result.error == "upsert_session is down"

    async def test_the_order_saved_is_the_order_loaded(self, db: AsyncSqliteDb) -> None:
        store = EventLogStore(SessionDataService(db, SESSION))
        await store.save([_viz(3), _viz(1), _viz(2)])
        assert [e.seq for e in (await store.load()).value] == [3, 1, 2]


class TestPlanDecisionsStore:
    async def test_a_failed_save_is_reported(self, db: AsyncSqliteDb) -> None:
        store = PlanDecisionsStore(SessionDataService(_Flaky(db, "upsert_session"), SESSION))
        result = await store.save(PlanDecisionsBlob(decisions={"r": "approved"}))
        assert result.ok is False
        assert result.error == "upsert_session is down"

    async def test_decisions_are_stored_as_plain_strings(self, db: AsyncSqliteDb) -> None:
        store = PlanDecisionsStore(SessionDataService(db, SESSION))
        await store.save(PlanDecisionsBlob(decisions={"r1": "approved", "r2": "dismissed"}))
        assert (await _stored(db)).session_data["plan_decisions"] == {
            "r1": "approved",
            "r2": "dismissed",
        }
        assert (await store.load()).value.decisions == {"r1": "approved", "r2": "dismissed"}

    async def test_a_blob_that_is_not_a_mapping_loads_as_no_decisions(
        self, db: AsyncSqliteDb
    ) -> None:
        service = SessionDataService(db, SESSION)
        await service.write_key("plan_decisions", ["approved"])
        loaded = await PlanDecisionsStore(service).load()
        assert loaded.ok
        assert loaded.value.decisions == {}


# ── Listing ──────────────────────────────────────────────────────────


class TestListing:
    async def test_newest_first_and_the_limit_caps(self, db: AsyncSqliteDb) -> None:
        await db.upsert_sessions(
            [
                _session(name, updated_at=at)
                for name, at in (("old", 100), ("new", 300), ("mid", 200))
            ],
            preserve_updated_at=True,
        )
        listing = SessionListing(db)
        rows = (await listing.list_sessions()).value
        assert [r.session_id for r in rows] == ["new", "mid", "old"]
        capped = (await listing.list_sessions(limit=2)).value
        assert [r.session_id for r in capped] == ["new", "mid"]

    async def test_sub_agents_are_hidden_and_legacy_and_unlabelled_rows_are_kept(
        self, db: AsyncSqliteDb
    ) -> None:
        await db.upsert_sessions(
            [
                _session("main", agent_id="igni", updated_at=500),
                _session("legacy", agent_id="ember", updated_at=400),
                _session("unlabelled", agent_id="", updated_at=300),
                _session("scratch", agent_id="visualizer", updated_at=600),
            ],
            preserve_updated_at=True,
        )
        rows = (await SessionListing(db).list_sessions()).value
        assert [r.session_id for r in rows] == ["main", "legacy", "unlabelled"]

    async def test_a_row_carries_its_name_and_run_count(self, db: AsyncSqliteDb) -> None:
        await db.upsert_session(
            _session(runs=[_run(0), _run(1)], session_data={"session_name": "Chat"})
        )
        (row,) = (await SessionListing(db).list_sessions()).value
        assert (row.session_id, row.name) == (SESSION, "Chat")

    async def test_no_db_is_an_ok_empty_list(self) -> None:
        result = await SessionListing(None).list_sessions()
        assert (result.ok, result.value) == (True, [])

    async def test_a_failing_backend_is_reported_with_an_empty_list(
        self, db: AsyncSqliteDb
    ) -> None:
        result = await SessionListing(_Flaky(db, "get_sessions")).list_sessions()
        assert result.ok is False
        assert result.value == []
        assert result.error == "get_sessions is down"

    async def test_the_backend_is_asked_newest_updated_first(self) -> None:
        asked: dict[str, Any] = {}

        class _Spy:
            async def get_sessions(self, **kwargs: Any) -> Any:
                asked.update(kwargs)
                return [], 0

        await SessionListing(_Spy()).list_sessions(limit=3)
        assert asked["sort_by"] == "updated_at"
        assert asked["sort_order"] == "desc"
        assert asked["limit"] == 3
        assert asked["session_type"] == SessionType.AGENT


# ── Naming ───────────────────────────────────────────────────────────


class TestNamer:
    async def test_a_name_that_exists_is_returned_clean_without_asking_the_model(
        self, db: AsyncSqliteDb
    ) -> None:
        await db.upsert_session(_session(session_data={"session_name": '**"Kept title"**'}))
        model = _Model()
        namer = SessionNamer(db, SESSION, SessionDataService(db, SESSION))
        assert await namer.auto_name(model) == "Kept title"
        assert model.asked == []

    async def test_a_stored_decorated_name_reads_back_clean(self, db: AsyncSqliteDb) -> None:
        await db.upsert_session(_session(session_data={"session_name": "# Heading title"}))
        namer = SessionNamer(db, SESSION, SessionDataService(db, SESSION))
        assert await namer.get_name() == "Heading title"
        assert await namer._get_name_raw() == "# Heading title"

    async def test_the_model_titles_a_session_from_its_opening_and_the_title_is_stored(
        self, db: AsyncSqliteDb
    ) -> None:
        await RunHistory(db).save(_session(runs=[_run(0)]))
        model = _Model("Port lookup")
        namer = SessionNamer(db, SESSION, SessionDataService(db, SESSION))
        assert await namer.auto_name(model) == "Port lookup"
        assert len(model.asked) == 1
        assert "question 0" in model.asked[0][1].content
        assert (await _stored(db)).session_data["session_name"] == "Port lookup"

    async def test_only_the_first_two_runs_make_the_title(self, db: AsyncSqliteDb) -> None:
        await RunHistory(db).save(_session(runs=[_run(0), _run(1), _run(2), _run(3)]))
        model = _Model()
        namer = SessionNamer(db, SESSION, SessionDataService(db, SESSION))
        await namer.auto_name(model)
        prompt = model.asked[0][1].content
        assert "question 0" in prompt
        assert "question 1" in prompt
        assert "question 2" not in prompt
        assert "question 3" not in prompt

    async def test_a_busy_model_is_not_asked_and_the_title_is_the_opening(
        self, db: AsyncSqliteDb
    ) -> None:
        opening = "Why does the nightly export stall on the third shard every time"
        run = _run(0)
        run.input = RunInput(input_content=opening)
        await RunHistory(db).save(_session(runs=[run]))
        model = _Model()
        namer = SessionNamer(db, SESSION, SessionDataService(db, SESSION))
        title = await namer.auto_name(model, model_busy=True)
        assert title == "Why does the nightly export stall…"
        assert model.asked == []
        assert (await _stored(db)).session_data["session_name"] == title

    async def test_nothing_to_title_names_nothing(self, db: AsyncSqliteDb) -> None:
        namer = SessionNamer(db, SESSION, SessionDataService(db, SESSION))
        model = _Model()
        assert await namer.auto_name(model) == ""  # no row at all
        await db.upsert_session(_session(runs=[]))
        assert await namer.auto_name(model) == ""  # a row with no runs
        blank = _run(0)
        blank.input = RunInput(input_content="   ")
        await RunHistory(db).save(_session(runs=[blank]))
        assert await namer.auto_name(model) == ""  # a blank opening
        assert model.asked == []

    async def test_a_title_that_cannot_be_written_is_not_claimed(self, db: AsyncSqliteDb) -> None:
        await RunHistory(db).save(_session(runs=[_run(0)]))
        down = _Flaky(db, "rename_session")
        namer = SessionNamer(down, SESSION, SessionDataService(down, SESSION))
        assert await namer.auto_name(_Model()) == ""
        assert not (await _stored(db)).session_data.get("session_name")

    async def test_rename_updates_the_row_and_reports_a_failure(self, db: AsyncSqliteDb) -> None:
        await db.upsert_session(_session(session_data={"session_name": "Before"}))
        namer = SessionNamer(db, SESSION, SessionDataService(db, SESSION))
        assert (await namer.rename("After")).ok
        assert await namer.get_name() == "After"

        down = _Flaky(db, "rename_session")
        failed = await SessionNamer(down, SESSION, SessionDataService(down, SESSION)).rename("X")
        assert failed.ok is False
        assert failed.error == "rename_session is down"

    async def test_title_creates_the_row_when_rename_found_none_and_keeps_an_existing_one(
        self, db: AsyncSqliteDb
    ) -> None:
        namer = SessionNamer(db, SESSION, SessionDataService(db, SESSION))
        assert (await namer.title("Born named")).ok
        assert (await _stored(db)).session_data == {"session_name": "Born named"}

        await db.upsert_session(
            _session("has-row", session_data={"session_name": "Old", "todos": []})
        )
        other = SessionNamer(db, "has-row", SessionDataService(db, "has-row"))
        assert (await other.title("New")).ok
        assert (await _stored(db, "has-row")).session_data == {
            "session_name": "New",
            "todos": [],
        }

    async def test_title_reports_a_failed_rename_without_creating_a_row(
        self, db: AsyncSqliteDb
    ) -> None:
        down = _Flaky(db, "rename_session")
        namer = SessionNamer(down, SESSION, SessionDataService(down, SESSION))
        assert (await namer.title("X")).ok is False
        assert await _stored(db) is None

    async def test_rebind_points_the_namer_at_the_new_id(self, db: AsyncSqliteDb) -> None:
        await db.upsert_session(_session("a", session_data={"session_name": "A"}))
        await db.upsert_session(_session("b", session_data={"session_name": "B"}))
        namer = SessionNamer(db, "a", SessionDataService(db, "a"))
        assert await namer.get_name() == "A"
        namer.rebind("b")
        assert await namer.get_name() == "B"
        assert (await namer.rename("B2")).ok
        assert (await _stored(db, "a")).session_data["session_name"] == "A"
        assert (await _stored(db, "b")).session_data["session_name"] == "B2"

    async def test_without_a_db_the_namer_is_quiet(self) -> None:
        namer = SessionNamer(None, SESSION, SessionDataService(None, SESSION))
        assert await namer.get_name() == ""
        assert (await namer.rename("x")).ok
        assert await namer.auto_name(_Model()) == ""

    async def test_an_unreadable_session_names_nothing(self, db: AsyncSqliteDb) -> None:
        down = _Flaky(db, "get_session")
        namer = SessionNamer(down, SESSION, SessionDataService(down, SESSION))
        assert await namer.get_name() == ""
        assert await namer.auto_name(_Model()) == ""


# ── Forking ──────────────────────────────────────────────────────────


class TestForker:
    async def test_a_fork_is_a_new_row_with_fresh_stamps_and_the_source_untouched(
        self, db: AsyncSqliteDb
    ) -> None:
        source = _session(runs=[_run(0)], session_data={"session_name": "Source", "k": 1})
        source.created_at = 10
        source.updated_at = 20
        await RunHistory(db).save(source)

        result = await SessionForker(db, SESSION).fork()
        assert result.ok
        assert len(result.new_session_id) == 8
        assert result.new_session_id != SESSION

        copy = await _stored(db, result.new_session_id)
        assert copy.session_data == {"session_name": "Source", "k": 1}
        assert copy.created_at > 20
        assert copy.updated_at > 20
        original = await _stored(db)
        assert original.created_at == 10
        assert [r.run_id for r in original.runs] == ["run-0"]

    async def test_a_name_renames_the_copy_only_and_an_empty_one_keeps_the_source_name(
        self, db: AsyncSqliteDb
    ) -> None:
        await db.upsert_session(_session(session_data={"session_name": "Source"}))
        forker = SessionForker(db, SESSION)
        named = await forker.fork(name="Copy")
        blank = await forker.fork(name="")
        assert (await _stored(db, named.new_session_id)).session_data["session_name"] == "Copy"
        assert (await _stored(db, blank.new_session_id)).session_data["session_name"] == "Source"
        assert (await _stored(db)).session_data["session_name"] == "Source"

    async def test_a_name_is_set_on_a_source_that_has_no_session_data(
        self, db: AsyncSqliteDb
    ) -> None:
        await db.upsert_session(_session(session_data={}))
        result = await SessionForker(db, SESSION).fork(name="Copy")
        assert (await _stored(db, result.new_session_id)).session_data == {"session_name": "Copy"}

    async def test_a_fork_of_a_session_with_no_runs_has_none(self, db: AsyncSqliteDb) -> None:
        await db.upsert_session(_session())
        result = await SessionForker(db, SESSION).fork()
        assert result.ok
        assert (await _stored(db, result.new_session_id)).runs in (None, [])

    async def test_the_copied_runs_belong_to_the_copy_and_their_parent_links_follow(
        self, db: AsyncSqliteDb
    ) -> None:
        await RunHistory(db).save(
            _session(runs=[_run(0), _run(1, parent="run-0"), _run(2, parent="elsewhere")])
        )
        result = await SessionForker(db, SESSION).fork()
        copy = await _stored(db, result.new_session_id)
        assert {r.session_id for r in copy.runs} == {result.new_session_id}
        assert copy.runs[1].parent_run_id == copy.runs[0].run_id
        assert copy.runs[2].parent_run_id == "elsewhere"

    async def test_a_failed_write_is_reported_and_leaves_no_copy(self, db: AsyncSqliteDb) -> None:
        await db.upsert_session(_session())
        result = await SessionForker(_Flaky(db, "upsert_session"), SESSION).fork()
        assert result.ok is False
        assert result.error == "upsert_session is down"
        assert result.new_session_id == ""

    async def test_a_failed_read_a_missing_source_and_no_db_are_reported(
        self, db: AsyncSqliteDb
    ) -> None:
        assert (await SessionForker(_Flaky(db, "get_session"), SESSION).fork()).error == (
            "get_session is down"
        )
        missing = await SessionForker(db, "nope").fork()
        assert (missing.ok, missing.error) == (False, "source session not found: nope")
        assert (await SessionForker(None, SESSION).fork()).error == "session store unavailable"

    async def test_rebind_forks_the_new_id(self, db: AsyncSqliteDb) -> None:
        await db.upsert_session(_session("a", session_data={"session_name": "A"}))
        await db.upsert_session(_session("b", session_data={"session_name": "B"}))
        forker = SessionForker(db, "a")
        forker.rebind("b")
        result = await forker.fork()
        assert (await _stored(db, result.new_session_id)).session_data["session_name"] == "B"


# ── Run history ──────────────────────────────────────────────────────


async def _run_ids(db: Any, session_id: str = SESSION) -> list[str]:
    stored = await _stored(db, session_id)
    return [r.run_id for r in stored.runs or []]


class TestRunHistory:
    async def test_a_session_with_no_runs_clears_the_stored_ones(self, db: AsyncSqliteDb) -> None:
        history = RunHistory(db)
        await history.save(_session(runs=[_run(0), _run(1)]))
        await history.save(_session(runs=[]))
        assert await _run_ids(db) == []

    async def test_a_session_whose_runs_are_none_clears_them_too(self, db: AsyncSqliteDb) -> None:
        history = RunHistory(db)
        await history.save(_session(runs=[_run(0)]))
        stored = await _stored(db)
        stored.runs = None
        await history.save(stored)
        assert await _run_ids(db) == []

    async def test_each_run_is_stored_with_its_place_in_the_session(self) -> None:
        stored: list[tuple[str, int | None]] = []

        class _Db:
            async def upsert_session(self, *_a: Any, **_kw: Any) -> None:
                return None

            async def get_runs(self, **_kw: Any) -> list[Any]:
                return []

            async def upsert_run(self, run: Any, **kwargs: Any) -> None:
                stored.append((run.run_id, kwargs["run_index"]))

        await RunHistory(_Db()).save(_session(runs=[_run(2), _run(0), _run(1)]))
        assert stored == [("run-2", 0), ("run-0", 1), ("run-1", 2)]

    async def test_a_run_is_stored_with_the_sessions_user(self, db: AsyncSqliteDb) -> None:
        seen: dict[str, Any] = {}

        class _Spy:
            async def upsert_run(self, run: Any, **kwargs: Any) -> None:
                seen.update(kwargs)

        session = _session()
        session.user_id = "user-7"
        await RunHistory(_Spy()).save_run(session, _run(0), run_index=4)
        assert seen == {"session_id": SESSION, "user_id": "user-7", "run_index": 4}

    async def test_save_for_saves_the_row_the_agents_way_then_the_runs(
        self, db: AsyncSqliteDb
    ) -> None:
        order: list[str] = []

        class _Agent:
            def __init__(self, store: AsyncSqliteDb) -> None:
                self.db = store

            async def asave_session(self, session: AgentSession) -> None:
                order.append("row")
                await self.db.upsert_session(session)

        await RunHistory(db).save(_session(runs=[_run(0), _run(1)]))
        session = await _stored(db)
        session.runs = session.runs[:1]
        await RunHistory.save_for(_Agent(db), session)
        assert await _run_ids(db) == ["run-0"]
        assert order == ["row"]

    async def test_a_backend_that_hands_runs_as_a_tuple_is_read(self) -> None:
        deleted: list[list[str]] = []

        class _Db:
            async def get_runs(self, **_kw: Any) -> Any:
                return [_run(0), _run(1)], 2

            async def delete_runs(self, ids: list[str]) -> None:
                deleted.append(ids)

            async def upsert_run(self, *_a: Any, **_kw: Any) -> None:
                return None

        await RunHistory(_Db()).replace_runs(_session(runs=[_run(0)]))
        assert deleted == [["run-1"]]

    async def test_a_paused_run_is_closed_in_error_with_the_note(self, db: AsyncSqliteDb) -> None:
        paused = _run(1)
        paused.status = RunStatus.paused
        await RunHistory(db).save(_session(runs=[_run(0), paused]))

        assert await RunHistory(db).close_interrupted(SESSION, "run-1", "cut off") is True
        stored = await _stored(db)
        assert stored.runs[1].status == RunStatus.error
        assert stored.runs[1].content == "cut off"
        assert stored.runs[0].content == "answer 0"
        assert await _run_ids(db) == ["run-0", "run-1"]

    async def test_a_run_that_already_ended_is_found_and_left_alone(
        self, db: AsyncSqliteDb
    ) -> None:
        done = _run(0)
        done.status = RunStatus.completed
        await RunHistory(db).save(_session(runs=[done]))
        assert await RunHistory(db).close_interrupted(SESSION, "run-0", "note") is True
        stored = await _stored(db)
        assert stored.runs[0].status == RunStatus.completed
        assert stored.runs[0].content == "answer 0"

    async def test_a_missing_session_or_run_is_not_found(self, db: AsyncSqliteDb) -> None:
        history = RunHistory(db)
        assert await history.close_interrupted("nope", "run-0", "n") is False
        await history.save(_session(runs=[_run(0)]))
        assert await history.close_interrupted(SESSION, "run-9", "n") is False

    def test_copies_have_fresh_ids_and_parent_links_inside_the_set_follow(self) -> None:
        runs = [_run(0), _run(1, parent="run-0"), _run(2, parent="outside")]
        copies = RunHistory.copied(runs)
        assert [c.run_id for c in copies] != ["run-0", "run-1", "run-2"]
        assert len({c.run_id for c in copies}) == 3
        assert copies[1].parent_run_id == copies[0].run_id
        assert copies[2].parent_run_id == "outside"
        # The originals are not touched, and the copies are deep.
        assert [r.run_id for r in runs] == ["run-0", "run-1", "run-2"]
        assert runs[1].parent_run_id == "run-0"
        assert copies[0] is not runs[0]
        assert copies[0].messages is not runs[0].messages

    def test_a_run_without_an_id_is_copied_as_it_is(self) -> None:
        anonymous = _run(0)
        anonymous.run_id = None
        (copy,) = RunHistory.copied([anonymous])
        assert copy.run_id is None
        assert copy is not anonymous
