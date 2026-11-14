"""``SubAgentHITLCoordinator``, one behaviour at a time.

The end-to-end flows (``test_subagent_hitl_e2e.py``, the restart and
multiplexer tests) drive it inside a session. Here the coordinator is
driven directly with real Agno requirements: ids, surfacing once, the
explanation's two paths, how an answer is read, waiting and cancelling,
the journal's restore of what a restart cut off, and the relay count.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from igni_core.core.approvals.journal import ApprovalJournal
from igni_core.core.db.database import Database
from igni_core.core.db.urls import DatabaseUrl
from igni_core.core.hitl.models import DetailUpdate
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.core.tools.spawn_context import ParentCall, SpawnCalls
from tests.storage_backends import Scopes

SESSION = "s1"


def _requirement(call_id: str = "call-1", tool: str = "run_shell_command") -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_call_id=call_id,
            tool_name=tool,
            tool_args={"command": "make deploy"},
            requires_confirmation=True,
        )
    )


def _parent(run_id: str = "main-run") -> ParentCall:
    return ParentCall(
        agent_name="igni",
        run_id=run_id,
        session_id=SESSION,
        tool_call_id="spawn-1",
        tool_name="spawn_agent",
    )


@pytest.fixture
def under_a_spawn() -> Iterator[None]:
    """What a sub-agent's stream handler runs under: the call waiting above it."""
    with SpawnCalls.restored([_parent()]):
        yield


def _durable(db: Database) -> SubAgentHITLCoordinator:
    coordinator = SubAgentHITLCoordinator()
    coordinator.attach_journal(ApprovalJournal(db), lambda: SESSION)
    return coordinator


# ── Pushing and surfacing ─────────────────────────────────────────


async def test_a_pushed_question_gets_a_short_distinct_id_and_wakes_the_relay():
    coordinator = SubAgentHITLCoordinator()
    ids = await asyncio.gather(
        *(coordinator.push_requirement(_requirement(f"c{n}"), run_id="run-1") for n in range(5))
    )
    assert len(set(ids)) == 5
    assert all(len(i) == 8 for i in ids)
    assert coordinator.new_arrival.is_set()
    assert coordinator.has_unresolved()


async def test_a_question_is_surfaced_once_and_can_be_handed_back():
    coordinator = SubAgentHITLCoordinator()
    first = await coordinator.push_requirement(_requirement(), run_id="run-1", agent_path=["a"])
    (handle,) = coordinator.list_new_pending()
    assert handle.requirement_id == first
    assert coordinator.new_arrival.is_set() is False
    assert coordinator.list_new_pending() == []

    coordinator.hand_back([handle])
    assert coordinator.new_arrival.is_set()
    assert [h.requirement_id for h in coordinator.list_new_pending()] == [first]


async def test_waiting_requests_group_the_unanswered_questions_by_run():
    coordinator = SubAgentHITLCoordinator()
    a1 = await coordinator.push_requirement(_requirement("c1"), run_id="run-1", agent_path=["x"])
    a2 = await coordinator.push_requirement(_requirement("c2"), run_id="run-1")
    b1 = await coordinator.push_requirement(_requirement("c3"), run_id="run-2")
    assert coordinator.resolve(b1, "confirm")

    (paused,) = coordinator.waiting_requests()
    assert paused.run_id == "run-1"
    assert [r.requirement_id for r in paused.requirements] == [a1, a2]
    assert paused.requirements[0].agent_path == ["x"]
    assert paused.requirements[0].tool_name == "run_shell_command"
    assert coordinator.request_for(a1) == paused.requirements[0]
    assert coordinator.request_for("nope") is None


# ── The explanation ───────────────────────────────────────────────


async def test_an_explanation_before_surfacing_rides_the_request():
    coordinator = SubAgentHITLCoordinator()
    rid = await coordinator.push_requirement(_requirement(), run_id="run-1")
    coordinator.update_details(rid, "Deploys the build.")
    (handle,) = coordinator.list_new_pending()
    assert handle.entry.request(rid).details == "Deploys the build."
    assert coordinator.take_detail_updates() == []


async def test_an_explanation_after_surfacing_is_queued_once_as_an_update():
    coordinator = SubAgentHITLCoordinator()
    rid = await coordinator.push_requirement(_requirement(), run_id="run-1")
    coordinator.list_new_pending()
    coordinator.new_arrival.clear()
    coordinator.update_details(rid, "Deploys the build.")
    assert coordinator.new_arrival.is_set()
    assert coordinator.take_detail_updates() == [
        DetailUpdate(requirement_id=rid, details="Deploys the build.")
    ]
    assert coordinator.take_detail_updates() == []
    assert coordinator.request_for(rid).details == "Deploys the build."


async def test_an_explanation_for_an_answered_or_unknown_question_is_ignored():
    coordinator = SubAgentHITLCoordinator()
    rid = await coordinator.push_requirement(_requirement(), run_id="run-1")
    coordinator.list_new_pending()
    coordinator.resolve(rid, "confirm")
    coordinator.update_details(rid, "too late")
    coordinator.update_details("missing", "nobody")
    assert coordinator.take_detail_updates() == []
    assert coordinator.request_for(rid).details == ""


# ── Answering ─────────────────────────────────────────────────────


async def test_confirming_confirms_the_requirement_and_closes_the_question():
    coordinator = SubAgentHITLCoordinator()
    rid = await coordinator.push_requirement(_requirement(), run_id="run-1")
    assert coordinator.resolve(rid, "confirm") is True
    requirement = await coordinator.wait_resolved(rid)
    assert requirement.confirmation is True
    assert coordinator.has_unresolved() is False
    assert coordinator.waiting_requests() == []


async def test_rejecting_tells_the_model_which_tool_was_declined():
    coordinator = SubAgentHITLCoordinator()
    rid = await coordinator.push_requirement(_requirement(tool="edit_file"), run_id="run-1")
    assert coordinator.resolve(rid, "reject") is True
    requirement = await coordinator.wait_resolved(rid)
    assert requirement.confirmation is False
    assert requirement.confirmation_note == "Blocked: the person declined 'edit_file'."


@pytest.mark.parametrize("action", ["maybe", "", "CONFIRM "])
async def test_a_string_that_is_not_a_confirm_rejects(action):
    coordinator = SubAgentHITLCoordinator()
    rid = await coordinator.push_requirement(_requirement(), run_id="run-1")
    assert coordinator.resolve(rid, action) is True
    assert (await coordinator.wait_resolved(rid)).confirmation is False


def test_resolving_an_unknown_question_is_not_handled_here():
    assert SubAgentHITLCoordinator().resolve("missing", "confirm") is False


async def test_waiting_on_an_unknown_question_is_a_key_error():
    with pytest.raises(KeyError):
        await SubAgentHITLCoordinator().wait_resolved("missing")


async def test_a_waiter_has_no_deadline_and_is_counted_until_answered_or_cancelled():
    coordinator = SubAgentHITLCoordinator()
    answered = await coordinator.push_requirement(_requirement("c1"), run_id="run-1")
    cancelled = await coordinator.push_requirement(_requirement("c2"), run_id="run-2")
    waiter = asyncio.create_task(coordinator.wait_resolved(answered))
    doomed = asyncio.create_task(coordinator.wait_resolved(cancelled))
    await asyncio.sleep(0.01)
    assert not waiter.done()
    assert coordinator.waiting_count == 2

    doomed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await doomed
    assert coordinator.waiting_count == 1, "a cancelled wait is not counted as waiting"

    coordinator.resolve(answered, "confirm")
    assert (await waiter).confirmation is True
    assert coordinator.waiting_count == 0


async def test_cleanup_drops_the_question_but_a_restart_still_finds_the_run_interrupted(
    database_url: DatabaseUrl, under_a_spawn
):
    db = Database(database_url, scope=Scopes.fresh())
    before = _durable(db)
    rid = await before.push_requirement(
        _requirement(), run_id="sub-run", agent_path=["architect"], session_id="agent-session"
    )
    before.resolve(rid, "confirm")
    before.cleanup(rid)
    assert before.request_for(rid) is None
    with pytest.raises(KeyError):
        await before.wait_resolved(rid)

    after = _durable(db)
    assert after.restore(SESSION) == 0
    (link,) = after.take_interrupted()
    assert (link.run_id, link.agent_session_id, link.agent_name) == (
        "sub-run",
        "agent-session",
        "architect",
    )
    assert after.take_interrupted() == [], "each is returned once"


async def test_an_answered_question_is_not_asked_again_after_a_restart(
    database_url: DatabaseUrl, under_a_spawn
):
    db = Database(database_url, scope=Scopes.fresh())
    before = _durable(db)
    answered = await before.push_requirement(
        _requirement("c1"), run_id="sub-run", agent_path=["architect"], session_id="agent-session"
    )
    open_one = await before.push_requirement(
        _requirement("c2"), run_id="sub-run", agent_path=["architect"], session_id="agent-session"
    )
    before.resolve(answered, "confirm")  # answered, but the spawn has not cleaned up yet

    after = _durable(db)
    assert after.restore(SESSION) == 1
    (paused,) = after.waiting_requests()
    assert [r.requirement_id for r in paused.requirements] == [open_one]


async def test_a_run_that_is_over_leaves_no_link_to_continue(
    database_url: DatabaseUrl, under_a_spawn
):
    db = Database(database_url, scope=Scopes.fresh())
    before = _durable(db)
    rid = await before.push_requirement(
        _requirement(), run_id="sub-run", agent_path=["architect"], session_id="agent-session"
    )
    before.resolve(rid, "confirm")
    before.forget_spawn("sub-run")
    after = _durable(db)
    after.restore(SESSION)
    assert after.take_interrupted() == []


# ── Restart ───────────────────────────────────────────────────────


async def test_a_restart_brings_back_what_waited_marked_shown_with_its_explanation(
    database_url: DatabaseUrl, under_a_spawn
):
    db = Database(database_url, scope=Scopes.fresh())
    before = _durable(db)
    rid = await before.push_requirement(
        _requirement(), run_id="sub-run", agent_path=["architect"], session_id="agent-session"
    )
    before.update_details(rid, "Deploys the build.")

    after = _durable(db)
    assert after.restore(SESSION) == 1
    assert after.list_new_pending() == [], "restored questions are shown by a window attaching"
    (paused,) = after.waiting_requests()
    (request,) = paused.requirements
    assert (request.requirement_id, request.agent_path) == (rid, ["architect"])
    assert request.details == "Deploys the build."
    assert after.has_unresolved()


async def test_a_restart_with_nothing_waiting_restores_nothing(database_url: DatabaseUrl):
    assert _durable(Database(database_url, scope=Scopes.fresh())).restore(SESSION) == 0
    assert SubAgentHITLCoordinator().restore(SESSION) == 0, "without a journal there is nothing"


async def test_a_restored_run_is_ready_once_all_its_questions_are_answered(
    database_url: DatabaseUrl, under_a_spawn
):
    db = Database(database_url, scope=Scopes.fresh())
    before = _durable(db)
    first = await before.push_requirement(
        _requirement("c1"), run_id="sub-run", agent_path=["architect"], session_id="agent-session"
    )
    second = await before.push_requirement(
        _requirement("c2"), run_id="sub-run", agent_path=["architect"], session_id="agent-session"
    )

    after = _durable(db)
    assert after.restore(SESSION) == 2
    assert after.take_ready_restored() == []
    after.resolve(first, "confirm")
    assert after.take_ready_restored() == [], "one answer of two is not enough"
    after.resolve(second, "reject")

    (ready,) = after.take_ready_restored()
    assert ready.run_id == "sub-run"
    assert sorted((r.tool_execution.tool_call_id, r.confirmation) for r in ready.requirements) == [
        ("c1", True),
        ("c2", False),
    ]
    assert ready.link is not None
    assert ready.link.agent_session_id == "agent-session"
    assert after.take_ready_restored() == [], "a run is ready once"
    assert after.has_unresolved() is False


# ── Turns ─────────────────────────────────────────────────────────


async def test_the_finished_calls_of_a_turn_a_spawn_waits_in_are_kept_until_it_is_over(
    database_url: DatabaseUrl, under_a_spawn
):
    coordinator = _durable(Database(database_url, scope=Scopes.fresh()))
    coordinator.tool_finished("main-run", "c1", "first result")
    await coordinator.push_requirement(
        _requirement(), run_id="sub-run", agent_path=["architect"], session_id="agent-session"
    )
    coordinator.tool_finished("main-run", "c2", "second result")
    coordinator.tool_finished("other-run", "c9", "not kept: nothing of it waits")
    assert coordinator.turn_results("main-run") == {"c1": "first result", "c2": "second result"}
    assert coordinator.turn_results("other-run") == {}

    coordinator.forget_turn("main-run")
    assert coordinator.turn_results("main-run") == {}


# ── Relays ────────────────────────────────────────────────────────


def test_a_relay_counts_until_every_forwarder_has_closed():
    coordinator = SubAgentHITLCoordinator()
    assert coordinator.has_relay is False
    coordinator.open_relay()
    coordinator.open_relay()
    coordinator.close_relay()
    assert coordinator.has_relay is True
    coordinator.close_relay()
    assert coordinator.has_relay is False
    coordinator.close_relay()  # an extra close does not go below none
    coordinator.open_relay()
    assert coordinator.has_relay is True
