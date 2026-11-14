"""``ParentCall``, ``SpawnCalls`` and ``SpawnDeadline``: finding the waiting
call in a run's messages, the chain of waiting calls, and a spawn's clock
stopping while a person decides."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from agno.models.message import Message

from igni_core.core.tools.spawn_context import ParentCall, SpawnCalls, SpawnDeadline


def _call(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        },
    }


def _assistant(*calls):
    return Message(role="assistant", tool_calls=list(calls))


def _run_context(messages, run_id="run-1", session_id="sess-1"):
    return SimpleNamespace(messages=messages, run_id=run_id, session_id=session_id)


def _parent(name="main", **over):
    fields = {
        "agent_name": name,
        "run_id": "r",
        "session_id": "s",
        "tool_call_id": "c1",
        "tool_name": "spawn_agent",
    }
    fields.update(over)
    return ParentCall(**fields)


# ── from_run_context ────────────────────────────────────────────────


def test_no_run_context_or_no_agent_is_no_call():
    messages = [_assistant(_call("c1", "spawn_agent", {"task": "t"}))]
    agent = SimpleNamespace(name="main", id="m")

    assert (
        ParentCall.from_run_context(None, agent=agent, tool_name="spawn_agent", tool_args={})
        is None
    )
    assert (
        ParentCall.from_run_context(
            _run_context(messages), agent=None, tool_name="spawn_agent", tool_args={}
        )
        is None
    )


def test_a_call_that_is_not_in_the_messages_is_no_call():
    for messages in ([], None, [_assistant(_call("c9", "other_tool", {}))]):
        assert (
            ParentCall.from_run_context(
                _run_context(messages),
                agent=SimpleNamespace(name="main", id="m"),
                tool_name="spawn_agent",
                tool_args={"task": "t"},
            )
            is None
        )


def test_the_call_record_carries_the_run_the_agent_and_the_serialised_messages():
    first = _assistant(_call("c1", "spawn_agent", {"task": "one"}))
    newest = _assistant(_call("c2", "spawn_agent", {"task": "two"}))
    messages = [Message(role="user", content="go"), first, newest]

    call = ParentCall.from_run_context(
        _run_context(messages, run_id="run-7", session_id="sess-7"),
        agent=SimpleNamespace(name="coder", id="agent-id"),
        tool_name="spawn_agent",
        tool_args={"task": "two"},
    )

    assert call is not None
    assert (call.agent_name, call.agent_id) == ("coder", "agent-id")
    assert (call.run_id, call.session_id) == ("run-7", "sess-7")
    assert (call.tool_call_id, call.tool_name, call.tool_args) == (
        "c2",
        "spawn_agent",
        {"task": "two"},
    )
    assert call.messages == [m.to_dict() for m in messages]
    assert all(isinstance(m, dict) for m in call.messages)


def test_an_agent_with_no_name_or_id_is_recorded_with_empty_ones():
    call = ParentCall.from_run_context(
        _run_context([_assistant(_call("c1", "spawn_agent", {}))]),
        agent=SimpleNamespace(name=None, id=None),
        tool_name="spawn_agent",
        tool_args={},
    )

    assert call is not None
    assert (call.agent_name, call.agent_id) == ("", "")


# ── find_call ───────────────────────────────────────────────────────


def test_the_newest_call_with_matching_arguments_wins():
    messages = [
        _assistant(_call("old", "spawn_agent", {"task": "same"})),
        _assistant(_call("new", "spawn_agent", {"task": "same"})),
    ]

    assert ParentCall.find_call(messages, "spawn_agent", {"task": "same"}) == "new"


def test_empty_and_none_arguments_do_not_have_to_match():
    messages = [_assistant(_call("c1", "spawn_agent", {"task": "t", "mode": "route"}))]

    found = ParentCall.find_call(
        messages, "spawn_agent", {"task": "t", "mode": "", "isolation": None}
    )

    assert found == "c1"


def test_a_given_argument_that_differs_is_no_match():
    messages = [_assistant(_call("c1", "spawn_agent", {"task": "t"}))]

    assert ParentCall.find_call(messages, "spawn_agent", {"task": "other"}) is None


def test_other_tools_and_other_roles_are_skipped():
    messages = [
        _assistant(_call("a", "spawn_agent", {"task": "t"})),
        Message(role="user", content="spawn_agent"),
        _assistant(_call("b", "run_shell_command", {"task": "t"})),
    ]

    assert ParentCall.find_call(messages, "spawn_agent", {"task": "t"}) == "a"
    assert ParentCall.find_call([Message(role="tool", content="x")], "spawn_agent", {}) is None


def test_arguments_that_are_a_dict_are_matched_like_a_json_string():
    call = {"id": "c1", "function": {"name": "spawn_agent", "arguments": {"task": "t"}}}

    assert ParentCall.find_call([_assistant(call)], "spawn_agent", {"task": "t"}) == "c1"


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", "null", "3", '"text"', 5])
def test_a_call_whose_arguments_are_not_an_object_is_skipped_not_a_crash(raw):
    messages = [
        _assistant(_call("good", "spawn_agent", {"task": "t"})),
        _assistant(_call("bad", "spawn_agent", raw)),
    ]

    assert ParentCall.find_call(messages, "spawn_agent", {"task": "t"}) == "good"


# ── spawn_args, is_team_spawn, turn ────────────────────────────────


def test_the_stored_arguments_read_as_spawn_arguments():
    call = _parent(tool_args={"task": "t", "agent_names": "a, b", "mode": None})

    assert call.spawn_args.team_names == ["a", "b"]
    assert call.spawn_args.mode == ""


@pytest.mark.parametrize(
    ("tool", "names", "expected"),
    [
        ("spawn_team", "a,b", True),
        ("spawn_team", "a,b,c", True),
        ("spawn_team", "a", False),
        ("spawn_team", "a,,", False),
        ("spawn_team", "", False),
        ("spawn_agent", "a,b", False),
    ],
)
def test_only_a_team_of_two_or_more_is_a_team_spawn(tool, names, expected):
    assert _parent(tool_name=tool, tool_args={"agent_names": names}).is_team_spawn is expected


def test_the_turn_is_every_call_of_the_assistant_message_in_order():
    turn = Message(
        role="assistant",
        tool_calls=[
            _call("c0", "read_file", {"path": "x"}),
            _call("c1", "spawn_agent", {"task": "t"}),
            _call("c2", "spawn_agent", {"task": "u"}),
        ],
    )
    call = _parent(messages=[Message(role="user", content="go").to_dict(), turn.to_dict()])

    assert [(c.id, c.name, c.arguments) for c in call.turn()] == [
        ("c0", "read_file", {"path": "x"}),
        ("c1", "spawn_agent", {"task": "t"}),
        ("c2", "spawn_agent", {"task": "u"}),
    ]


def test_the_newest_assistant_message_that_holds_the_call_is_the_turn():
    older = Message(role="assistant", tool_calls=[_call("c1", "spawn_agent", {})]).to_dict()
    newer = Message(
        role="assistant",
        tool_calls=[_call("c1", "spawn_agent", {}), _call("c5", "other", {})],
    ).to_dict()

    assert [c.id for c in _parent(messages=[older, newer]).turn()] == ["c1", "c5"]


def test_with_no_assistant_message_the_turn_is_just_this_call():
    not_assistant = {"role": "tool", "tool_calls": [{"id": "c1"}]}

    for messages in ([], [not_assistant]):
        turn = _parent(messages=messages).turn()
        assert [(c.id, c.name, c.arguments) for c in turn] == [("c1", "spawn_agent", {})]


# ── SpawnCalls ──────────────────────────────────────────────────────


def test_the_chain_is_empty_outside_any_spawn():
    assert SpawnCalls.current() == []


def test_entered_calls_stack_nearest_first_and_unwind():
    outer, inner = _parent("main"), _parent("coder")

    with SpawnCalls.entered(outer):
        with SpawnCalls.entered(inner):
            assert SpawnCalls.current() == [inner, outer]
        assert SpawnCalls.current() == [outer]
    assert SpawnCalls.current() == []


def test_entering_nothing_changes_nothing():
    outer = _parent()
    with SpawnCalls.entered(outer), SpawnCalls.entered(None):
        assert SpawnCalls.current() == [outer]


def test_an_exception_inside_entered_still_restores_the_chain():
    with pytest.raises(RuntimeError), SpawnCalls.entered(_parent()):
        raise RuntimeError("boom")

    assert SpawnCalls.current() == []


def test_restored_replaces_the_chain_with_the_stored_one_and_puts_the_old_back():
    live, stored = _parent("live"), [_parent("a"), _parent("b")]

    with SpawnCalls.entered(live):
        with SpawnCalls.restored(stored):
            assert SpawnCalls.current() == stored
        assert SpawnCalls.current() == [live]

    with pytest.raises(RuntimeError), SpawnCalls.restored(stored):
        raise RuntimeError("boom")
    assert SpawnCalls.current() == []


def test_the_path_runs_from_below_the_main_agent_down_to_the_asker():
    chain = [_parent("coder"), _parent("planner"), _parent("main")]

    assert SpawnCalls.path_to("tester", chain) == ["planner", "coder", "tester"]
    assert SpawnCalls.path_to("tester", [_parent("main")]) == ["tester"]
    assert SpawnCalls.path_to("tester", []) == ["tester"]


def test_the_path_defaults_to_the_current_chain():
    with SpawnCalls.entered(_parent("main")), SpawnCalls.entered(_parent("coder")):
        assert SpawnCalls.path_to("tester") == ["coder", "tester"]


async def test_concurrent_tasks_each_see_their_own_chain():
    seen = {}

    async def _task(name):
        with SpawnCalls.entered(_parent(name)):
            await asyncio.sleep(0.01)
            seen[name] = [p.agent_name for p in SpawnCalls.current()]

    await asyncio.gather(_task("one"), _task("two"))

    assert seen == {"one": ["one"], "two": ["two"]}


# ── SpawnDeadline ───────────────────────────────────────────────────


async def test_a_spawn_that_outlasts_its_time_is_cut_off():
    with pytest.raises(TimeoutError):
        async with SpawnDeadline.running(0.05):
            await asyncio.sleep(1)


async def test_a_spawn_inside_its_time_completes():
    async with SpawnDeadline.running(1):
        await asyncio.sleep(0.01)


async def test_a_wait_longer_than_the_budget_does_not_end_the_spawn():
    async with SpawnDeadline.running(0.1):
        with SpawnDeadline.waiting_on_person():
            await asyncio.sleep(0.4)
        await asyncio.sleep(0.01)


async def test_a_nested_spawns_wait_pauses_every_enclosing_clock():
    async with SpawnDeadline.running(0.1):
        async with SpawnDeadline.running(0.1):
            with SpawnDeadline.waiting_on_person():
                await asyncio.sleep(0.4)
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)


async def test_the_budget_left_after_a_wait_is_what_was_left_before_it():
    loop = asyncio.get_running_loop()
    began = loop.time()
    with pytest.raises(TimeoutError):
        async with SpawnDeadline.running(0.3):
            await asyncio.sleep(0.15)
            with SpawnDeadline.waiting_on_person():
                await asyncio.sleep(0.5)
            await asyncio.sleep(5)
    took = loop.time() - began

    # 0.15s used + 0.5s asking + ~0.15s left; never the full 0.3s again.
    assert 0.6 <= took < 0.95


def test_waiting_on_person_with_no_spawn_running_is_a_no_op():
    with SpawnDeadline.waiting_on_person():
        pass
