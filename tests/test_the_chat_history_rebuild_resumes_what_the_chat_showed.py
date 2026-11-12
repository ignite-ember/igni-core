"""``ChatHistoryRebuilder``: a resumed chat shows what it showed before.

``test_plan_rehydrate`` and ``test_plan_decisions`` pin the plan card, and
``test_event_log`` pins the splicer's pairing. This file pins the rest of
the orchestrator: the session it cannot find, the runs it skips, the
character totals it threads from run to run, and the corners of the plan
states and visualization cards.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from igni_core.backend.schemas_history import (
    AssistantTurn,
    ChatTurn,
    PlanTurn,
    StatsTurn,
    ToolTurn,
    UserTurn,
    VisualizationTurn,
)
from igni_core.backend.server_history import ChatHistoryRebuilder
from igni_core.core.session.event_log_schema import (
    VisualizationDeltaEntry,
    VisualizationDeltaPayload,
)


def _session(runs: Any, decisions: dict[str, str] | None = None, events=()):  # type: ignore[no-untyped-def]
    agno_session = None if runs is None else SimpleNamespace(runs=runs)
    team = SimpleNamespace(aget_session=AsyncMock(return_value=agno_session))
    return SimpleNamespace(
        main_team=team,
        user_id="u",
        plan_store=SimpleNamespace(decisions=decisions or {}),
        event_log=list(events),
    )


def _run(run_id: str, *messages: SimpleNamespace, **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(run_id=run_id, messages=list(messages), **extra)


def _msg(role: str, content: str = "", **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content, **extra)


def _plan_run(run_id: str) -> SimpleNamespace:
    call = {
        "id": f"call-{run_id}",
        "function": {"name": "exit_plan_mode", "arguments": f'{{"plan": "P-{run_id}"}}'},
    }
    return _run(
        run_id,
        _msg("assistant", "", tool_calls=[call]),
        _msg("tool", "ok", tool_name="exit_plan_mode", tool_call_id=f"call-{run_id}"),
    )


async def _rebuild(session: Any) -> list[ChatTurn]:
    return await ChatHistoryRebuilder(session).rebuild("sess-1")


class TestWhichRunsBecomeTurns:
    async def test_an_unknown_session_is_an_empty_chat(self) -> None:
        session = _session(None)

        assert await _rebuild(session) == []
        session.main_team.aget_session.assert_awaited_once_with(session_id="sess-1", user_id="u")

    async def test_a_session_with_no_runs_is_an_empty_chat(self) -> None:
        assert await _rebuild(_session([])) == []

    async def test_a_session_whose_runs_are_none_is_an_empty_chat(self) -> None:
        session = _session(None)
        session.main_team.aget_session = AsyncMock(return_value=SimpleNamespace(runs=None))

        assert await _rebuild(session) == []

    async def test_runs_come_out_in_the_order_they_were_stored(self) -> None:
        runs = [
            _run("r1", _msg("user", "one"), _msg("assistant", "uno")),
            _run("r2", _msg("user", "two"), _msg("assistant", "dos")),
        ]

        turns = await _rebuild(_session(runs))

        texts = [t.content for t in turns if isinstance(t, (UserTurn, AssistantTurn))]
        assert texts == ["one", "uno", "two", "dos"]
        assert [t.run_id for t in turns] == ["r1", "r1", "r1", "r2", "r2", "r2"]

    async def test_a_sub_agent_run_is_not_a_turn_of_its_own(self) -> None:
        runs = [
            _run("r1", _msg("user", "go")),
            _run("r1.child", _msg("assistant", "child talk"), parent_run_id="r1"),
        ]

        turns = await _rebuild(_session(runs))

        assert all(t.run_id == "r1" for t in turns)
        assert "child talk" not in [getattr(t, "content", "") for t in turns]

    async def test_a_malformed_run_is_skipped_and_the_rest_survive(self) -> None:
        broken = _run("bad", _msg("user", "lost"), metrics=SimpleNamespace(reasoning_tokens="x"))
        good = _run("good", _msg("user", "kept"))

        turns = await _rebuild(_session([broken, good]))

        assert [t.run_id for t in turns] == ["good", "good"]
        assert turns[0].content == "kept"  # type: ignore[union-attr]


class TestTheTokenBadgeAcrossRuns:
    async def test_each_run_s_estimate_counts_the_prompt_it_was_given(self) -> None:
        runs = [
            _run(
                "r1",
                _msg("system", "s" * 400),
                _msg("user", "u" * 40),
                _msg("assistant", "a" * 80),
            ),
            _run(
                "r2",
                _msg("system", "z" * 8),
                _msg("user", "v" * 40),
                _msg("assistant", "b" * 8),
            ),
        ]

        turns = await _rebuild(_session(runs))

        first, second = [t for t in turns if isinstance(t, StatsTurn)]
        assert (first.run_id, first.input_tokens, first.output_tokens) == ("r1", 110, 20)
        # system stays 400 (measured once); history is run one's 40 + 80
        # characters; plus this run's 40: (400 + 120 + 40) // 4
        assert (second.run_id, second.input_tokens, second.output_tokens) == ("r2", 140, 2)

    async def test_a_stats_turn_follows_its_run_and_only_when_there_is_something_to_count(
        self,
    ) -> None:
        turns = await _rebuild(_session([_run("empty"), _run("r2", _msg("user", "hello there"))]))

        assert [type(t) for t in turns] == [UserTurn, StatsTurn]
        assert turns[1].run_id == "r2"

    async def test_the_run_s_metrics_reach_its_badge(self) -> None:
        run = _run(
            "r1",
            _msg("user", "hello there"),
            metrics=SimpleNamespace(reasoning_tokens=7, duration=2.5),
        )

        stats = (await _rebuild(_session([run])))[-1]

        assert (stats.reasoning_tokens, stats.duration) == (7, 2.5)  # type: ignore[union-attr]


class TestPlanStates:
    async def _states(self, runs: list[Any], decisions: dict[str, str]) -> list[str]:
        turns = await _rebuild(_session(runs, decisions))
        return [t.state for t in turns if isinstance(t, PlanTurn)]

    async def test_a_chat_with_no_plan_is_left_alone(self) -> None:
        assert await self._states([_run("r1", _msg("user", "hi"))], {"r1": "approved"}) == []

    async def test_one_undecided_plan_is_pending(self) -> None:
        assert await self._states([_plan_run("a")], {}) == ["pending"]

    async def test_earlier_undecided_plans_are_dismissed_and_the_latest_pending(self) -> None:
        runs = [_plan_run("a"), _plan_run("b"), _plan_run("c")]

        assert await self._states(runs, {}) == ["dismissed", "dismissed", "pending"]

    async def test_a_recorded_decision_wins_wherever_the_plan_is(self) -> None:
        runs = [_plan_run("a"), _plan_run("b")]

        states = await self._states(runs, {"a": "approved", "b": "dismissed"})

        assert states == ["approved", "dismissed"]

    async def test_a_decision_on_an_early_plan_leaves_the_latest_pending(self) -> None:
        runs = [_plan_run("a"), _plan_run("b")]

        assert await self._states(runs, {"a": "approved"}) == ["approved", "pending"]

    async def test_a_word_other_than_approved_or_dismissed_is_not_a_decision(self) -> None:
        runs = [_plan_run("a"), _plan_run("b")]

        assert await self._states(runs, {"a": "maybe", "b": "pending"}) == [
            "dismissed",
            "pending",
        ]

    async def test_a_plan_with_no_run_id_takes_no_decision(self) -> None:
        run = _plan_run("a")
        run.run_id = ""

        assert await self._states([run], {"": "approved"}) == ["pending"]


def _card(seq: int, run_id: str, spec_json: str, **payload: Any) -> VisualizationDeltaEntry:
    return VisualizationDeltaEntry(
        seq=seq,
        run_id=run_id,
        payload=VisualizationDeltaPayload(spec_id=f"s{seq}", spec_json=spec_json, **payload),
    )


def _spliced(history: list[ChatTurn], events: list[Any]) -> list[ChatTurn]:
    out = list(history)
    ChatHistoryRebuilder(_session([], events=events))._splice_visualizations(out)  # type: ignore[arg-type]
    return out


def _spawn(run_id: str) -> ToolTurn:
    return ToolTurn(run_id=run_id, tool_name="spawn_agent")


class TestVisualizationCards:
    def test_no_events_leave_the_history_as_it_was(self) -> None:
        history: list[ChatTurn] = [UserTurn(run_id="r1", content="hi")]

        assert _spliced(history, []) == history

    def test_events_of_another_type_are_not_cards(self) -> None:
        history: list[ChatTurn] = [UserTurn(run_id="r1", content="hi")]
        other = SimpleNamespace(type="tool_started", run_id="r1", seq=1, payload=None)

        assert _spliced(history, [other]) == history

    def test_a_card_whose_payload_is_not_a_visualization_is_dropped(self) -> None:
        history: list[ChatTurn] = [UserTurn(run_id="r1", content="hi")]
        odd = SimpleNamespace(
            type="visualization_delta", run_id="r1", seq=1, payload=SimpleNamespace(spec_json="{}")
        )

        assert _spliced(history, [odd]) == history

    def test_an_empty_spec_is_dropped(self) -> None:
        history: list[ChatTurn] = [UserTurn(run_id="r1", content="hi")]

        assert _spliced(history, [_card(1, "r1", "")]) == history

    def test_a_spec_that_is_not_an_object_is_dropped(self) -> None:
        history: list[ChatTurn] = [UserTurn(run_id="r1", content="hi")]

        assert _spliced(history, [_card(1, "r1", "[1, 2]"), _card(2, "r1", "{nope")]) == history

    def test_a_card_is_named_for_its_source_and_carries_its_title(self) -> None:
        history: list[ChatTurn] = [UserTurn(run_id="r1", content="hi")]
        events = [
            _card(1, "r1", '{"a": 1}', source="acme.charts", title="Sales"),
            _card(2, "r1", '{"a": 2}'),
        ]

        _, first, second = _spliced(history, events)

        assert isinstance(first, VisualizationTurn)
        assert (first.source_agent, first.extension_id, first.title) == (
            "acme.charts",
            "acme.charts",
            "Sales",
        )
        assert (second.source_agent, second.extension_id) == ("visualizer", "")  # type: ignore[attr-defined]
        assert (first.run_id, first.seq, first.spec_id) == ("r1", 1, "s1")

    def test_cards_for_the_same_place_keep_the_order_they_were_logged_in(self) -> None:
        history: list[ChatTurn] = [UserTurn(run_id="r1", content="hi"), _spawn("r1")]
        events = [_card(5, "r1", '{"n": 5}'), _card(3, "r1", '{"n": 3}')]

        result = _spliced(history, events)

        # Nth card pairs with Nth spawn; the second has none and follows
        # the run's last turn. Seq order decides who is first.
        assert [getattr(t, "spec", None) for t in result[2:]] == [{"n": 3}, {"n": 5}]

    def test_more_cards_than_spawns_follow_the_runs_last_turn(self) -> None:
        history: list[ChatTurn] = [
            UserTurn(run_id="r1", content="hi"),
            _spawn("r1"),
            AssistantTurn(run_id="r1", content="done"),
        ]
        events = [_card(1, "r1", '{"n": 1}'), _card(2, "r1", '{"n": 2}')]

        result = _spliced(history, events)

        assert [t.role for t in result] == [
            "user",
            "tool",
            "visualization",
            "assistant",
            "visualization",
        ]

    def test_two_runs_with_a_spawn_each_get_their_own_card_without_shifting(self) -> None:
        history: list[ChatTurn] = [
            _spawn("r1"),
            AssistantTurn(run_id="r1", content="x"),
            _spawn("r2"),
            AssistantTurn(run_id="r2", content="y"),
        ]
        events = [_card(2, "r2", '{"n": 2}'), _card(1, "r1", '{"n": 1}')]

        result = _spliced(history, events)

        assert [(t.role, t.run_id) for t in result] == [
            ("tool", "r1"),
            ("visualization", "r1"),
            ("assistant", "r1"),
            ("tool", "r2"),
            ("visualization", "r2"),
            ("assistant", "r2"),
        ]

    def test_cards_logged_out_of_order_still_pair_with_the_spawns_in_seq_order(self) -> None:
        history: list[ChatTurn] = [_spawn("r1"), _spawn("r1")]
        events = [_card(2, "r1", '{"n": 2}'), _card(1, "r1", '{"n": 1}')]

        result = _spliced(history, events)

        assert [getattr(t, "spec", None) for t in result] == [None, {"n": 1}, None, {"n": 2}]

    def test_cards_for_runs_not_in_the_history_go_to_the_tail_in_seq_order(self) -> None:
        history: list[ChatTurn] = [UserTurn(run_id="r1", content="hi")]
        events = [_card(9, "gone-a", '{"n": 9}'), _card(4, "gone-b", '{"n": 4}')]

        result = _spliced(history, events)

        assert [getattr(t, "spec", None) for t in result[1:]] == [{"n": 4}, {"n": 9}]

    def test_only_a_spawn_agent_or_spawn_team_tool_anchors_a_card(self) -> None:
        history: list[ChatTurn] = [
            ToolTurn(run_id="r1", tool_name="grep"),
            ToolTurn(run_id="r1", tool_name="spawn_team"),
            AssistantTurn(run_id="r1", content="end"),
        ]

        result = _spliced(history, [_card(1, "r1", '{"n": 1}')])

        assert [t.role for t in result] == ["tool", "tool", "visualization", "assistant"]

    async def test_rebuild_splices_the_cards_it_finds_in_the_event_log(self) -> None:
        runs = [_run("r1", _msg("user", "chart it"), _msg("assistant", "here"))]
        session = _session(runs, events=[_card(1, "r1", '{"n": 1}')])

        turns = await _rebuild(session)

        assert [t.role for t in turns] == ["user", "assistant", "stats", "visualization"]
