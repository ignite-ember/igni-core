"""What the stream bundles default to, how a finished run's answer is read
back, what the base handler guarantees around a stream, and the glyphs the
activity log is drawn with.

Every assertion is about a value a spawn, a stream or the parent agent's
recap sees: a default, a returned string, a registry that was cleaned.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from agno.run import agent as ev
from agno.run import team as team_ev
from pydantic import ValidationError

from igni_core.core.tools.orchestrate_agent_stream import SubAgentStreamHandler
from igni_core.core.tools.orchestrate_events import AgentCompletedEvent
from igni_core.core.tools.orchestrate_modes import IsolationMode, TeamMode
from igni_core.core.tools.orchestrate_registry import SubAgentRegistry
from igni_core.core.tools.orchestrate_spawn_types import (
    DEFAULT_TEAM_MAX_ITERATIONS,
    AgentBuildResult,
    AgentSpawnState,
    NestedToolkitSpec,
    OrchestrateScope,
    SpawnDeps,
    SpawnResult,
    SpawnWiring,
    SubagentStartPayload,
    SubagentStopPayload,
    SubTeamBuildResult,
    TeamBuildRequest,
    TeamSpawnState,
    TeamSpec,
)
from igni_core.core.tools.orchestrate_stream_context import (
    ResumeSpec,
    StreamContext,
    StreamSinks,
)
from igni_core.core.tools.orchestrate_stream_finalize import FinalizeResult, RunFinalizer
from igni_core.core.tools.orchestrate_stream_handler import (
    BaseStreamHandler,
    active_requirements,
    handler_name,
)
from igni_core.core.tools.orchestrate_stream_state import StreamState
from igni_core.core.tools.orchestrate_streaming import run_agent_streaming, run_team_streaming
from igni_core.core.tools.orchestrate_symbols import (
    TASK_STATUS_ICONS,
    VIS_SPEC_PREFIX,
    LogSymbols,
)

# ── Symbols ────────────────────────────────────────────────────────


class TestTheLogGlyphs:
    def test_each_glyph_is_the_character_the_recap_is_drawn_with(self):
        assert {s.name: s.value for s in LogSymbols} == {
            "PAUSE": "⏸",
            "COMPLETED": "✓",
            "FAILED": "✗",
            "RUNNING": "…",
            "WARNING": "⚠",
            "T_BRANCH": "├─",
            "T_TRUNK": "│",
            "T_ROOT": "┌─",
            "T_JOIN": "╞═",
            "T_LEAF": "└─",
        }

    def test_a_task_status_has_its_icon_and_an_unknown_one_has_none(self):
        assert TASK_STATUS_ICONS == {"completed": "✓", "failed": "✗", "running": "…"}
        assert "cancelled" not in TASK_STATUS_ICONS

    def test_the_spec_prefix_is_the_text_the_result_formatter_scans_for(self):
        assert VIS_SPEC_PREFIX == "VISUALIZE SPEC "


# ── State ──────────────────────────────────────────────────────────


class TestStreamStateLatching:
    def test_a_fresh_state_is_empty_and_its_log_is_its_own(self):
        a, b = StreamState(), StreamState()
        assert (a.card_id, a.current_tool, a.current_run_id, a.current_session_id) == (
            "",
            None,
            None,
            None,
        )
        assert (a.completed_content, a.agent_completed_emitted, a.emits_agent_completed) == (
            "",
            False,
            True,
        )
        a.log.append("x")
        assert b.log == []

    def test_the_first_ids_are_latched_and_only_a_new_run_id_says_so(self):
        s = StreamState()
        assert s.latch_ids(SimpleNamespace(run_id="r1", session_id="s1")) is True
        assert (s.current_run_id, s.current_session_id) == ("r1", "s1")

    def test_a_later_event_never_overwrites_and_reports_nothing_new(self):
        s = StreamState()
        s.latch_ids(SimpleNamespace(run_id="r1", session_id="s1"))
        assert s.latch_ids(SimpleNamespace(run_id="r2", session_id="s2")) is False
        assert (s.current_run_id, s.current_session_id) == ("r1", "s1")

    def test_the_session_id_latches_without_a_run_id_and_the_run_id_later(self):
        s = StreamState()
        assert s.latch_ids(SimpleNamespace(run_id=None, session_id="s1")) is False
        assert (s.current_run_id, s.current_session_id) == (None, "s1")
        assert s.latch_ids(SimpleNamespace(run_id="r1", session_id="s9")) is True
        assert (s.current_run_id, s.current_session_id) == ("r1", "s1")

    @pytest.mark.parametrize("empty", ["", None])
    def test_empty_ids_and_an_event_without_the_attributes_latch_nothing(self, empty):
        s = StreamState()
        assert s.latch_ids(SimpleNamespace(run_id=empty, session_id=empty)) is False
        assert s.latch_ids(object()) is False
        assert (s.current_run_id, s.current_session_id) == (None, None)


class TestRecordCompletion:
    def test_content_is_kept_and_the_completion_is_marked(self):
        s = StreamState()
        s.record_completion("done")
        assert (s.completed_content, s.agent_completed_emitted) == ("done", True)

    @pytest.mark.parametrize("empty", [None, ""])
    def test_empty_content_keeps_what_was_kept_before(self, empty):
        s = StreamState()
        s.record_completion("first")
        s.record_completion(empty)
        assert s.completed_content == "first"

    def test_content_that_is_not_text_is_turned_into_text(self):
        s = StreamState()
        s.record_completion(42)  # type: ignore[arg-type]
        assert s.completed_content == "42"

    def test_a_stream_that_does_not_emit_completions_never_marks_one(self):
        s = StreamState(emits_agent_completed=False)
        s.record_completion("member answer")
        assert s.agent_completed_emitted is False
        assert s.completed_content == "member answer"

    def test_metrics_change_nothing(self):
        a, b = StreamState(), StreamState()
        a.record_completion("x")
        b.record_completion("x", metrics=SimpleNamespace(input_tokens=9))
        assert a == b


# ── Finalizer ──────────────────────────────────────────────────────


class _Store:
    def __init__(self, by_id=None, last=None, raises: BaseException | None = None) -> None:
        self.by_id, self.last, self.raises = by_id, last, raises
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def aget_run_output(self, *, run_id: str, session_id: str) -> Any:
        self.calls.append(("by_id", {"run_id": run_id, "session_id": session_id}))
        if self.raises:
            raise self.raises
        return self.by_id

    async def aget_last_run_output(self, *, session_id: str) -> Any:
        self.calls.append(("last", {"session_id": session_id}))
        return self.last


def _finalizer(store: Any, *, run_id=None, session_id=None, completed="") -> RunFinalizer:
    state = StreamState(
        current_run_id=run_id, current_session_id=session_id, completed_content=completed
    )
    return RunFinalizer(store, state, ["a"])


class TestTheFinalRead:
    @pytest.mark.asyncio
    async def test_the_store_answer_is_read_by_run_and_session(self):
        store = _Store(by_id=SimpleNamespace(content="stored", status="COMPLETED"))
        result = await _finalizer(store, run_id="r", session_id="s").fetch()
        assert result == FinalizeResult(content="stored", status="COMPLETED", found=True)
        assert store.calls == [("by_id", {"run_id": "r", "session_id": "s"})]

    @pytest.mark.asyncio
    async def test_with_no_ids_the_store_is_not_asked_and_the_result_is_empty(self):
        store = _Store(by_id=SimpleNamespace(content="x"))
        result = await _finalizer(store).fetch()
        assert result == FinalizeResult()
        assert result.found is False and result.content is None and result.error is None
        assert store.calls == []

    @pytest.mark.asyncio
    async def test_with_only_a_session_the_last_run_is_read(self):
        store = _Store(last=SimpleNamespace(content="last one", status=None))
        result = await _finalizer(store, session_id="s").fetch()
        assert store.calls == [("last", {"session_id": "s"})]
        assert (result.content, result.status, result.found) == ("last one", None, True)

    @pytest.mark.asyncio
    async def test_a_run_the_store_does_not_know_falls_back_to_the_last_run(self):
        store = _Store(by_id=None, last=SimpleNamespace(content="fallback"))
        result = await _finalizer(store, run_id="r", session_id="s").fetch()
        assert [c[0] for c in store.calls] == ["by_id", "last"]
        assert result.content == "fallback"

    @pytest.mark.asyncio
    async def test_a_status_that_is_an_enum_becomes_its_text_and_content_is_text(self):
        from agno.run.base import RunStatus

        store = _Store(by_id=SimpleNamespace(content=123, status=RunStatus.completed))
        result = await _finalizer(store, run_id="r", session_id="s").fetch()
        assert result.content == "123"
        assert result.status == str(RunStatus.completed)

    @pytest.mark.asyncio
    async def test_a_run_with_empty_content_is_found_but_has_no_content(self):
        store = _Store(by_id=SimpleNamespace(content="", status="COMPLETED"))
        result = await _finalizer(store, run_id="r", session_id="s").fetch()
        assert (result.content, result.found) == (None, True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc", [AttributeError("no attr"), KeyError("k"), TimeoutError("t")])
    async def test_a_store_that_has_not_flushed_is_a_failed_result_not_a_raise(self, exc):
        result = await _finalizer(_Store(raises=exc), run_id="r", session_id="s").fetch()
        assert result.content is None and result.found is False
        assert result.error == str(exc)

    @pytest.mark.asyncio
    async def test_any_other_failure_is_not_hidden_behind_an_empty_answer(self):
        finalizer = _finalizer(_Store(raises=RuntimeError("boom")), run_id="r", session_id="s")
        with pytest.raises(RuntimeError, match="boom"):
            await finalizer.fetch()

    @pytest.mark.asyncio
    async def test_the_store_answer_wins_over_the_streamed_one(self):
        store = _Store(by_id=SimpleNamespace(content="stored"))
        final = await _finalizer(
            store, run_id="r", session_id="s", completed="streamed"
        ).final_content()
        assert final == "stored"

    @pytest.mark.asyncio
    async def test_the_streamed_answer_is_used_when_the_store_has_none(self):
        final = await _finalizer(
            _Store(by_id=None), run_id="r", session_id="s", completed="streamed"
        ).final_content()
        assert final == "streamed"

    @pytest.mark.asyncio
    async def test_the_streamed_answer_is_used_when_the_store_failed(self):
        final = await _finalizer(
            _Store(raises=KeyError("k")), run_id="r", session_id="s", completed="streamed"
        ).final_content()
        assert final == "streamed"

    @pytest.mark.asyncio
    async def test_with_neither_the_answer_is_an_empty_string(self):
        assert (
            await _finalizer(_Store(by_id=None), run_id="r", session_id="s").final_content() == ""
        )

    @pytest.mark.asyncio
    async def test_the_think_markers_are_removed_from_the_answer_and_it_is_trimmed(self):
        store = _Store(by_id=SimpleNamespace(content="  <think>plan</think> answer\n"))
        final = await _finalizer(store, run_id="r", session_id="s").final_content()
        assert final == "plan answer"


class TestCleanFinal:
    @pytest.mark.parametrize("empty", [None, ""])
    def test_nothing_is_an_empty_string(self, empty):
        assert RunFinalizer.clean_final(empty) == ""

    def test_markers_go_verbatim_and_the_text_between_stays(self):
        assert RunFinalizer.clean_final(" <think>a</think>b<think> ") == "ab"

    def test_whitespace_only_content_trims_to_nothing_and_unicode_survives(self):
        assert RunFinalizer.clean_final(" \n\t ") == ""
        assert RunFinalizer.clean_final(" héllo ✓ ") == "héllo ✓"


# ── Base handler ───────────────────────────────────────────────────


class _Coordinator:
    def __init__(self) -> None:
        self.forgotten: list[str] = []

    def forget_spawn(self, run_id: str) -> None:
        self.forgotten.append(run_id)


class _Tape:
    """A runnable whose stream is a scripted list; may blow up after it."""

    def __init__(self, tape, *, explode: BaseException | None = None, stored=None) -> None:
        self.tape, self.explode, self.stored = tape, explode, stored

    def arun(self, task: str, *, stream: bool = True):
        return self._play()

    async def _play(self):
        for event in self.tape:
            yield event
        if self.explode:
            raise self.explode

    async def aget_run_output(self, *, run_id: str, session_id: str):
        return self.stored

    async def aget_last_run_output(self, *, session_id: str):
        return self.stored


class _Probe(BaseStreamHandler[StreamState]):
    """Handles ``run_id``-carrying events; ``follow`` on an event asks to continue."""

    def __init__(self, context: StreamContext) -> None:
        super().__init__(context, StreamState())
        self.seen: list[Any] = []

    async def _handle(self, event: Any) -> Any:
        self.seen.append(event)
        self._latch_run_ids(event)
        return getattr(event, "follow", None)


async def _aiter(items):
    for i in items:
        yield i


def _ctx(runnable, *, path=None, card_id="", on_progress=None, coordinator=None, registry=None):
    return StreamContext(
        runnable=runnable,
        task="t",
        agent_path=path or [],
        card_id=card_id,
        sinks=StreamSinks(
            on_progress=on_progress,
            hitl_coordinator=coordinator,
            subagent_registry=registry if registry is not None else SubAgentRegistry(),
        ),
    )


class TestTheBaseHandlerLoop:
    @pytest.mark.asyncio
    async def test_a_stream_without_handlers_raises_not_implemented(self):
        handler = BaseStreamHandler(_ctx(_Tape([SimpleNamespace()])), StreamState())
        with pytest.raises(NotImplementedError):
            await handler.run()

    @pytest.mark.asyncio
    async def test_the_final_text_is_shaped_by_the_subclass_and_unshaped_by_default(self):
        runnable = _Tape([], stored=SimpleNamespace(content="answer"))
        handler = _Probe(_ctx(runnable))
        handler.state.current_run_id, handler.state.current_session_id = "r", "s"
        assert await handler.run() == ("answer", [])

    @pytest.mark.asyncio
    async def test_a_paused_stream_is_read_to_its_end_after_a_follow_up_is_chosen(self):
        resumed = SimpleNamespace(run_id="resumed")
        first = SimpleNamespace(run_id="r", follow=_aiter([resumed]))
        later = SimpleNamespace(run_id="later", follow=_aiter([SimpleNamespace(run_id="never")]))
        tail = SimpleNamespace(run_id="tail")
        handler = _Probe(_ctx(_Tape([first, later, tail])))
        await handler.run()
        # Later events of the paused stream are consumed but not handled, and
        # the follow-up chosen first is the one that is driven.
        assert [e.run_id for e in handler.seen] == ["r", "resumed"]

    @pytest.mark.asyncio
    async def test_a_paused_stream_is_fully_consumed_not_abandoned(self):
        consumed: list[str] = []

        class Runnable(_Tape):
            async def _play(self):
                yield SimpleNamespace(run_id="r", follow=_aiter([]))
                yield SimpleNamespace(run_id="r")
                consumed.append("end")

        await _Probe(_ctx(Runnable([]))).run()
        assert consumed == ["end"]

    @pytest.mark.asyncio
    async def test_the_run_is_registered_while_it_runs_and_forgotten_afterwards(self):
        registry, coordinator = SubAgentRegistry(), _Coordinator()
        during: list[bool] = []

        class Probe(_Probe):
            async def _handle(self, event):
                out = await super()._handle(event)
                during.append("r1" in registry)
                return out

        await Probe(
            _ctx(_Tape([SimpleNamespace(run_id="r1")]), registry=registry, coordinator=coordinator)
        ).run()
        assert during == [True]
        assert "r1" not in registry
        assert coordinator.forgotten == ["r1"]

    @pytest.mark.asyncio
    async def test_an_error_mid_stream_still_cleans_the_registry_and_the_coordinator(self):
        registry, coordinator = SubAgentRegistry(), _Coordinator()
        runnable = _Tape([SimpleNamespace(run_id="r1")], explode=RuntimeError("mid-stream"))
        with pytest.raises(RuntimeError, match="mid-stream"):
            await _Probe(_ctx(runnable, registry=registry, coordinator=coordinator)).run()
        assert "r1" not in registry
        assert coordinator.forgotten == ["r1"]

    @pytest.mark.asyncio
    async def test_a_run_that_never_latched_an_id_forgets_nothing(self):
        coordinator = _Coordinator()
        await _Probe(_ctx(_Tape([SimpleNamespace()]), coordinator=coordinator)).run()
        assert coordinator.forgotten == []

    @pytest.mark.asyncio
    async def test_a_run_without_a_coordinator_still_discards_its_id(self):
        registry = SubAgentRegistry()
        await _Probe(_ctx(_Tape([SimpleNamespace(run_id="r1")]), registry=registry)).run()
        assert len(registry) == 0


class TestTheCompletionTheCardNeeds:
    @pytest.mark.asyncio
    async def test_a_specialist_that_never_said_done_says_it_once_on_its_path(self):
        events: list[Any] = []
        handler = _Probe(_ctx(_Tape([]), path=["root", "dev"], on_progress=events.append))
        await handler.run()
        assert [(type(e), e.agent_path) for e in events] == [(AgentCompletedEvent, "root")]
        assert handler.state.agent_completed_emitted is True

    @pytest.mark.asyncio
    async def test_an_empty_path_is_root_and_the_first_element_otherwise(self):
        assert _Probe(_ctx(_Tape([])))._agent_completed_path() == "root"
        assert _Probe(_ctx(_Tape([]), path=["dev", "x"]))._agent_completed_path() == "dev"

    @pytest.mark.asyncio
    async def test_a_completion_already_recorded_is_not_repeated(self):
        events: list[Any] = []
        handler = _Probe(_ctx(_Tape([]), on_progress=events.append))
        handler.state.record_completion("x")
        await handler.run()
        assert events == []

    @pytest.mark.asyncio
    async def test_a_team_never_says_the_whole_team_is_done(self):
        events: list[Any] = []
        handler = _Probe(_ctx(_Tape([]), on_progress=events.append))
        handler.state.emits_agent_completed = False
        await handler.run()
        assert events == []
        assert handler.state.agent_completed_emitted is False


class TestEmit:
    def test_the_card_id_is_stamped_and_the_event_delivered(self):
        events: list[Any] = []
        handler = _Probe(_ctx(_Tape([]), card_id="card-1", on_progress=events.append))
        event = AgentCompletedEvent(agent_path="a")
        handler._emit(event)
        assert events == [event] and event.card_id == "card-1"

    def test_an_empty_card_id_leaves_the_event_untouched(self):
        events: list[Any] = []
        handler = _Probe(_ctx(_Tape([]), on_progress=events.append))
        event = AgentCompletedEvent(agent_path="a", card_id="own")
        handler._emit(event)
        assert event.card_id == "own"

    def test_without_a_callback_nothing_happens(self):
        event = AgentCompletedEvent(agent_path="a")
        _Probe(_ctx(_Tape([]), card_id="c"))._emit(event)
        assert event.card_id != "c"

    def test_a_raising_callback_is_logged_as_a_warning_and_swallowed(self, caplog):
        def boom(event: Any) -> None:
            raise ValueError("ui gone")

        handler = _Probe(_ctx(_Tape([]), on_progress=boom))
        with caplog.at_level("WARNING", logger="igni_core.llm_calls"):
            handler._emit(AgentCompletedEvent(agent_path="a"))
        assert "progress callback failed: ui gone" in caplog.text

    def test_a_log_line_goes_to_the_recap_log_only(self):
        events: list[Any] = []
        handler = _Probe(_ctx(_Tape([]), on_progress=events.append))
        handler._log_line("hello")
        assert handler.state.log == ["hello"] and events == []


class TestTheHelpers:
    def test_active_requirements_lists_what_the_event_waits_on(self):
        event = SimpleNamespace(active_requirements=("a", "b"))
        assert active_requirements(event) == ["a", "b"]

    def test_an_event_that_cannot_answer_waits_on_nothing(self):
        assert active_requirements(object()) == []

    def test_a_subclass_of_a_handled_class_is_answered_and_a_stranger_is_not(self):
        class Parent: ...

        class Child(Parent): ...

        class Other: ...

        table = {Parent: "_p"}
        assert handler_name(table, Child()) == "_p"
        assert handler_name(table, Other()) is None

    def test_the_nearest_class_in_the_mro_wins(self):
        class Parent: ...

        class Child(Parent): ...

        assert handler_name({Parent: "_p", Child: "_c"}, Child()) == "_c"


# ── Streamers ──────────────────────────────────────────────────────


class TestTheStreamers:
    @pytest.mark.asyncio
    async def test_a_team_run_answers_with_the_teams_stored_output_and_member_cards(self):
        events: list[Any] = []
        tape = [
            ev.RunStartedEvent(run_id="m1", session_id="s", agent_name="alice"),
            ev.RunCompletedEvent(run_id="m1", session_id="s", agent_name="alice", content="a"),
            team_ev.RunCompletedEvent(run_id="team-run", session_id="s", content="team"),
        ]
        registry = SubAgentRegistry()
        runnable = _Tape(tape, stored=SimpleNamespace(content="stored team answer"))
        context = _ctx(runnable, path=["crew"], on_progress=events.append, registry=registry)
        response, log = await run_team_streaming(context)
        assert response == "stored team answer"
        assert log == ["  ├─ [alice]"]
        done = [e for e in events if isinstance(e, AgentCompletedEvent)]
        assert [e.agent_path for e in done] == ["crew.alice"]
        assert len(registry) == 0

    @pytest.mark.asyncio
    async def test_a_team_run_that_raises_propagates_after_cleaning_up(self):
        registry = SubAgentRegistry()
        runnable = _Tape(
            [team_ev.RunStartedEvent(run_id="t1", session_id="s")], explode=RuntimeError("x")
        )
        with pytest.raises(RuntimeError, match="x"):
            await run_team_streaming(_ctx(runnable, registry=registry))
        assert len(registry) == 0

    @pytest.mark.asyncio
    async def test_a_specialist_run_returns_the_answer_and_its_activity_log(self):
        runnable = _Tape(
            [ev.RunContentEvent(run_id="r", session_id="s", content="hi")],
            stored=SimpleNamespace(content="final"),
        )
        response, log = await run_agent_streaming(_ctx(runnable, path=["dev"]))
        assert response == "final"
        assert isinstance(log, list)

    @pytest.mark.asyncio
    async def test_a_pause_with_no_coordinator_is_not_continued_by_the_streamer(self):
        class Runnable(_Tape):
            continued = False

            def acontinue_run(self, *a: Any, **k: Any):
                Runnable.continued = True
                return _aiter([])

        paused = ev.RunPausedEvent(run_id="r", session_id="s", requirements=[])
        await run_agent_streaming(_ctx(Runnable([paused])))
        assert Runnable.continued is False

    @pytest.mark.asyncio
    async def test_the_sub_agent_handler_is_a_base_handler(self):
        assert issubclass(SubAgentStreamHandler, BaseStreamHandler)


# ── Spawn types ────────────────────────────────────────────────────


class TestTheSpawnBundlesDefaults:
    def test_wiring_defaults_to_no_session_no_hooks_and_an_empty_registry(self):
        w = SpawnWiring()
        assert w.session_id == "" and w.hook_executor is None
        assert w.sinks.on_progress is None and w.sinks.hitl_coordinator is None
        assert w.sinks.event_appender is None and len(w.sinks.subagent_registry) == 0

    def test_each_wiring_gets_its_own_sinks_and_registry(self):
        a, b = SpawnWiring(), SpawnWiring()
        a.sinks.subagent_registry.register("r")
        assert len(b.sinks.subagent_registry) == 0
        assert a.sinks is not b.sinks

    def test_a_stream_context_without_sinks_gets_its_own_empty_registry(self):
        a = StreamContext(runnable=object())
        b = StreamContext(runnable=object())
        a.sinks.subagent_registry.register("r")
        assert len(b.sinks.subagent_registry) == 0
        assert (a.task, a.agent_path, a.card_id) == ("", [], "")
        a.agent_path.append("x")
        assert b.agent_path == []

    def test_a_resume_spec_defaults_to_no_requirements_and_a_stored_run(self):
        r = ResumeSpec(run_id="r", session_id="s")
        assert r.requirements == [] and r.run_response is None
        with pytest.raises(ValidationError):
            ResumeSpec(session_id="s")  # type: ignore[call-arg]

    def test_the_scope_nests_from_the_top_with_a_depth_limit_of_five(self):
        scope = OrchestrateScope(pool=object(), settings=object(), budget=object())
        assert (scope.current_depth, scope.max_depth, scope.project_dir) == (0, 5, None)

    def test_the_scope_keeps_the_objects_it_was_given_by_identity(self):
        pool, settings, budget = object(), object(), object()
        scope = OrchestrateScope(pool=pool, settings=settings, budget=budget)
        assert scope.pool is pool and scope.settings is settings and scope.budget is budget

    def test_a_nested_toolkit_spec_names_depth_scope_and_wiring(self):
        scope = OrchestrateScope(pool=object(), settings=object(), budget=object())
        wiring = SpawnWiring(session_id="s")
        spec = NestedToolkitSpec(depth=2, scope=scope, wiring=wiring)
        assert (spec.depth, spec.scope.max_depth, spec.wiring.session_id) == (2, 5, "s")

    def test_a_team_spec_defaults_to_no_telemetry_markdown_and_agnos_iteration_cap(self):
        spec = TeamSpec(name="crew", mode=TeamMode.COORDINATE)
        assert (spec.telemetry, spec.markdown, spec.max_iterations) == (False, True, 10)
        assert DEFAULT_TEAM_MAX_ITERATIONS == 10
        assert (spec.members, spec.model, spec.db) == ([], None, None)

    def test_a_team_build_request_defaults_to_no_names_no_mode_and_depth_one(self):
        req = TeamBuildRequest(pool=object(), settings=object())
        assert (req.names, req.mode, req.depth) == ([], "", 1)

    def test_the_spawn_states_start_empty_with_none_isolation_and_coordinate_mode(self):
        a = AgentSpawnState(agent_name="dev")
        assert (a.isolation_raw, a.isolation, a.agent, a.description) == (
            "",
            IsolationMode.NONE,
            None,
            "",
        )
        assert (a.tools, a.effective_task, a.sandbox) == ("none", "", None)
        t = TeamSpawnState()
        assert (t.names, t.mode_raw, t.mode, t.team) == ([], "", TeamMode.COORDINATE, None)
        assert (t.member_lines, t.team_label) == ([], "")
        t.names.append("x")
        assert TeamSpawnState().names == []

    def test_the_build_results_default_to_nothing_built_and_no_error(self):
        a = AgentBuildResult()
        assert (a.agent, a.defn, a.isolation, a.sandbox, a.task, a.error) == (
            None,
            None,
            IsolationMode.NONE,
            None,
            "",
            None,
        )
        s = SubTeamBuildResult()
        assert (s.team, s.mode, s.error) == (None, None, None)


class TestSpawnResultAndPayloads:
    def test_a_failure_carries_its_message_and_is_not_ok(self):
        r = SpawnResult.failure("no such agent")
        assert (r.ok, r.message) == (False, "no such agent")

    def test_a_failure_may_say_nothing(self):
        assert SpawnResult.failure("") == SpawnResult(ok=False, message="")

    def test_a_success_is_ok_with_no_message(self):
        r = SpawnResult.success()
        assert (r.ok, r.message) == (True, "")

    def test_a_bare_result_is_a_failure(self):
        assert SpawnResult().ok is False

    def test_the_start_payload_wire_shape_has_no_mode_for_a_single_agent(self):
        p = SubagentStartPayload(session_id="s", agent_name="dev", task="t")
        assert p.model_dump() == {"session_id": "s", "agent_name": "dev", "task": "t", "mode": None}
        assert SubagentStartPayload(
            session_id="s", agent_name="c", task="t", mode="route"
        ).mode == ("route")

    def test_the_stop_payload_wire_shape_has_neither_preview_nor_error_until_given(self):
        assert SubagentStopPayload(session_id="s", agent_name="dev").model_dump() == {
            "session_id": "s",
            "agent_name": "dev",
            "result_preview": None,
            "error": None,
        }
        assert SubagentStopPayload(session_id="s", agent_name="d", error="x").error == "x"

    def test_the_payloads_refuse_a_missing_identity(self):
        with pytest.raises(ValidationError):
            SubagentStartPayload(agent_name="dev", task="t")  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            SubagentStopPayload(session_id="s")  # type: ignore[call-arg]


class TestSpawnDeps:
    @staticmethod
    def _deps() -> SpawnDeps:
        async def streamer(context: StreamContext):
            return "", []

        return SpawnDeps(
            agent_streamer=streamer,
            team_streamer=streamer,
            team_factory=lambda spec: spec,
            model_registry_factory=lambda settings: settings,
            nested_toolkit_factory=lambda spec: spec,
        )

    def test_the_deps_are_frozen(self):
        deps = self._deps()
        with pytest.raises(ValidationError):
            deps.team_factory = lambda spec: None  # type: ignore[misc]

    def test_a_missing_factory_is_refused(self):
        with pytest.raises(ValidationError):
            SpawnDeps(agent_streamer=lambda c: None)  # type: ignore[call-arg]
