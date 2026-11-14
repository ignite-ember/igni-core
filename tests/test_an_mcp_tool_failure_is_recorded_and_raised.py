"""What a failed MCP call says, how it is carried to the wrapper, and what Agno logs.

The real-server end to end is in
``test_a_failing_mcp_tool_reaches_the_model_as_an_error.py``. These are the
pieces: the text of a failed result, the per-call holder, the wrapper on an
entrypoint, and the logging filter on Agno's loggers.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import types

from igni_core.core.mcp import tool_errors
from igni_core.core.mcp.tool_errors import (
    MCPToolFailed,
    error_text,
    raise_on_failure,
    record_failure,
)

pytestmark = pytest.mark.asyncio

NOTHING_SAID = "the tool reported an error and said nothing"


def _result(*content: Any) -> types.CallToolResult:
    return types.CallToolResult(content=list(content), is_error=True)


def _text(text: str) -> types.TextContent:
    return types.TextContent(type="text", text=text)


@dataclass
class _Function:
    entrypoint: Any = None


@dataclass
class _Toolkit:
    functions: Any = field(default_factory=dict)


# ── error_text ───────────────────────────────────────────────────────


async def test_the_text_parts_of_a_failure_are_joined_with_newlines():
    assert error_text(_result(_text("first"), _text("second"))) == "first\nsecond"


async def test_a_part_that_is_not_text_is_named_not_dropped():
    image = types.ImageContent(type="image", data="AAAA", mime_type="image/png")
    assert error_text(_result(_text("see this"), image)) == "see this\n[image content]"
    assert error_text(_result(image)) == "[image content]"


async def test_blank_text_is_left_out_of_the_join():
    assert error_text(_result(_text("  "), _text("real"), _text(""))) == "real"


async def test_a_failure_with_nothing_in_it_says_it_said_nothing():
    assert error_text(_result()) == NOTHING_SAID
    assert error_text(_result(_text(" \n"))) == NOTHING_SAID
    assert error_text(SimpleNamespace(content=None)) == NOTHING_SAID
    assert error_text(object()) == NOTHING_SAID


# ── record_failure and the wrapper ───────────────────────────────────


async def test_recording_outside_a_call_does_nothing():
    record_failure("nobody is listening", answered=True)
    assert tool_errors._failures.get() is None


async def test_a_recorded_failure_raises_with_the_servers_text_and_answered_flag():
    async def entrypoint() -> str:
        record_failure("no such ticket", answered=True)
        return "Error from MCP tool: [TextContent(...)]"

    toolkit = _Toolkit({"t": _Function(entrypoint)})
    raise_on_failure(toolkit)

    with pytest.raises(MCPToolFailed) as raised:
        await toolkit.functions["t"].entrypoint()
    assert str(raised.value) == "no such ticket"
    assert raised.value.answered is True


async def test_a_failure_the_server_never_answered_is_marked_unanswered():
    async def entrypoint() -> None:
        record_failure("calling t failed: timed out", answered=False)

    toolkit = _Toolkit({"t": _Function(entrypoint)})
    raise_on_failure(toolkit)

    with pytest.raises(MCPToolFailed) as raised:
        await toolkit.functions["t"].entrypoint()
    assert raised.value.answered is False


async def test_the_last_of_several_failures_in_one_call_wins():
    async def entrypoint() -> None:
        record_failure("first", answered=False)
        record_failure("second", answered=True)

    toolkit = _Toolkit({"t": _Function(entrypoint)})
    raise_on_failure(toolkit)

    with pytest.raises(MCPToolFailed, match="second") as raised:
        await toolkit.functions["t"].entrypoint()
    assert raised.value.answered is True


async def test_a_call_that_did_not_fail_returns_its_result_unchanged():
    sentinel = object()

    async def entrypoint(x: int) -> object:
        return sentinel

    toolkit = _Toolkit({"t": _Function(entrypoint)})
    raise_on_failure(toolkit)

    assert await toolkit.functions["t"].entrypoint(1) is sentinel


async def test_an_entrypoint_that_raises_its_own_error_propagates_and_clears_the_holder():
    async def entrypoint() -> None:
        raise ValueError("its own")

    toolkit = _Toolkit({"t": _Function(entrypoint)})
    raise_on_failure(toolkit)

    with pytest.raises(ValueError, match="its own"):
        await toolkit.functions["t"].entrypoint()
    assert tool_errors._failures.get() is None


async def test_two_concurrent_calls_do_not_see_each_others_failure():
    release = asyncio.Event()

    async def failing() -> str:
        record_failure("only the first failed", answered=True)
        release.set()
        return "x"

    async def fine() -> str:
        await release.wait()
        return "fine"

    toolkit = _Toolkit({"a": _Function(failing), "b": _Function(fine)})
    raise_on_failure(toolkit)

    results = await asyncio.gather(
        toolkit.functions["a"].entrypoint(),
        toolkit.functions["b"].entrypoint(),
        return_exceptions=True,
    )
    assert isinstance(results[0], MCPToolFailed)
    assert results[1] == "fine"


async def test_wrapping_twice_wraps_once_and_keeps_the_signature():
    async def entrypoint(query: str, _agno_agent: object = None) -> str:
        return query

    function = _Function(entrypoint)
    toolkit = _Toolkit({"t": function})
    raise_on_failure(toolkit)
    wrapped = function.entrypoint
    raise_on_failure(toolkit)

    assert function.entrypoint is wrapped
    assert wrapped is not entrypoint
    assert wrapped.__wrapped__ is entrypoint
    assert list(inspect.signature(wrapped).parameters) == ["query", "_agno_agent"]
    assert await wrapped("q", _agno_agent=None) == "q"


async def test_a_toolkit_with_no_functions_or_no_entrypoint_is_tolerated():
    raise_on_failure(_Toolkit(None))
    raise_on_failure(object())
    bare = _Function(None)
    raise_on_failure(_Toolkit({"t": bare}))
    assert bare.entrypoint is None


# ── what Agno logs ───────────────────────────────────────────────────


def _record(logger_name: str, exc: BaseException | None) -> logging.LogRecord:
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    return logging.LogRecord(logger_name, logging.ERROR, __file__, 1, "boom", None, exc_info)


async def test_the_traceback_of_a_failed_call_goes_to_debug_and_not_the_agno_log(caplog):
    raise_on_failure(_Toolkit({}))
    failed = MCPToolFailed("no such ticket", answered=True)

    with caplog.at_level(logging.DEBUG, logger=tool_errors.logger.name):
        kept = [
            logging.getLogger(name).filter(_record(name, failed))
            for name in ("agno", "agno-team", "agno-workflow")
        ]

    assert kept == [False, False, False]
    assert any("MCP tool failed: no such ticket" in r.getMessage() for r in caplog.records)


async def test_every_other_record_on_agnos_logger_is_left_alone():
    raise_on_failure(_Toolkit({}))
    agno = logging.getLogger("agno")
    assert agno.filter(_record("agno", RuntimeError("something else")))
    assert agno.filter(_record("agno", None))


async def test_the_filter_is_on_each_agno_logger_once_however_often_wrapping_runs():
    for _ in range(3):
        raise_on_failure(_Toolkit({}))
    for name in ("agno", "agno-team", "agno-workflow"):
        assert logging.getLogger(name).filters.count(tool_errors._ONE_LINE) == 1
