"""The approval journal and the stores beside it, driven directly on a real SQLite file.

``ApprovalJournal`` (waiting approvals), ``SpawnLinks`` (paused sub-agent runs) and
``TurnResults`` (finished calls) write best-effort into the project's database:
rows are scoped, replaced by id, announced to the app bus when they ask a person,
and a database that fails gives ``ok=False`` or nothing, never an exception.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement
from pydantic import ValidationError
from sqlalchemy import insert, inspect, text

from igni_core.core.approvals.events import (
    ApprovalChange,
    ApprovalEvents,
    ApprovalState,
    approval_events,
)
from igni_core.core.approvals.journal import ApprovalJournal
from igni_core.core.approvals.records import ApprovalOrigin, SpawnLink, StoredApproval
from igni_core.core.approvals.tables import (
    TurnResultModel,
    WaitingApprovalModel,
    WaitingSpawnModel,
)
from igni_core.core.db.database import Database
from igni_core.core.db.scope import StorageScope, Tenant
from igni_core.core.tools.spawn_context import ParentCall
from igni_core.protocol import messages as msg


def _requirement(call_id: str) -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_call_id=call_id,
            tool_name="run_shell_command",
            tool_args={"command": call_id},
            requires_confirmation=True,
        )
    )


def _request(call_id: str, tool: str = "run_shell_command") -> msg.HITLRequest:
    return msg.HITLRequest(requirement_id=call_id, tool_name=tool, friendly_name="Shell")


def _parent(call_id: str = "c1") -> ParentCall:
    return ParentCall(
        agent_name="main",
        agent_id="a1",
        run_id="run-main",
        session_id="s1",
        tool_call_id=call_id,
        tool_name="spawn_agent",
        tool_args={"task": "x", "n": 2},
        messages=[{"role": "assistant", "content": "héllo"}],
    )


def _scope(project: str = "p") -> StorageScope:
    return StorageScope.for_tenant(Tenant(org="org", producer="person:1"), project=project)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "approvals.db")


@pytest.fixture
def journal(db: Database) -> ApprovalJournal:
    return ApprovalJournal(db)


@pytest.fixture
def changes() -> list[ApprovalChange]:
    heard: list[ApprovalChange] = []
    stop = approval_events.watch(heard.append)
    yield heard
    stop()


def _record(journal: ApprovalJournal, rid: str, origin=ApprovalOrigin.MAIN, **kw):
    kw.setdefault("session_id", "s1")
    kw.setdefault("run_id", "run-1")
    kw.setdefault("request", _request(rid))
    return journal.record(requirement_id=rid, origin=origin, requirement=_requirement(rid), **kw)


def _break(db: Database, table: str) -> None:
    with db.sync_session() as session, session.begin():
        session.execute(text(f"DROP TABLE {table}"))


# --- records ---------------------------------------------------------------


def test_only_main_and_sub_agent_origins_ask_a_person():
    assert {o.value: o.asks_a_person for o in ApprovalOrigin} == {
        "main": True,
        "sub_agent": True,
        "auto_resolved": False,
        "continuing": False,
    }
    with pytest.raises(ValueError):
        ApprovalOrigin("bogus")


def test_a_spawn_links_parents_default_to_a_list_of_its_own():
    one = SpawnLink(run_id="r", session_id="s", agent_session_id="a", agent_name="n")
    two = SpawnLink(run_id="r", session_id="s", agent_session_id="a", agent_name="n")
    one.parents.append(_parent())
    assert two.parents == []


def test_a_stored_approval_defaults_to_no_request_and_no_agent_path():
    stored = StoredApproval(
        requirement_id="r",
        session_id="s",
        run_id="run",
        origin=ApprovalOrigin.MAIN,
        requirement=_requirement("r"),
    )
    assert stored.request is None
    assert stored.agent_path == []


# --- the journal -----------------------------------------------------------


def test_a_recorded_approval_is_read_back_with_its_request_and_agent_path(journal):
    request = _request("a").model_copy(update={"agent_path": ["main", "worker"]})
    assert _record(journal, "a", request=request).ok

    [stored] = journal.waiting("s1")
    assert stored.requirement_id == "a"
    assert stored.run_id == "run-1"
    assert stored.origin is ApprovalOrigin.MAIN
    assert stored.requirement.tool_execution.tool_call_id == "a"
    assert stored.request == request
    assert stored.agent_path == ["main", "worker"]


def test_a_record_without_request_or_run_reads_back_as_none_and_empty(journal):
    _record(journal, "a", origin=ApprovalOrigin.AUTO_RESOLVED, request=None, run_id=None)

    [stored] = journal.waiting("s1")
    assert stored.request is None
    assert stored.agent_path == []
    assert stored.run_id == ""


def test_recording_the_same_requirement_again_replaces_the_row(journal):
    _record(journal, "a", run_id="old")
    _record(journal, "a", run_id="new")

    assert [s.run_id for s in journal.waiting("s1")] == ["new"]


def test_waiting_gives_one_sessions_rows_oldest_first_with_id_as_tie_break(
    journal, db, monkeypatch
):
    clock = iter([100, 100, 50, 100])
    monkeypatch.setattr(
        "igni_core.core.approvals.journal.time", SimpleNamespace(time=lambda: next(clock))
    )
    _record(journal, "b")
    _record(journal, "a")
    _record(journal, "old")
    _record(journal, "other-session", session_id="s2")

    assert [s.requirement_id for s in journal.waiting("s1")] == ["old", "a", "b"]
    assert [s.requirement_id for s in journal.waiting("s2")] == ["other-session"]
    assert journal.waiting("nobody") == []


def test_a_requirement_with_values_json_cannot_hold_is_still_recorded(journal):
    requirement = _requirement("a")
    requirement.tool_execution.tool_args = {"path": Path("/x/é")}
    assert journal.record(
        requirement_id="a",
        session_id="s1",
        run_id="r",
        origin=ApprovalOrigin.MAIN,
        requirement=requirement,
    ).ok
    assert len(journal.waiting("s1")) == 1


def test_forget_removes_one_approval_and_an_unknown_id_is_fine(journal):
    _record(journal, "a")
    _record(journal, "b")

    assert journal.forget("a").ok
    assert journal.forget("nope").ok
    assert [s.requirement_id for s in journal.waiting("s1")] == ["b"]


def test_forget_run_removes_the_run_or_only_one_origin_of_it(journal):
    _record(journal, "m", origin=ApprovalOrigin.MAIN, run_id="r")
    _record(journal, "s", origin=ApprovalOrigin.SUB_AGENT, run_id="r")
    _record(journal, "x", origin=ApprovalOrigin.MAIN, run_id="other")

    journal.forget_run("r", origin=ApprovalOrigin.SUB_AGENT)
    assert [s.requirement_id for s in journal.waiting("s1")] == ["m", "x"]

    journal.forget_run("r")
    assert [s.requirement_id for s in journal.waiting("s1")] == ["x"]


def test_update_request_changes_only_the_shown_request(journal):
    _record(journal, "a", run_id="r")
    _record(journal, "b", run_id="r")
    better = _request("a").model_copy(update={"friendly_name": "Better"})

    assert journal.update_request("a", better).ok

    by_id = {s.requirement_id: s for s in journal.waiting("s1")}
    assert by_id["a"].request == better
    assert by_id["a"].run_id == "r"
    assert by_id["b"].request.friendly_name == "Shell"


def test_the_continuing_marker_is_written_and_forgotten_without_telling_an_app(journal, changes):
    assert journal.record_continuing(session_id="s1", run_id="run-9").ok
    [stored] = journal.waiting("s1")
    assert (stored.requirement_id, stored.origin, stored.run_id, stored.request) == (
        "continuing-run-9",
        ApprovalOrigin.CONTINUING,
        "run-9",
        None,
    )

    assert journal.forget_continuing("run-9").ok
    assert journal.waiting("s1") == []
    assert changes == []


def test_auto_resolved_rows_get_their_own_prefixed_id_and_stay_silent(journal, changes):
    for _ in range(2):
        journal.record_auto_resolved(session_id="s1", run_id="r", requirement=_requirement("x"))

    stored = journal.waiting("s1")
    ids = [s.requirement_id for s in stored]
    assert len(set(ids)) == 2
    assert all(i.startswith("auto-") and len(i) == len("auto-") + 12 for i in ids)
    assert {s.origin for s in stored} == {ApprovalOrigin.AUTO_RESOLVED}
    journal.forget_run("r")
    assert changes == []


def test_an_app_hears_waiting_and_then_resolved_for_approvals_that_ask_a_person(journal, changes):
    _record(journal, "m", origin=ApprovalOrigin.MAIN, request=_request("m", "edit_file"))
    _record(journal, "s", origin=ApprovalOrigin.SUB_AGENT, run_id="r2")
    journal.forget("m")
    journal.forget_run("r2")

    assert changes == [
        ApprovalChange(
            session_id="s1",
            requirement_id="m",
            run_id="run-1",
            state=ApprovalState.WAITING,
            origin="main",
            tool_name="edit_file",
        ),
        ApprovalChange(
            session_id="s1",
            requirement_id="s",
            run_id="r2",
            state=ApprovalState.WAITING,
            origin="sub_agent",
            tool_name="run_shell_command",
        ),
        ApprovalChange(
            session_id="s1",
            requirement_id="m",
            run_id="run-1",
            state=ApprovalState.RESOLVED,
            origin="main",
        ),
        ApprovalChange(
            session_id="s1",
            requirement_id="s",
            run_id="r2",
            state=ApprovalState.RESOLVED,
            origin="sub_agent",
        ),
    ]


def test_forgetting_an_unknown_approval_tells_nobody(journal, changes):
    journal.forget("nope")
    assert changes == []


def test_a_failing_listener_does_not_stop_the_journal(journal):
    def broken(_: ApprovalChange) -> None:
        raise RuntimeError("app is gone")

    stop = approval_events.watch(broken)
    try:
        assert _record(journal, "a").ok
        assert journal.forget("a").ok
    finally:
        stop()


def test_another_producers_or_projects_approvals_are_invisible(db):
    mine = ApprovalJournal(db.in_scope(_scope("p")))
    other_project = ApprovalJournal(db.in_scope(_scope("q")))
    other_producer = ApprovalJournal(
        db.in_scope(StorageScope.for_tenant(Tenant(org="org", producer="person:2"), project="p"))
    )
    _record(mine, "a")
    _record(other_project, "a", run_id="q-run")
    _record(other_producer, "a", run_id="o-run")

    other_project.forget_run("run-1")
    other_producer.forget("a")

    assert [s.run_id for s in mine.waiting("s1")] == ["run-1"]
    assert [s.run_id for s in other_project.waiting("s1")] == ["q-run"]
    assert other_producer.waiting("s1") == []
    mine.forget("a")
    assert mine.waiting("s1") == []
    assert [s.run_id for s in other_project.waiting("s1")] == ["q-run"]


def _bad_rows(db: Database) -> None:
    base = {"org": "", "producer": "", "project": "", "session_id": "s1", "run_id": "r"}
    good = _requirement("g").to_dict()
    import json

    rows = [
        ("bad-json", "main", "{not json", ""),
        ("bad-request", "main", json.dumps(good), "{not json"),
        ("bad-origin", "bogus", json.dumps(good), ""),
        ("not-a-dict", "main", "[1]", ""),
    ]
    with db.sync_session() as session, session.begin():
        for n, (rid, origin, requirement, request) in enumerate(rows):
            session.execute(
                insert(WaitingApprovalModel).values(
                    **base,
                    requirement_id=rid,
                    origin=origin,
                    requirement=requirement,
                    request=request,
                    created_at=n,
                )
            )


def test_unreadable_rows_are_skipped_with_a_warning_and_the_rest_come_back(journal, db, caplog):
    _bad_rows(db)
    _record(journal, "good")

    with caplog.at_level(logging.WARNING):
        assert [s.requirement_id for s in journal.waiting("s1")] == ["good"]

    warned = "\n".join(r.getMessage() for r in caplog.records)
    for rid in ("bad-json", "bad-request", "bad-origin", "not-a-dict"):
        assert f"unreadable row {rid}" in warned


def test_deleting_a_row_with_an_unknown_origin_does_not_raise_or_announce(journal, db, changes):
    _bad_rows(db)

    assert journal.forget("bad-origin").ok
    assert changes == []
    with db.sync_session() as session:
        assert session.query(WaitingApprovalModel).count() == 3


def test_a_failing_database_gives_ok_false_and_nothing_to_read(journal, db, caplog):
    _break(db, "waiting_approvals")

    with caplog.at_level(logging.WARNING):
        written = _record(journal, "a")
        assert not written.ok
        assert "waiting_approvals" in written.error
        assert not journal.forget("a").ok
        assert not journal.forget_run("r").ok
        assert not journal.update_request("a", _request("a")).ok
        assert journal.waiting("s1") == []

    assert "approval journal: record a failed" in caplog.text
    assert "approval journal: read for s1 failed" in caplog.text


def test_an_approval_that_could_not_be_written_is_still_announced(journal, db, changes):
    _break(db, "waiting_approvals")

    assert not _record(journal, "a").ok
    assert [c.state for c in changes] == [ApprovalState.WAITING]


def test_an_error_that_is_not_the_databases_is_not_swallowed(journal):
    def explode(_session):
        raise ZeroDivisionError

    with pytest.raises(ZeroDivisionError):
        journal._write("explode", explode)


def test_a_write_that_fails_part_way_leaves_nothing_of_it(journal, db):
    from sqlalchemy.exc import IntegrityError

    def half(session):
        session.add(
            WaitingApprovalModel(
                requirement_id="half",
                session_id="s1",
                run_id="",
                origin="main",
                requirement="{}",
                request="",
                created_at=1,
            )
        )
        session.flush()
        raise IntegrityError("stmt", {}, Exception("boom"))

    written = journal._write("half", half)

    assert not written.ok
    assert "boom" in written.error
    with db.sync_session() as session:
        assert session.query(WaitingApprovalModel).count() == 0


# --- spawn links -----------------------------------------------------------


def _link(run_id: str = "sub-1", session_id: str = "s1", parents=None) -> SpawnLink:
    return SpawnLink(
        run_id=run_id,
        session_id=session_id,
        agent_session_id="agent-s",
        agent_name="worker",
        parents=[_parent()] if parents is None else parents,
    )


def test_a_spawn_link_round_trips_with_its_parents(journal):
    link = _link(parents=[_parent("c1"), _parent("c2")])
    assert journal.spawns.record(link).ok

    assert journal.spawns.get("sub-1") == link
    assert journal.spawns.get("unknown") is None


def test_a_link_with_no_parents_round_trips(journal):
    journal.spawns.record(_link(parents=[]))
    assert journal.spawns.get("sub-1").parents == []


def test_recording_a_run_again_replaces_its_link(journal):
    journal.spawns.record(_link())
    journal.spawns.record(_link().model_copy(update={"agent_name": "second"}))

    assert journal.spawns.get("sub-1").agent_name == "second"
    assert len(journal.spawns.of_session("s1")) == 1


def test_of_session_gives_only_that_sessions_links(journal):
    journal.spawns.record(_link("a", "s1"))
    journal.spawns.record(_link("b", "s1"))
    journal.spawns.record(_link("c", "s2"))

    assert sorted(link.run_id for link in journal.spawns.of_session("s1")) == ["a", "b"]
    assert journal.spawns.of_session("none") == []


def test_forgetting_a_link_removes_only_that_run_and_an_absent_one_is_fine(journal):
    journal.spawns.record(_link("a"))
    journal.spawns.record(_link("b"))

    assert journal.spawns.forget("a").ok
    assert journal.spawns.forget("zzz").ok
    assert journal.spawns.get("a") is None
    assert journal.spawns.get("b") is not None


def test_a_link_with_unreadable_parents_is_skipped_with_a_warning(journal, db, caplog):
    journal.spawns.record(_link("good"))
    for run_id, parents in (("bad-json", "{nope"), ("bad-parent", '[{"agent_name": 1}]')):
        with db.sync_session() as session, session.begin():
            session.execute(
                insert(WaitingSpawnModel).values(
                    org="",
                    producer="",
                    project="",
                    run_id=run_id,
                    session_id="s1",
                    agent_session_id="a",
                    agent_name="n",
                    parents=parents,
                    created_at=1,
                )
            )

    with caplog.at_level(logging.WARNING):
        assert journal.spawns.get("bad-json") is None
        assert journal.spawns.get("bad-parent") is None
        assert [link.run_id for link in journal.spawns.of_session("s1")] == ["good"]
    assert "unreadable spawn bad-json" in caplog.text


def test_spawn_links_are_scoped(db):
    mine = ApprovalJournal(db.in_scope(_scope("p"))).spawns
    theirs = ApprovalJournal(db.in_scope(_scope("q"))).spawns
    mine.record(_link())
    theirs.record(_link().model_copy(update={"agent_name": "theirs"}))

    theirs.forget("sub-1")

    assert mine.get("sub-1").agent_name == "worker"
    assert theirs.get("sub-1") is None
    assert [link.agent_name for link in mine.of_session("s1")] == ["worker"]


def test_spawn_link_failures_are_results_not_exceptions(journal, db):
    _break(db, "waiting_spawns")

    assert not journal.spawns.record(_link()).ok
    assert journal.spawns.get("sub-1") is None
    assert journal.spawns.of_session("s1") == []
    assert not journal.spawns.forget("sub-1").ok


# --- turn results ----------------------------------------------------------


def test_turn_results_come_back_keyed_by_call_for_the_run(journal):
    journal.turns.record("r", "c1", "one")
    journal.turns.record("r", "c2", "")
    journal.turns.record("other", "c1", "elsewhere")

    assert journal.turns.of_run("r") == {"c1": "one", "c2": ""}
    assert journal.turns.of_run("none") == {}


def test_a_result_recorded_again_for_the_same_call_replaces_the_old_one(journal):
    journal.turns.record("r", "c1", "old")
    journal.turns.record("r", "c1", "new")

    assert journal.turns.of_run("r") == {"c1": "new"}


def test_large_and_unicode_results_round_trip(journal):
    big = "é" * 200_000 + "日本"
    journal.turns.record("r", "c1", big)

    assert journal.turns.of_run("r") == {"c1": big}


def test_forgetting_a_run_removes_only_its_results(journal):
    journal.turns.record("r", "c1", "a")
    journal.turns.record("r", "c2", "b")
    journal.turns.record("other", "c1", "c")

    assert journal.turns.forget("r").ok
    assert journal.turns.forget("absent").ok
    assert journal.turns.of_run("r") == {}
    assert journal.turns.of_run("other") == {"c1": "c"}


def test_turn_results_are_scoped(db):
    mine = ApprovalJournal(db.in_scope(_scope("p"))).turns
    theirs = ApprovalJournal(db.in_scope(_scope("q"))).turns
    mine.record("r", "c1", "mine")
    theirs.record("r", "c1", "theirs")

    theirs.forget("r")

    assert mine.of_run("r") == {"c1": "mine"}
    assert theirs.of_run("r") == {}


def test_turn_result_failures_are_results_not_exceptions(journal, db):
    _break(db, "turn_results")

    assert not journal.turns.record("r", "c1", "x").ok
    assert journal.turns.of_run("r") == {}
    assert not journal.turns.forget("r").ok


# --- tables ----------------------------------------------------------------


def test_the_three_tables_have_scoped_keys_and_the_session_index(db):
    inspector = inspect(db.sync_session().get_bind())

    def pk(table):
        return inspector.get_pk_constraint(table)["constrained_columns"]

    assert pk("waiting_approvals") == ["org", "producer", "project", "requirement_id"]
    assert pk("waiting_spawns") == ["org", "producer", "project", "run_id"]
    assert pk("turn_results") == ["org", "producer", "project", "run_id", "tool_call_id"]
    [index] = [
        i
        for i in inspector.get_indexes("waiting_approvals")
        if i["name"] == "ix_waiting_approvals_session"
    ]
    assert index["column_names"] == ["org", "producer", "project", "session_id"]


def test_run_id_and_request_default_to_empty_in_the_database(db):
    with db.sync_session() as session, session.begin():
        session.execute(
            text(
                "INSERT INTO waiting_approvals (requirement_id, session_id, origin, requirement,"
                " created_at) VALUES ('a', 's', 'main', '{}', 1)"
            )
        )
    with db.sync_session() as session:
        row = session.query(WaitingApprovalModel).one()
        assert (row.run_id, row.request, row.org, row.producer, row.project) == ("", "", "", "", "")


def test_the_same_key_is_refused_within_a_scope_and_allowed_across_scopes(db):
    from sqlalchemy.exc import IntegrityError

    def turn(project):
        return TurnResultModel(
            org="o",
            producer="p",
            project=project,
            run_id="r",
            tool_call_id="c",
            result="x",
            created_at=1,
        )

    with db.sync_session() as session, session.begin():
        session.add_all([turn("one"), turn("two")])
    with pytest.raises(IntegrityError), db.sync_session() as session, session.begin():
        session.add(turn("one"))


# --- events ----------------------------------------------------------------


def _change(state=ApprovalState.WAITING) -> ApprovalChange:
    return ApprovalChange(
        session_id="s", requirement_id="r", run_id="run", state=state, origin="main"
    )


def test_listeners_hear_each_change_in_the_order_they_started_listening():
    events = ApprovalEvents()
    heard: list[str] = []
    events.watch(lambda c: heard.append(f"first:{c.state.value}"))
    events.watch(lambda c: heard.append(f"second:{c.state.value}"))

    events.announce(_change())
    events.announce(_change(ApprovalState.RESOLVED))

    assert heard == ["first:waiting", "second:waiting", "first:resolved", "second:resolved"]


def test_the_stop_call_unsubscribes_once_and_is_safe_to_repeat():
    events = ApprovalEvents()
    heard: list[ApprovalChange] = []
    other: list[ApprovalChange] = []
    stop = events.watch(heard.append)
    events.watch(other.append)

    stop()
    stop()
    events.announce(_change())

    assert heard == []
    assert len(other) == 1


def test_a_raising_listener_does_not_stop_the_others_or_the_announcer():
    events = ApprovalEvents()
    heard: list[ApprovalChange] = []

    def broken(_: ApprovalChange) -> None:
        raise RuntimeError("boom")

    events.watch(broken)
    events.watch(heard.append)

    events.announce(_change())

    assert len(heard) == 1


def test_a_listener_may_stop_itself_while_a_change_is_announced():
    events = ApprovalEvents()
    heard: list[str] = []
    stops: list = []

    def once(_: ApprovalChange) -> None:
        heard.append("once")
        stops[0]()

    stops.append(events.watch(once))
    events.watch(lambda _: heard.append("steady"))

    events.announce(_change())
    events.announce(_change())

    assert heard == ["once", "steady", "steady"]


def test_clear_forgets_every_listener():
    events = ApprovalEvents()
    heard: list[ApprovalChange] = []
    events.watch(heard.append)
    events.watch(heard.append)

    events.clear()
    events.announce(_change())

    assert heard == []


def test_a_change_needs_a_known_state_and_names_no_tool_by_default():
    assert _change().tool_name == ""
    with pytest.raises(ValidationError):
        ApprovalChange(session_id="s", requirement_id="r", run_id="r", state="gone", origin="main")


def test_the_models_describe_the_tables_the_migrations_create(db):
    inspector = inspect(db.sync_session().get_bind())
    for model in (WaitingApprovalModel, WaitingSpawnModel, TurnResultModel):
        table = model.__table__
        assert [c.name for c in table.primary_key.columns] == (
            inspector.get_pk_constraint(table.name)["constrained_columns"]
        )
        assert {c.name for c in table.columns} == {
            c["name"] for c in inspector.get_columns(table.name)
        }
    defaults = {
        c.name: c.server_default.arg
        for c in WaitingApprovalModel.__table__.columns
        if c.server_default
    }
    assert defaults["run_id"] == "" and defaults["request"] == ""
