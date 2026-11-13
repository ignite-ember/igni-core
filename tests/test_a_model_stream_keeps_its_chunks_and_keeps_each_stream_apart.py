"""The tool-argument wrappers hand every chunk on first, and keep one stream's calls to itself.

Agno finishes assembling a tool call only after its stream loop ends, so a
chunk must reach it whole and before any event derived from it. Each stream
gets its own accumulator: two streams in flight at once, with the same call
index, do not see each other's arguments. A call's id and name are the
first ones the stream gave.
"""

from __future__ import annotations

import asyncio

from agno.models.response import ModelResponse
from agno.run.agent import CustomEvent

from igni_core.core.config.model_stream import (
    _aemit_tool_arg_deltas,
    _emit_tool_arg_delta_events,
    _emit_tool_arg_deltas,
    _ToolCallAccumulatorStore,
)


def _chunk(*tool_calls: dict) -> ModelResponse:
    resp = ModelResponse()
    resp.tool_calls = list(tool_calls)  # type: ignore[assignment]
    return resp


def _call(args: str, *, call_id: str | None = None, name: str | None = None, index: int = 0):
    function: dict = {"arguments": args}
    if name is not None:
        function["name"] = name
    call: dict = {"index": index, "function": function}
    if call_id is not None:
        call["id"] = call_id
    return call


def test_the_sync_wrapper_yields_each_chunk_then_its_events():
    first = _chunk(_call("{", call_id="c1", name="visualize"))
    text = ModelResponse()
    text.content = "hi"
    second = _chunk(_call('"a":1}'))

    out = list(_emit_tool_arg_deltas(iter([first, text, second])))

    assert out[0] is first
    assert isinstance(out[1], CustomEvent)
    assert out[2] is text
    assert out[3] is second
    assert isinstance(out[4], CustomEvent)
    assert len(out) == 5
    assert [e.arguments_partial for e in out if isinstance(e, CustomEvent)] == ["{", '{"a":1}']


def test_the_chunk_is_already_delivered_when_the_events_are_built():
    """A consumer that stops after the chunk still has a whole chunk."""
    chunk = _chunk(_call("{", call_id="c1", name="t"))

    stream = _emit_tool_arg_deltas(iter([chunk]))

    assert next(stream) is chunk


def test_a_chunk_that_breaks_event_building_is_still_delivered():
    broken = _chunk(_call(7, call_id="c1", name="t"))  # type: ignore[arg-type]
    after = _chunk(_call("{}", call_id="c2", name="t", index=1))

    out = list(_emit_tool_arg_deltas(iter([broken, after])))

    assert out[0] is broken
    assert out[1] is after
    assert [type(x) for x in out[2:]] == [CustomEvent]


def test_events_already_built_in_a_broken_chunk_are_dropped_with_it():
    store = _ToolCallAccumulatorStore()
    chunk = _chunk(_call("{", call_id="c1", name="t"), _call(7, index=1))  # type: ignore[arg-type]

    assert _emit_tool_arg_delta_events(chunk, store) == []


def test_two_streams_with_the_same_call_index_do_not_share_arguments():
    one = [_chunk(_call("AAA", call_id="a", name="first"))]
    two = [_chunk(_call("BBB", call_id="b", name="second"))]

    events_one = [e for e in _emit_tool_arg_deltas(iter(one)) if isinstance(e, CustomEvent)]
    events_two = [e for e in _emit_tool_arg_deltas(iter(two)) if isinstance(e, CustomEvent)]

    assert events_one[0].arguments_partial == "AAA"
    assert events_two[0].arguments_partial == "BBB"
    assert events_two[0].tool_call_id == "b"


def test_two_async_streams_in_flight_do_not_leak_into_each_other():
    async def source(call_id: str, parts: list[str]):
        for i, part in enumerate(parts):
            yield _chunk(_call(part, call_id=call_id if i == 0 else None, name="t"))
            await asyncio.sleep(0)

    async def collect(stream) -> list[CustomEvent]:
        return [e async for e in stream if isinstance(e, CustomEvent)]

    async def both():
        return await asyncio.gather(
            collect(_aemit_tool_arg_deltas(source("a", ["1", "2", "3"]))),
            collect(_aemit_tool_arg_deltas(source("b", ["x", "y", "z"]))),
        )

    a, b = asyncio.run(both())

    assert [e.arguments_partial for e in a] == ["1", "12", "123"]
    assert [e.arguments_partial for e in b] == ["x", "xy", "xyz"]
    assert {e.tool_call_id for e in a} == {"a"}
    assert {e.tool_call_id for e in b} == {"b"}


def test_the_first_id_and_name_win():
    store = _ToolCallAccumulatorStore()
    events = _emit_tool_arg_delta_events(_chunk(_call("{", call_id="c1", name="real")), store)
    later = _emit_tool_arg_delta_events(_chunk(_call("}", call_id="c2", name="other")), store)

    assert events[0].tool_call_id == "c1"
    assert later[0].tool_call_id == "c1"
    assert later[0].tool_name == "real"
    assert later[0].arguments_partial == "{}"


def test_an_empty_argument_fragment_makes_no_event():
    store = _ToolCallAccumulatorStore()

    assert _emit_tool_arg_delta_events(_chunk(_call("", call_id="c1", name="t")), store) == []


def test_something_that_is_not_a_model_response_makes_no_event():
    store = _ToolCallAccumulatorStore()

    assert _emit_tool_arg_delta_events({"tool_calls": [_call("{")]}, store) == []
    assert _emit_tool_arg_delta_events("text", store) == []
    assert _emit_tool_arg_delta_events(None, store) == []
