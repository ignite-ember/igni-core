"""Each Agno event kind becomes its one protocol message, and only that one.

The handlers are built here with a fake formatter and detector, so what
is asserted is the handler's own reading of the event: which fields it
copies, what it coerces, what it defaults and when it says nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from agno.models.response import ToolExecution
from agno.run import agent as agent_events
from agno.run import team as team_events
from agno.run.requirement import RunRequirement

from igni_core.protocol import messages as msg
from igni_core.protocol.event_handlers import (
    ContentHandler,
    EventHandler,
    FallbackContentHandler,
    ModelCompletedHandler,
    ReasoningContentHandler,
    ReasoningStartedHandler,
    RunCompletedHandler,
    RunErrorHandler,
    RunPausedHandler,
    RunStartedHandler,
    StreamingDoneHandler,
    TaskCreatedHandler,
    TaskIterationHandler,
    TaskStateUpdatedHandler,
    TaskUpdatedHandler,
    ToolCompletedHandler,
    ToolErrorHandler,
    ToolStartedHandler,
)
from igni_core.protocol.serializer import AgnoEventSerializer, serialize_event


class _Formatter:
    """Stands in for the tool formatter: names and summaries are recognisable."""

    def friendly_name(self, name: str, default: str = "") -> str:
        return f"friendly({name})" if name != "unnamed" else default

    def args_summary(self, name: str, args: Any) -> str:
        return f"args({name},{args})"

    def extract_result(self, event: Any) -> SimpleNamespace:
        return SimpleNamespace(
            summary="the summary",
            full_result=event.tool.result,
            has_markup=True,
            diff_rows=[],
        )


class _Detector:
    def is_error(self, result: str) -> bool:
        return result.startswith("ERR")

    def is_rejected(self, result: str) -> bool:
        return result.startswith("NO")


def _handler(cls: type[EventHandler]) -> EventHandler:
    return cls(_Formatter(), _Detector())  # type: ignore[arg-type]


class TestMatching:
    def test_a_handler_claims_the_events_of_its_types_only(self):
        handler = _handler(ContentHandler)
        assert handler.matches(agent_events.RunContentEvent(content="x"))
        assert handler.matches(team_events.RunContentEvent(content="x"))
        assert not handler.matches(agent_events.RunErrorEvent(content="x"))
        assert not handler.matches(object())

    def test_the_base_handler_builds_nothing_by_itself(self):
        with pytest.raises(NotImplementedError):
            _handler(EventHandler).build(object())

    def test_the_fallback_claims_an_event_with_a_string_content_only(self):
        handler = _handler(FallbackContentHandler)
        assert handler.matches(SimpleNamespace(content="text"))
        assert handler.matches(SimpleNamespace(content=""))
        assert not handler.matches(SimpleNamespace(content=5))
        assert not handler.matches(SimpleNamespace(content=None))
        assert not handler.matches(SimpleNamespace())

    def test_a_typed_event_with_string_content_is_built_by_its_typed_handler(self):
        """The fallback sits last: an error event's content is an error, not a delta."""
        message = serialize_event(agent_events.RunErrorEvent(content="boom"))
        assert isinstance(message, msg.RunError)
        assert message.error == "boom"

    def test_the_default_handler_list_ends_with_the_fallback(self):
        serializer = AgnoEventSerializer.default()
        assert isinstance(serializer._handlers[-1], FallbackContentHandler)
        assert not any(isinstance(h, FallbackContentHandler) for h in serializer._handlers[:-1])

    def test_an_event_nobody_claims_says_nothing(self):
        assert serialize_event(SimpleNamespace(content=7)) is None


class TestReasoningContent:
    def test_the_reasoning_becomes_a_thinking_delta(self):
        event = agent_events.ReasoningContentDeltaEvent(reasoning_content="pondering")
        message = _handler(ReasoningContentHandler).build(event)
        assert message == msg.ContentDelta(text="pondering", is_thinking=True)

    def test_literal_think_tags_are_stripped_wherever_they_occur(self):
        event = agent_events.ReasoningContentDeltaEvent(
            reasoning_content="<think>one</think> and <think>two</think>"
        )
        message = _handler(ReasoningContentHandler).build(event)
        assert isinstance(message, msg.ContentDelta)
        assert message.text == "one and two"

    @pytest.mark.parametrize("reasoning", ["", None])
    def test_empty_reasoning_says_nothing(self, reasoning):
        event = agent_events.ReasoningContentDeltaEvent(reasoning_content=reasoning)
        assert _handler(ReasoningContentHandler).build(event) is None


class TestContent:
    def test_the_content_becomes_a_visible_delta(self):
        message = _handler(ContentHandler).build(agent_events.RunContentEvent(content="hi"))
        assert message == msg.ContentDelta(text="hi", is_thinking=False)

    @pytest.mark.parametrize("content", ["", None])
    def test_empty_content_says_nothing(self, content):
        assert _handler(ContentHandler).build(agent_events.RunContentEvent(content=content)) is None

    def test_the_fallback_surfaces_a_string_content_as_a_visible_delta(self):
        message = _handler(FallbackContentHandler).build(SimpleNamespace(content="loose"))
        assert message == msg.ContentDelta(text="loose", is_thinking=False)

    def test_the_fallback_says_nothing_for_an_empty_string(self):
        assert _handler(FallbackContentHandler).build(SimpleNamespace(content="")) is None


class TestTools:
    def test_a_started_tool_carries_its_names_summary_and_run(self):
        event = agent_events.ToolCallStartedEvent(
            run_id="r1", tool=ToolExecution(tool_name="read_file", tool_args={"path": "a"})
        )
        message = _handler(ToolStartedHandler).build(event)
        assert message == msg.ToolStarted(
            tool_name="read_file",
            friendly_name="friendly(read_file)",
            args_summary="args(read_file,{'path': 'a'})",
            run_id="r1",
        )

    def test_a_started_event_without_a_tool_is_called_tool(self):
        event = agent_events.ToolCallStartedEvent(run_id="r1", tool=None)
        message = _handler(ToolStartedHandler).build(event)
        assert isinstance(message, msg.ToolStarted)
        assert message.tool_name == "tool"
        assert message.args_summary == "args(tool,None)"

    def test_a_started_tool_without_a_name_is_called_tool(self):
        event = agent_events.ToolCallStartedEvent(tool=ToolExecution(tool_name=None))
        message = _handler(ToolStartedHandler).build(event)
        assert isinstance(message, msg.ToolStarted)
        assert message.tool_name == "tool"

    def test_a_completed_tool_carries_the_result_and_what_the_detector_says(self):
        event = agent_events.ToolCallCompletedEvent(
            run_id="r2", tool=ToolExecution(tool_name="t", result="all fine")
        )
        message = _handler(ToolCompletedHandler).build(event)
        assert message == msg.ToolCompleted(
            summary="the summary",
            full_result="all fine",
            has_markup=True,
            diff_rows=[],
            run_id="r2",
            is_error=False,
            is_rejected=False,
        )

    def test_a_completed_tool_is_an_error_when_the_detector_says_so(self):
        event = agent_events.ToolCallCompletedEvent(
            tool=ToolExecution(tool_name="t", result="ERR x")
        )
        message = _handler(ToolCompletedHandler).build(event)
        assert isinstance(message, msg.ToolCompleted)
        assert (message.is_error, message.is_rejected) == (True, False)

    def test_a_completed_tool_is_rejected_when_the_detector_says_so(self):
        event = agent_events.ToolCallCompletedEvent(
            tool=ToolExecution(tool_name="t", result="NO x")
        )
        message = _handler(ToolCompletedHandler).build(event)
        assert isinstance(message, msg.ToolCompleted)
        assert (message.is_error, message.is_rejected) == (False, True)

    def test_a_real_completed_event_is_read_through_the_serializer(self):
        event = agent_events.ToolCallCompletedEvent(
            run_id="r3", tool=ToolExecution(tool_name="run_shell_command", result="Error: nope")
        )
        message = serialize_event(event)
        assert isinstance(message, msg.ToolCompleted)
        assert message.run_id == "r3"
        assert message.full_result == "Error: nope"
        assert message.is_error is True

    def test_a_tool_error_carries_its_text_and_run(self):
        event = agent_events.ToolCallErrorEvent(run_id="r4", error="it blew up")
        message = _handler(ToolErrorHandler).build(event)
        assert message == msg.ToolError(error="it blew up", run_id="r4")

    def test_a_tool_error_without_text_says_unknown_error(self):
        message = _handler(ToolErrorHandler).build(SimpleNamespace())
        assert isinstance(message, msg.ToolError)
        assert message.error == "Unknown error"

    def test_a_tool_error_that_is_not_text_is_written_as_text(self):
        event = agent_events.ToolCallErrorEvent(error=ValueError("bad"))  # type: ignore[arg-type]
        message = _handler(ToolErrorHandler).build(event)
        assert isinstance(message, msg.ToolError)
        assert message.error == "bad"


class TestModelCompleted:
    def test_the_counts_and_ids_are_copied(self):
        event = agent_events.ModelRequestCompletedEvent(
            run_id="r", parent_run_id="p", input_tokens=7, output_tokens=3
        )
        message = _handler(ModelCompletedHandler).build(event)
        assert message == msg.ModelCompleted(
            input_tokens=7, output_tokens=3, run_id="r", parent_run_id="p"
        )

    def test_absent_counts_are_zero(self):
        event = agent_events.ModelRequestCompletedEvent(input_tokens=None, output_tokens=None)
        message = _handler(ModelCompletedHandler).build(event)
        assert isinstance(message, msg.ModelCompleted)
        assert (message.input_tokens, message.output_tokens) == (0, 0)


class TestRunStarted:
    def test_an_agent_run_carries_name_ids_and_model(self):
        event = agent_events.RunStartedEvent(
            agent_name="editor", run_id="r1", parent_run_id="p0", model="m-1"
        )
        message = _handler(RunStartedHandler).build(event)
        assert message == msg.RunStarted(
            agent_name="editor", run_id="r1", parent_run_id="p0", model="m-1"
        )

    def test_a_team_run_carries_the_team_name(self):
        event = team_events.RunStartedEvent(team_name="reviewers", run_id="r1")
        message = _handler(RunStartedHandler).build(event)
        assert isinstance(message, msg.RunStarted)
        assert message.agent_name == "reviewers"

    def test_a_nameless_team_run_says_nothing(self):
        assert _handler(RunStartedHandler).build(team_events.RunStartedEvent(run_id="r1")) is None

    def test_a_nameless_agent_run_says_nothing(self):
        assert _handler(RunStartedHandler).build(agent_events.RunStartedEvent(run_id="r1")) is None

    def test_a_run_without_an_id_says_nothing(self):
        event = agent_events.RunStartedEvent(agent_name="editor", run_id="")
        assert _handler(RunStartedHandler).build(event) is None

    def test_an_absent_model_is_empty(self):
        event = agent_events.RunStartedEvent(agent_name="editor", run_id="r1", model=None)
        message = _handler(RunStartedHandler).build(event)
        assert isinstance(message, msg.RunStarted)
        assert message.model == ""


class TestRunEnds:
    def test_a_completed_run_carries_its_metrics(self):
        metrics = SimpleNamespace(
            input_tokens=11, output_tokens=5, reasoning_tokens=2, duration=1.5
        )
        event = SimpleNamespace(run_id="r", parent_run_id="p", metrics=metrics)
        message = _handler(RunCompletedHandler).build(event)
        assert message == msg.RunCompleted(
            run_id="r",
            parent_run_id="p",
            input_tokens=11,
            output_tokens=5,
            reasoning_tokens=2,
            duration=1.5,
        )

    def test_a_completed_run_without_metrics_is_all_zero(self):
        event = agent_events.RunCompletedEvent(run_id="r", metrics=None)
        message = _handler(RunCompletedHandler).build(event)
        assert isinstance(message, msg.RunCompleted)
        assert (message.input_tokens, message.output_tokens) == (0, 0)
        assert (message.reasoning_tokens, message.duration) == (0, 0.0)

    def test_metrics_with_none_fields_count_as_zero(self):
        metrics = SimpleNamespace(
            input_tokens=None, output_tokens=None, reasoning_tokens=None, duration=None
        )
        message = _handler(RunCompletedHandler).build(SimpleNamespace(metrics=metrics))
        assert isinstance(message, msg.RunCompleted)
        assert (message.input_tokens, message.reasoning_tokens, message.duration) == (0, 0, 0.0)

    def test_the_end_of_visible_content_carries_the_run(self):
        event = agent_events.RunContentCompletedEvent(run_id="r9")
        assert _handler(StreamingDoneHandler).build(event) == msg.StreamingDone(run_id="r9")

    def test_a_run_error_carries_the_event_content(self):
        message = _handler(RunErrorHandler).build(agent_events.RunErrorEvent(content="failed"))
        assert message == msg.RunError(error="failed")

    def test_a_run_error_without_content_is_written_as_text(self):
        message = _handler(RunErrorHandler).build(SimpleNamespace())
        assert message == msg.RunError(error="Unknown error")

    def test_the_reasoning_phase_starting_carries_the_run(self):
        event = agent_events.ReasoningStartedEvent(run_id="r5")
        assert _handler(ReasoningStartedHandler).build(event) == msg.ReasoningStarted(run_id="r5")


class TestTasks:
    def test_a_created_task_carries_its_fields(self):
        event = team_events.TaskCreatedEvent(
            task_id="t1", title="write", assignee="ann", status="running"
        )
        message = _handler(TaskCreatedHandler).build(event)
        assert message == msg.TaskCreated(
            task_id="t1", title="write", assignee="ann", status="running"
        )

    def test_a_created_task_with_no_assignee_has_an_empty_one(self):
        event = team_events.TaskCreatedEvent(task_id="t1", title="write", assignee=None)
        message = _handler(TaskCreatedHandler).build(event)
        assert isinstance(message, msg.TaskCreated)
        assert message.assignee == ""

    def test_the_fields_of_a_bare_object_take_their_defaults(self):
        message = _handler(TaskCreatedHandler).build(SimpleNamespace())
        assert message == msg.TaskCreated(task_id="", title="", assignee="", status="pending")

    def test_ids_are_written_as_text(self):
        message = _handler(TaskCreatedHandler).build(SimpleNamespace(task_id=12))
        assert isinstance(message, msg.TaskCreated)
        assert message.task_id == "12"

    def test_an_updated_task_carries_status_and_assignee(self):
        event = team_events.TaskUpdatedEvent(task_id=3, status="completed", assignee="bob")  # type: ignore[arg-type]
        message = _handler(TaskUpdatedHandler).build(event)
        assert message == msg.TaskUpdated(task_id="3", status="completed", assignee="bob")

    def test_an_updated_task_with_nothing_has_empty_fields(self):
        message = _handler(TaskUpdatedHandler).build(SimpleNamespace(assignee=None))
        assert message == msg.TaskUpdated(task_id="", status="", assignee="")

    def test_an_iteration_carries_its_numbers(self):
        event = team_events.TaskIterationStartedEvent(iteration=2, max_iterations=5)
        message = _handler(TaskIterationHandler).build(event)
        assert message == msg.TaskIteration(iteration=2, max_iterations=5)

    def test_an_iteration_with_nothing_is_zero(self):
        message = _handler(TaskIterationHandler).build(SimpleNamespace())
        assert message == msg.TaskIteration(iteration=0, max_iterations=0)

    def test_a_state_update_has_one_snapshot_per_task(self):
        tasks = [
            SimpleNamespace(task_id="a", title="A", status="pending", assignee=None),
            SimpleNamespace(task_id="b", title="B", status="running", assignee="ann"),
        ]
        message = _handler(TaskStateUpdatedHandler).build(SimpleNamespace(tasks=tasks))
        assert message == msg.TaskStateUpdated(
            tasks=[
                msg.TaskSnapshot(task_id="a", title="A", status="pending", assignee=""),
                msg.TaskSnapshot(task_id="b", title="B", status="running", assignee="ann"),
            ]
        )

    @pytest.mark.parametrize("tasks", [None, []])
    def test_a_state_update_without_tasks_has_an_empty_list(self, tasks):
        message = _handler(TaskStateUpdatedHandler).build(SimpleNamespace(tasks=tasks))
        assert message == msg.TaskStateUpdated(tasks=[])


class TestRunPaused:
    @staticmethod
    def _requirement(name: str, args: dict[str, Any]) -> RunRequirement:
        return RunRequirement(
            ToolExecution(tool_name=name, tool_args=args, requires_confirmation=True)
        )

    def test_every_active_requirement_becomes_a_request_with_its_friendly_name(self):
        first = self._requirement("read_file", {"path": "a"})
        second = self._requirement("unnamed", {})
        event = agent_events.RunPausedEvent(run_id="r7", requirements=[first, second])
        message = _handler(RunPausedHandler).build(event)
        assert isinstance(message, msg.RunPaused)
        assert message.run_id == "r7"
        assert [(r.tool_name, r.friendly_name, r.tool_args) for r in message.requirements] == [
            ("read_file", "friendly(read_file)", {"path": "a"}),
            ("unnamed", "", {}),
        ]
        assert [r.requirement_id for r in message.requirements] == [str(id(first)), str(id(second))]

    def test_a_resolved_requirement_is_left_out(self):
        done = self._requirement("read_file", {})
        done.confirm()
        open_ = self._requirement("write_file", {})
        event = agent_events.RunPausedEvent(requirements=[done, open_])
        message = _handler(RunPausedHandler).build(event)
        assert isinstance(message, msg.RunPaused)
        assert [r.tool_name for r in message.requirements] == ["write_file"]

    @pytest.mark.parametrize("requirements", [None, []])
    def test_a_pause_without_requirements_lists_none(self, requirements):
        event = agent_events.RunPausedEvent(run_id="r", requirements=requirements)
        message = _handler(RunPausedHandler).build(event)
        assert message == msg.RunPaused(run_id="r", requirements=[])
