"""A resumed sub-agent continues its paused run instead of starting one."""

from __future__ import annotations

from typing import Any

import pytest
from agno.run import agent as ev

from igni_core.core.tools.orchestrate_events import AgentCompletedEvent
from igni_core.core.tools.orchestrate_registry import SubAgentRegistry
from igni_core.core.tools.orchestrate_resumed_agent_stream import ResumedSubAgentStreamHandler
from igni_core.core.tools.orchestrate_stream_context import (
    ResumeSpec,
    StreamContext,
    StreamSinks,
)


class _Runnable:
    def __init__(self) -> None:
        self.arun_calls: list[str] = []
        self.continued: list[dict[str, Any]] = []

    def arun(self, task: str, *, stream: bool = True) -> Any:
        self.arun_calls.append(task)
        return self._play()

    def acontinue_run(self, run_response: Any = None, **kwargs: Any) -> Any:
        # Record exactly which keywords the handler passed.
        self.continued.append({"run_response": run_response, **kwargs})
        return self._play()

    @staticmethod
    async def _play():
        yield ev.RunCompletedEvent(content="continued answer", run_id="r1", session_id="s1")

    async def aget_run_output(self, *, run_id: str, session_id: str) -> Any:
        return None

    async def aget_last_run_output(self, *, session_id: str) -> Any:
        return None


def _handler(resume: ResumeSpec) -> tuple[ResumedSubAgentStreamHandler, _Runnable, list[Any]]:
    runnable = _Runnable()
    events: list[Any] = []
    context = StreamContext(
        runnable=runnable,
        task="original task",
        agent_path=["main", "ed"],
        sinks=StreamSinks(on_progress=events.append, subagent_registry=SubAgentRegistry()),
    )
    return ResumedSubAgentStreamHandler(context, resume), runnable, events


class TestTheFirstStream:
    @pytest.mark.asyncio
    async def test_a_stored_run_is_continued_by_its_id_and_the_task_is_not_rerun(self):
        reqs = [object()]
        handler, runnable, _ = _handler(ResumeSpec(run_id="r1", session_id="s1", requirements=reqs))
        result, _ = await handler.run()
        assert runnable.arun_calls == []
        assert runnable.continued == [
            {
                "run_response": None,
                "run_id": "r1",
                "session_id": "s1",
                "requirements": reqs,
                "stream": True,
            }
        ]
        assert result == "continued answer"

    @pytest.mark.asyncio
    async def test_a_rebuilt_run_is_continued_from_the_object_not_the_id(self):
        rebuilt = object()
        reqs = [object()]
        handler, runnable, _ = _handler(
            ResumeSpec(run_id="r1", session_id="s1", requirements=reqs, run_response=rebuilt)
        )
        await handler.run()
        (call,) = runnable.continued
        assert call["run_response"] is rebuilt
        assert "run_id" not in call
        assert (call["session_id"], call["requirements"], call["stream"]) == ("s1", reqs, True)


class TestTheState:
    def test_the_session_id_is_seeded_from_the_resume_spec(self):
        handler, _, _ = _handler(ResumeSpec(run_id="r1", session_id="seeded"))
        assert handler.state.current_session_id == "seeded"
        assert handler.state.current_run_id is None

    @pytest.mark.asyncio
    async def test_the_continued_run_registers_and_reports_like_a_live_one(self):
        handler, _, events = _handler(ResumeSpec(run_id="r1", session_id="s1"))
        registry = handler._context.sinks.subagent_registry
        seen_during: list[bool] = []
        original = handler._handle

        async def spy(event: Any) -> Any:
            out = await original(event)
            seen_during.append("r1" in registry)
            return out

        handler._handle = spy  # type: ignore[method-assign]
        await handler.run()
        assert seen_during == [True]
        assert "r1" not in registry
        assert [type(e) for e in events] == [AgentCompletedEvent]
        assert events[0].agent_path == "main.ed"
