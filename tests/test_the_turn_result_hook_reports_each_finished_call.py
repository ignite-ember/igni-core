"""TurnResultHook: every finished call's result is handed to the sink, so a restart
mid-turn keeps them; the result itself goes back to the agent untouched.

The call's id is found among the run's messages by name and arguments; with no run
context, or no such call, nothing is reported.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.core.hooks.turn_results import TurnResultHook, TurnResultSink


class _Sink:
    def __init__(self) -> None:
        self.finished: list[tuple[str, str, str]] = []

    def tool_finished(self, run_id: str, tool_call_id: str, result: str) -> None:
        self.finished.append((run_id, tool_call_id, result))


def _call(call_id: str, name: str, arguments: dict[str, Any] | str) -> dict[str, Any]:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id, "function": {"name": name, "arguments": raw}}


def _run(*calls: dict[str, Any], run_id: Any = "run-1") -> SimpleNamespace:
    message = SimpleNamespace(role="assistant", tool_calls=list(calls))
    return SimpleNamespace(run_id=run_id, messages=[message])


def test_the_sink_is_a_protocol_anything_with_tool_finished_satisfies():
    assert isinstance(_Sink(), TurnResultSink)
    assert not isinstance(object(), TurnResultSink)


def test_the_hook_is_a_coroutine_function_to_agno():
    assert inspect.iscoroutinefunction(TurnResultHook(_Sink()))
    assert TurnResultHook(_Sink()).__name__ == "TurnResultHook"


async def test_a_finished_call_is_reported_with_its_id_and_the_result_returned_as_it_is():
    sink = _Sink()
    hook = TurnResultHook(sink)
    payload = {"rows": 2}

    result = await hook(
        name="query",
        func=lambda **kw: payload,
        args={"q": "x"},
        run_context=_run(_call("call-1", "query", {"q": "x"})),
    )

    assert result is payload
    assert sink.finished == [("run-1", "call-1", "{'rows': 2}")]


async def test_an_async_tool_is_awaited_before_it_is_reported():
    sink = _Sink()

    async def tool(**kw: Any) -> str:
        await asyncio.sleep(0)
        return "late"

    result = await TurnResultHook(sink)(
        name="t", func=tool, args={}, run_context=_run(_call("c", "t", {}))
    )

    assert result == "late" and sink.finished == [("run-1", "c", "late")]


async def test_a_run_id_that_is_not_text_is_reported_as_text():
    sink = _Sink()

    await TurnResultHook(sink)(
        name="t", func=lambda **kw: "r", args={}, run_context=_run(_call("c", "t", {}), run_id=7)
    )

    assert sink.finished[0][0] == "7"


async def test_with_no_run_context_nothing_is_reported():
    sink = _Sink()

    assert await TurnResultHook(sink)(name="t", func=lambda **kw: "r", args={}) == "r"
    assert sink.finished == []


@pytest.mark.parametrize("messages", [None, [], "missing"])
async def test_a_run_with_no_messages_reports_nothing(messages):
    sink = _Sink()
    context = SimpleNamespace(run_id="r")
    if messages != "missing":
        context.messages = messages

    await TurnResultHook(sink)(name="t", func=lambda **kw: "r", args={}, run_context=context)

    assert sink.finished == []


async def test_a_call_the_messages_do_not_hold_is_not_reported():
    sink = _Sink()

    await TurnResultHook(sink)(
        name="t",
        func=lambda **kw: "r",
        args={"a": 1},
        run_context=_run(_call("c1", "other", {"a": 1}), _call("c2", "t", {"a": 2})),
    )

    assert sink.finished == []


async def test_two_calls_of_one_tool_with_different_arguments_are_told_apart():
    sink = _Sink()
    context = _run(_call("c1", "t", {"a": 1}), _call("c2", "t", {"a": 2}))
    hook = TurnResultHook(sink)

    await hook(name="t", func=lambda **kw: "one", args={"a": 1}, run_context=context)
    await hook(name="t", func=lambda **kw: "two", args={"a": 2}, run_context=context)

    assert sink.finished == [("run-1", "c1", "one"), ("run-1", "c2", "two")]


async def test_a_failing_tool_propagates_and_reports_nothing():
    sink = _Sink()

    def tool(**kw: Any) -> str:
        raise RuntimeError("down")

    with pytest.raises(RuntimeError, match="down"):
        await TurnResultHook(sink)(
            name="t", func=tool, args={}, run_context=_run(_call("c", "t", {}))
        )

    assert sink.finished == []


async def test_a_call_with_no_function_returns_nothing():
    result = await TurnResultHook(_Sink())(name="t", func=None, args=None)

    assert result is None


async def test_a_large_result_is_kept_whole():
    sink = _Sink()
    big = "z" * 100_000

    await TurnResultHook(sink)(
        name="t", func=lambda **kw: big, args={}, run_context=_run(_call("c", "t", {}))
    )

    assert sink.finished[0][2] == big
