"""What the sub-agent stream handler logs, emits and does for each Agno event.

A scripted Agno event tape drives the real handler; the progress
callback, the event-log appender and the HITL bridge are the sinks it
reports through. Every assertion is about what those sinks received.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from agno.metrics import RunMetrics as Metrics
from agno.models.response import ToolExecution
from agno.run import agent as ev
from agno.run.agent import CustomEvent

from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.core.tools.orchestrate_agent_stream import (
    AGENT_EVENT_HANDLERS,
    SubAgentStreamHandler,
)
from igni_core.core.tools.orchestrate_contracts import AgnoRunnable, HitlCoordinatorProtocol
from igni_core.core.tools.orchestrate_events import (
    AgentCompletedEvent,
    AgentPausedEvent,
    ContentPreviewEvent,
    RunErrorEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
    VisualizationDeltaEvent,
)
from igni_core.core.tools.orchestrate_registry import SubAgentRegistry
from igni_core.core.tools.orchestrate_stream_context import StreamContext, StreamSinks
from igni_core.core.tools.orchestrate_symbols import VIS_SPEC_PREFIX, LogSymbols
from igni_core.core.tools.subagent_stream import SubAgentStreamState


class _Runnable:
    """Plays one scripted tape; records the continuations it is asked for."""

    def __init__(self, tape: list[Any], follow_up: list[Any] | None = None) -> None:
        self.tape = tape
        self.follow_up = follow_up or []
        self.continued: list[dict[str, Any]] = []
        self.arun_calls: list[str] = []

    def arun(self, task: str, *, stream: bool = True) -> Any:
        self.arun_calls.append(task)
        return self._play(self.tape)

    def acontinue_run(self, run_response: Any = None, **kwargs: Any) -> Any:
        self.continued.append({"run_response": run_response, **kwargs})
        return self._play(self.follow_up)

    @staticmethod
    async def _play(tape: list[Any]):
        for event in tape:
            yield event

    async def aget_run_output(self, *, run_id: str, session_id: str) -> Any:
        return None

    async def aget_last_run_output(self, *, session_id: str) -> Any:
        return None


class _Sinks:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self.appended: list[tuple[Any, str]] = []
        self.append_error: Exception | None = None

    def on_progress(self, event: Any) -> None:
        self.events.append(event)

    async def event_appender(self, delta: Any, run_id: str) -> None:
        if self.append_error is not None:
            raise self.append_error
        self.appended.append((delta, run_id))

    def of(self, kind: type) -> list[Any]:
        return [e for e in self.events if isinstance(e, kind)]


def _handler(
    tape: list[Any],
    *,
    path: list[str] | None = None,
    coordinator: Any = None,
    follow_up: list[Any] | None = None,
    sinks: _Sinks | None = None,
    with_appender: bool = True,
    card_id: str = "",
):
    sinks = sinks or _Sinks()
    runnable = _Runnable(tape, follow_up)
    context = StreamContext(
        runnable=runnable,
        task="do it",
        agent_path=path or [],
        card_id=card_id,
        sinks=StreamSinks(
            on_progress=sinks.on_progress,
            event_appender=sinks.event_appender if with_appender else None,
            hitl_coordinator=coordinator,
            subagent_registry=SubAgentRegistry(),
        ),
    )
    return SubAgentStreamHandler(context), sinks, runnable


def _tool(name: str | None = "t", args: Any = None, call_id: str | None = "c1") -> Any:
    return ToolExecution(
        tool_name=name, tool_args=args if args is not None else {}, tool_call_id=call_id
    )


class _Req:
    def __init__(self, name: str = "edit_file") -> None:
        self.tool_execution = SimpleNamespace(tool_name=name, tool_args={})
        self.confirmed = False

    def is_resolved(self) -> bool:
        return self.confirmed

    def confirm(self) -> None:
        self.confirmed = True

    def reject(self, note: str = "") -> None:
        self.confirmed = True


class TestThePathAndCard:
    def test_the_agent_path_is_the_dot_joined_context_path_or_root(self):
        nested, _, _ = _handler([], path=["architect", "editor"])
        top, _, _ = _handler([])
        assert nested.state.agent_path_id == "architect.editor"
        assert top.state.agent_path_id == "root"

    @pytest.mark.asyncio
    async def test_every_event_is_stamped_with_the_card_id_of_the_context(self):
        handler, sinks, _ = _handler(
            [ev.ToolCallStartedEvent(tool=_tool()), ev.RunErrorEvent(content="x")], card_id="card-7"
        )
        await handler.run()
        assert sinks.events and all(e.card_id == "card-7" for e in sinks.events)
        assert all(e.agent_path == "root" for e in sinks.events)


class TestTheDispatchTable:
    def test_every_row_names_a_method_the_handler_has(self):
        for name in AGENT_EVENT_HANDLERS.values():
            assert callable(getattr(SubAgentStreamHandler, name))

    @pytest.mark.asyncio
    async def test_a_subclass_of_a_handled_event_is_answered_by_its_parents_method(self):
        class _MyError(ev.RunErrorEvent):
            pass

        handler, sinks, _ = _handler([_MyError(content="derived")])
        await handler.run()
        assert [e.error for e in sinks.of(RunErrorEvent)] == ["derived"]

    @pytest.mark.asyncio
    async def test_an_event_nobody_answers_changes_nothing(self):
        handler, sinks, _ = _handler([ev.RunStartedEvent(run_id="r1", session_id="s1")])
        await handler.run()
        assert [type(e) for e in sinks.events] == [AgentCompletedEvent]
        assert handler.state.log == []
        assert handler.state.current_run_id == "r1"

    @pytest.mark.asyncio
    async def test_progress_callbacks_receive_typed_events_not_dicts(self):
        handler, sinks, _ = _handler([ev.ToolCallStartedEvent(tool=_tool("read", {"p": "a"}))])
        await handler.run()
        started = sinks.of(ToolStartedEvent)
        assert len(started) == 1 and isinstance(started[0], ToolStartedEvent)


class TestToolEvents:
    @pytest.mark.asyncio
    async def test_a_start_logs_the_full_args_and_emits_the_short_preview(self):
        long = "v" * 50
        handler, sinks, _ = _handler(
            [ev.ToolCallStartedEvent(tool=_tool("read", {"path": long, "b": 2, "c": 3}))]
        )
        await handler.run()
        (started,) = sinks.of(ToolStartedEvent)
        assert started.tool == "read"
        assert started.tool_call_id == "c1"
        assert started.args == "path=" + "v" * 27 + "..., b=2"
        assert handler.state.log[0].endswith(f"read(path={long}, b=2, c=3)")

    @pytest.mark.asyncio
    async def test_a_start_with_no_tool_is_called_tool_with_no_args(self):
        handler, sinks, _ = _handler([ev.ToolCallStartedEvent(tool=None)])
        await handler.run()
        (started,) = sinks.of(ToolStartedEvent)
        assert (started.tool, started.args, started.tool_call_id) == ("tool", "", None)
        assert handler.state.log[0].endswith("tool()")

    @pytest.mark.asyncio
    async def test_a_completion_logs_the_whole_result_and_emits_a_sixty_char_preview(self):
        result = "r" * 100
        handler, sinks, _ = _handler(
            [
                ev.ToolCallStartedEvent(tool=_tool("read")),
                ev.ToolCallCompletedEvent(
                    tool=ToolExecution(tool_name="read", result=result, tool_call_id="c1")
                ),
            ]
        )
        await handler.run()
        (done,) = sinks.of(ToolCompletedEvent)
        assert done.result == "r" * 60 + "..."
        assert done.is_error is False
        assert done.tool_call_id == "c1"
        assert handler.state.log[-1].endswith(result)
        assert handler.state.current_tool is None

    @pytest.mark.asyncio
    async def test_a_completion_with_no_result_logs_and_previews_nothing(self):
        handler, sinks, _ = _handler(
            [ev.ToolCallCompletedEvent(tool=ToolExecution(tool_name="t", result=None))]
        )
        await handler.run()
        (done,) = sinks.of(ToolCompletedEvent)
        assert done.result == ""
        assert handler.state.log[-1] == f"  │  │  {LogSymbols.T_LEAF.value} "

    @pytest.mark.asyncio
    async def test_a_completion_of_another_tool_leaves_the_running_one_set(self):
        handler, sinks, _ = _handler(
            [
                ev.ToolCallStartedEvent(tool=_tool("slow")),
                ev.ToolCallCompletedEvent(tool=ToolExecution(tool_name="other", result="x")),
            ]
        )
        await handler.run()
        assert handler.state.current_tool == "slow"
        assert sinks.of(ToolCompletedEvent)[0].tool == "other"

    @pytest.mark.asyncio
    async def test_a_completion_without_a_tool_names_the_running_one(self):
        handler, sinks, _ = _handler(
            [ev.ToolCallStartedEvent(tool=_tool("slow")), ev.ToolCallCompletedEvent(tool=None)]
        )
        await handler.run()
        assert sinks.of(ToolCompletedEvent)[0].tool == "slow"
        assert handler.state.current_tool is None

    @pytest.mark.asyncio
    async def test_a_tool_error_logs_the_error_and_emits_it_cut_at_200_flagged_as_error(self):
        err = "e" * 300
        handler, sinks, _ = _handler(
            [
                ev.ToolCallStartedEvent(tool=_tool("edit")),
                ev.ToolCallErrorEvent(
                    tool=ToolExecution(tool_name="edit", tool_call_id="c9"), error=err
                ),
            ]
        )
        await handler.run()
        (done,) = sinks.of(ToolCompletedEvent)
        assert done.is_error is True
        assert done.result == "e" * 200
        assert done.tool == "edit" and done.tool_call_id == "c9"
        assert handler.state.log[-1].endswith("ERROR: " + err)
        assert handler.state.current_tool is None

    @pytest.mark.asyncio
    async def test_a_tool_error_of_another_tool_leaves_the_running_one_set(self):
        handler, _, _ = _handler(
            [
                ev.ToolCallStartedEvent(tool=_tool("slow")),
                ev.ToolCallErrorEvent(tool=ToolExecution(tool_name="other"), error="x"),
            ]
        )
        await handler.run()
        assert handler.state.current_tool == "slow"


class TestRunErrorAndCompletion:
    @pytest.mark.asyncio
    async def test_a_run_error_is_logged_whole_and_emitted_cut_at_400(self):
        handler, sinks, _ = _handler([ev.RunErrorEvent(content="x" * 500)])
        await handler.run()
        (err,) = sinks.of(RunErrorEvent)
        assert err.error == "x" * 400
        assert handler.state.log[-1].endswith("RUN ERROR: " + "x" * 500)

    @pytest.mark.asyncio
    async def test_content_is_preferred_over_the_error_attribute(self):
        event = ev.RunErrorEvent(content="from content")
        event.error = "from error"  # type: ignore[attr-defined]
        handler, sinks, _ = _handler([event])
        await handler.run()
        assert sinks.of(RunErrorEvent)[0].error == "from content"

    @pytest.mark.asyncio
    async def test_with_no_content_the_error_attribute_then_a_question_mark(self):
        with_error = ev.RunErrorEvent(content="")
        with_error.error = "the error"  # type: ignore[attr-defined]
        handler, sinks, _ = _handler([with_error])
        await handler.run()
        assert sinks.of(RunErrorEvent)[0].error == "the error"

        bare, bare_sinks, _ = _handler([ev.RunErrorEvent(content="")])
        await bare.run()
        assert bare_sinks.of(RunErrorEvent)[0].error == "?"

    @pytest.mark.asyncio
    async def test_a_completion_reports_the_run_token_counts(self):
        metrics = Metrics(input_tokens=11, output_tokens=22, reasoning_tokens=33)
        handler, sinks, _ = _handler([ev.RunCompletedEvent(content="done", metrics=metrics)])
        await handler.run()
        (done,) = sinks.of(AgentCompletedEvent)
        assert (done.input_tokens, done.output_tokens, done.reasoning_tokens) == (11, 22, 33)
        assert done.is_error is False
        assert handler.state.completed_content == "done"

    @pytest.mark.asyncio
    async def test_a_completion_without_metrics_reports_zeros(self):
        handler, sinks, _ = _handler([ev.RunCompletedEvent(content="done", metrics=None)])
        await handler.run()
        (done,) = sinks.of(AgentCompletedEvent)
        assert (done.input_tokens, done.output_tokens, done.reasoning_tokens) == (0, 0, 0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("unset", ["input_tokens", "output_tokens", "reasoning_tokens"])
    async def test_an_unset_token_field_counts_as_zero_and_the_others_stay(self, unset):
        metrics = Metrics(input_tokens=5, output_tokens=6, reasoning_tokens=7)
        setattr(metrics, unset, None)
        handler, sinks, _ = _handler([ev.RunCompletedEvent(content="d", metrics=metrics)])
        await handler.run()
        (done,) = sinks.of(AgentCompletedEvent)
        got = {
            "input_tokens": done.input_tokens,
            "output_tokens": done.output_tokens,
            "reasoning_tokens": done.reasoning_tokens,
        }
        assert got == {"input_tokens": 5, "output_tokens": 6, "reasoning_tokens": 7} | {unset: 0}


class TestVisualization:
    @pytest.mark.asyncio
    async def test_a_visualize_call_emits_a_final_delta_logs_the_spec_and_appends_it(self):
        spec = {"root": "r"}
        tape = [
            ev.RunStartedEvent(run_id="own-run", session_id="s", parent_run_id="top-run"),
            ev.ToolCallStartedEvent(tool=_tool("visualize", {"spec": spec})),
        ]
        handler, sinks, _ = _handler(tape, path=["main", "visualizer"], card_id="k")
        await handler.run()
        (delta,) = sinks.of(VisualizationDeltaEvent)
        assert delta.final is True
        assert delta.spec_id == handler.state.vis_spec_id
        assert delta.spec_json == '{"root": "r"}'
        assert VIS_SPEC_PREFIX + '{"root": "r"}' in handler.state.log
        assert sinks.appended == [(delta, "top-run")]

    @pytest.mark.asyncio
    async def test_the_append_falls_back_to_the_runs_own_id_without_a_parent(self):
        tape = [
            ev.RunStartedEvent(run_id="own-run", session_id="s"),
            ev.ToolCallStartedEvent(tool=_tool("visualize", {"spec": {}})),
        ]
        handler, sinks, _ = _handler(tape)
        await handler.run()
        assert sinks.appended[0][1] == "own-run"

    @pytest.mark.asyncio
    async def test_a_failing_append_is_a_warning_and_the_stream_goes_on(self, caplog):
        sinks = _Sinks()
        sinks.append_error = RuntimeError("disk full")
        tape = [
            ev.ToolCallStartedEvent(tool=_tool("visualize", {"spec": {}})),
            ev.RunCompletedEvent(content="after"),
        ]
        handler, sinks, _ = _handler(tape, sinks=sinks)
        with caplog.at_level(
            logging.WARNING, logger="igni_core.core.tools.orchestrate_agent_stream"
        ):
            await handler.run()
        assert any(
            "visualization event log append failed: disk full" in r.getMessage()
            for r in caplog.records
        )
        assert sinks.of(ToolStartedEvent) and sinks.of(AgentCompletedEvent)

    @pytest.mark.asyncio
    async def test_without_an_appender_the_delta_is_still_emitted(self):
        handler, sinks, _ = _handler(
            [ev.ToolCallStartedEvent(tool=_tool("visualize", {"spec": {}}))], with_appender=False
        )
        await handler.run()
        assert len(sinks.of(VisualizationDeltaEvent)) == 1
        assert sinks.appended == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", [{"spec": "text"}, {"spec": [1]}, {"other": {}}, {}])
    async def test_a_spec_that_is_not_an_object_makes_no_final_delta(self, args):
        handler, sinks, _ = _handler([ev.ToolCallStartedEvent(tool=_tool("visualize", args))])
        await handler.run()
        assert sinks.of(VisualizationDeltaEvent) == []
        assert sinks.appended == []

    @pytest.mark.asyncio
    async def test_another_tool_with_a_spec_argument_makes_no_delta(self):
        handler, sinks, _ = _handler([ev.ToolCallStartedEvent(tool=_tool("plot", {"spec": {}}))])
        await handler.run()
        assert sinks.of(VisualizationDeltaEvent) == []

    @pytest.mark.asyncio
    async def test_the_visualizers_final_answer_is_replaced_by_a_one_line_summary(self):
        tape = [ev.RunCompletedEvent(content='{"huge": "spec"}')]
        viz, _, _ = _handler(tape, path=["main", "visualizer"])
        other, _, _ = _handler(tape, path=["main", "editor"])
        assert (await viz.run())[0] == "Emitted visualization to the client."
        assert (await other.run())[0] == '{"huge": "spec"}'


def _custom(partial: str, *, name: str = "tool_call_input_delta", tool: str = "visualize") -> Any:
    event = CustomEvent(event=name)
    event.tool_name = tool  # type: ignore[attr-defined]
    event.arguments_partial = partial  # type: ignore[attr-defined]
    return event


class TestProgressiveVisualization:
    @pytest.mark.asyncio
    async def test_a_growing_partial_spec_emits_a_non_final_delta(self):
        handler, sinks, _ = _handler([_custom('{"spec": {"root": "r"')])
        await handler.run()
        (delta,) = sinks.of(VisualizationDeltaEvent)
        assert delta.final is False
        assert delta.spec_json == '{"root": "r"}'
        assert delta.spec_id == handler.state.vis_spec_id

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "event",
        [
            _custom('{"spec": {}}', name="other_event"),
            _custom('{"spec": {}}', tool="not_visualize"),
            _custom(""),
            _custom('{"spec": 5}'),
            _custom("garbage"),
        ],
        ids=["other-event", "other-tool", "empty", "spec-not-object", "unparsable"],
    )
    async def test_everything_else_is_ignored(self, event):
        handler, sinks, _ = _handler([event])
        await handler.run()
        assert sinks.of(VisualizationDeltaEvent) == []

    @pytest.mark.asyncio
    async def test_a_partial_that_did_not_grow_is_ignored(self):
        first = _custom('{"spec": {"a": 1}}')
        same = _custom('{"spec": {"a": 2}}')
        handler, sinks, _ = _handler([first, same])
        await handler.run()
        assert len(sinks.of(VisualizationDeltaEvent)) == 1

    @pytest.mark.asyncio
    async def test_a_partial_that_did_not_grow_is_ignored_even_when_not_throttled(
        self, monkeypatch
    ):
        monkeypatch.setattr(SubAgentStreamState, "can_emit_vis_delta", lambda self, now: True)
        first = _custom('{"spec": {"a": 1}}')
        same_length = _custom('{"spec": {"a": 2}}')
        longer = _custom('{"spec": {"a": 2, "b": 3}}')
        handler, sinks, _ = _handler([first, same_length, longer])
        await handler.run()
        assert [json.loads(d.spec_json) for d in sinks.of(VisualizationDeltaEvent)] == [
            {"a": 1},
            {"a": 2, "b": 3},
        ]

    @pytest.mark.asyncio
    async def test_a_second_delta_inside_the_throttle_window_is_dropped(self):
        handler, sinks, _ = _handler(
            [_custom('{"spec": {"a": 1}}'), _custom('{"spec": {"a": 1, "b": 2}}')]
        )
        await handler.run()
        assert len(sinks.of(VisualizationDeltaEvent)) == 1
        assert handler.state.vis_last_emitted_len == len('{"spec": {"a": 1}}')


class TestContentPreview:
    @pytest.mark.asyncio
    async def test_content_emits_a_preview_of_its_tail(self):
        handler, sinks, _ = _handler([ev.RunContentEvent(content="hello\nworld")])
        await handler.run()
        (preview,) = sinks.of(ContentPreviewEvent)
        assert preview.text == "hello\nworld"

    @pytest.mark.asyncio
    async def test_empty_content_emits_nothing(self):
        handler, sinks, _ = _handler(
            [ev.RunContentEvent(content=None), ev.RunContentEvent(content="")]
        )
        await handler.run()
        assert sinks.of(ContentPreviewEvent) == []
        assert handler.state.content_buf == ""


class TestPause:
    @pytest.mark.asyncio
    async def test_a_pause_with_no_bridge_says_so_and_does_not_resume(self):
        paused = ev.RunPausedEvent(run_id="r", session_id="s", requirements=[_Req()])
        handler, sinks, runnable = _handler([paused])
        await handler.run()
        (err,) = sinks.of(RunErrorEvent)
        assert err.error == "paused: no HITL bridge available"
        assert any("paused: no HITL bridge available" in line for line in handler.state.log)
        assert runnable.continued == []

    @pytest.mark.asyncio
    async def test_a_pause_with_nothing_to_wait_for_says_so_even_with_a_bridge(self):
        coordinator = SubAgentHITLCoordinator()
        paused = ev.RunPausedEvent(run_id="r", session_id="s", requirements=[])
        handler, sinks, runnable = _handler([paused], coordinator=coordinator)
        await handler.run()
        assert sinks.of(RunErrorEvent)[0].error == "paused: no HITL bridge available"
        assert runnable.continued == []

    @pytest.mark.asyncio
    async def test_a_pause_on_several_requirements_waits_for_all_then_continues(self):
        reqs = [_Req("a"), _Req("b")]
        pushed: list[str] = []
        waited: list[str] = []
        cleaned: list[str] = []

        class _Bridge:
            async def push_requirement(self, req, *, run_id, agent_path, session_id=""):
                req_id = f"id-{len(pushed)}"
                pushed.append(req_id)
                assert (run_id, session_id, agent_path) == ("r", "s", ["main", "ed"])
                return req_id

            async def wait_resolved(self, req_id):
                waited.append(req_id)

            def cleanup(self, req_id):
                cleaned.append(req_id)

            def forget_spawn(self, run_id):
                pass

        assert isinstance(_Bridge(), HitlCoordinatorProtocol)
        paused = ev.RunPausedEvent(run_id="r", session_id="s", requirements=reqs)
        handler, sinks, runnable = _handler(
            [paused],
            path=["main", "ed"],
            coordinator=_Bridge(),
            follow_up=[ev.RunCompletedEvent(content="ok")],
        )
        await handler.run()
        assert pushed == waited == cleaned == ["id-0", "id-1"]
        assert sinks.of(AgentPausedEvent)[0].count == 2
        assert any("awaiting approval (2 tools)" in line for line in handler.state.log)
        (call,) = runnable.continued
        assert (call["run_id"], call["session_id"], call["stream"]) == ("r", "s", True)
        assert call["requirements"] == reqs

    @pytest.mark.asyncio
    async def test_one_requirement_has_a_plain_line_and_a_count_of_one(self):
        class _Bridge:
            async def push_requirement(self, req, *, run_id, agent_path, session_id=""):
                return "x"

            async def wait_resolved(self, req_id):
                return None

            def cleanup(self, req_id):
                pass

            def forget_spawn(self, run_id):
                pass

        paused = ev.RunPausedEvent(run_id="r", session_id="s", requirements=[_Req()])
        handler, sinks, _ = _handler([paused], coordinator=_Bridge())
        await handler.run()
        assert sinks.of(AgentPausedEvent)[0].count == 1
        line = next(x for x in handler.state.log if "awaiting approval" in x)
        assert "tools)" not in line

    @pytest.mark.asyncio
    async def test_every_requirement_is_cleaned_up_even_when_the_wait_fails(self):
        cleaned: list[str] = []

        class _Bridge:
            async def push_requirement(self, req, *, run_id, agent_path, session_id=""):
                return f"id-{req.tool_execution.tool_name}"

            async def wait_resolved(self, req_id):
                raise RuntimeError("window closed")

            def cleanup(self, req_id):
                cleaned.append(req_id)

            def forget_spawn(self, run_id):
                pass

        paused = ev.RunPausedEvent(run_id="r", session_id="s", requirements=[_Req("a"), _Req("b")])
        handler, _, runnable = _handler([paused], coordinator=_Bridge())
        with pytest.raises(RuntimeError, match="window closed"):
            await handler.run()
        assert cleaned == ["id-a", "id-b"]
        assert runnable.continued == []

    @pytest.mark.asyncio
    async def test_a_pause_without_a_run_id_uses_the_latched_one_then_empty(self):
        class _Bridge:
            def __init__(self) -> None:
                self.run_ids: list[str] = []

            async def push_requirement(self, req, *, run_id, agent_path, session_id=""):
                self.run_ids.append(run_id)
                return "x"

            async def wait_resolved(self, req_id):
                return None

            def cleanup(self, req_id):
                pass

            def forget_spawn(self, run_id):
                pass

        paused = ev.RunPausedEvent(requirements=[_Req()])
        paused.run_id = None
        bridge = _Bridge()
        handler, _, runnable = _handler([paused], coordinator=bridge)
        await handler.run()
        assert bridge.run_ids == [""]
        assert runnable.continued[0]["run_id"] == ""

        bridge2 = _Bridge()
        latched, _, _ = _handler(
            [ev.RunStartedEvent(run_id="latched", session_id="s"), paused], coordinator=bridge2
        )
        await latched.run()
        assert bridge2.run_ids == ["latched"]


class TestTheContractsTheHandlerReliesOn:
    def test_the_real_hitl_coordinator_satisfies_the_bridge_protocol(self):
        assert isinstance(SubAgentHITLCoordinator(), HitlCoordinatorProtocol)

    def test_a_real_agno_agent_and_team_satisfy_the_runnable_protocol(self):
        from agno.agent import Agent
        from agno.team import Team

        assert isinstance(Agent(name="a"), AgnoRunnable)
        assert isinstance(Team(name="t", members=[Agent(name="m")]), AgnoRunnable)

    def test_an_object_missing_the_surface_is_not_a_runnable(self):
        assert not isinstance(object(), AgnoRunnable)
        assert not isinstance(object(), HitlCoordinatorProtocol)
