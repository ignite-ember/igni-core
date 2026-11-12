"""A session's id as the database holds it: the scope, then the plain id.

``igni_cloud.storage.scoped_session_ids``: the pure id arithmetic behind
:class:`ScopedAsyncPostgresDb`, without a database.
"""

from __future__ import annotations

from agno.run.agent import RunOutput
from agno.session import AgentSession
from igni_cloud.storage.scoped_session_ids import ScopedSessionIds

SCOPE = "acme/person:ada/project"


def test_stored_puts_the_scope_first_and_plain_takes_it_off():
    ids = ScopedSessionIds(SCOPE)
    assert ids.prefix == f"{SCOPE}#"
    assert ids.stored("s1") == f"{SCOPE}#s1"
    assert ids.plain(ids.stored("s1")) == "s1"
    assert ids.stored("") == f"{SCOPE}#"
    assert ids.plain(ids.stored("")) == ""


def test_plain_takes_off_only_the_leading_scope_once():
    ids = ScopedSessionIds(SCOPE)
    for odd in ["a#b", f"{SCOPE}#inner", "ünï#cødé", "#"]:
        assert ids.plain(ids.stored(odd)) == odd
    assert ids.plain("no-prefix") == "no-prefix"
    foreign = "other/person:bob/project#s1"
    assert ids.plain(foreign) == foreign


def test_a_row_is_handed_back_with_the_plain_id_and_the_input_untouched():
    ids = ScopedSessionIds(SCOPE)
    row = {"session_id": ids.stored("s1"), "agent_id": "igni"}
    plain = ids.plain_row(row)
    assert plain == {"session_id": "s1", "agent_id": "igni"}
    assert row["session_id"] == f"{SCOPE}#s1"


def test_a_row_without_a_string_session_id_is_returned_as_it_is():
    ids = ScopedSessionIds(SCOPE)
    for row in [{"agent_id": "igni"}, {"session_id": None}, {"session_id": 7}]:
        assert ids.plain_row(row) is row


def test_a_page_of_rows_is_unscoped_and_keeps_its_total():
    ids = ScopedSessionIds(SCOPE)
    rows = [{"session_id": ids.stored("a")}, {"session_id": ids.stored("b")}]
    assert ids.plain_rows_page((rows, 42)) == ([{"session_id": "a"}, {"session_id": "b"}], 42)
    assert ids.plain_rows_page(([], 5)) == ([], 5)


def test_a_session_and_a_run_are_unscoped_in_place():
    ids = ScopedSessionIds(SCOPE)
    session = AgentSession(session_id=ids.stored("s1"), agent_id="igni")
    assert ids.plain_session(session) is session
    assert session.session_id == "s1"
    run = RunOutput(run_id="r", session_id=ids.stored("s1"))
    assert ids.plain_run(run) is run
    assert run.session_id == "s1"


def test_a_run_without_a_session_is_left_alone():
    ids = ScopedSessionIds(SCOPE)
    run = RunOutput(run_id="r")
    assert ids.plain_run(run).session_id is None


def test_what_was_found_is_unscoped_whatever_shape_it_has():
    ids = ScopedSessionIds(SCOPE)
    assert ids.plain_found_session(None) is None
    assert ids.plain_found_run(None) is None
    assert ids.plain_found_session({"session_id": ids.stored("s1")}) == {"session_id": "s1"}
    assert ids.plain_found_run({"session_id": ids.stored("s1")}) == {"session_id": "s1"}
    session = AgentSession(session_id=ids.stored("s1"), agent_id="igni")
    assert ids.plain_found_session(session).session_id == "s1"  # type: ignore[union-attr]
    run = RunOutput(run_id="r", session_id=ids.stored("s1"))
    assert ids.plain_found_run(run).session_id == "s1"  # type: ignore[union-attr]
