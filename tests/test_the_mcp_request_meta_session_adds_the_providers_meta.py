"""Every tool call carries the provider's ``_meta``, and is busy while it runs.

A real :class:`RequestMetaSession` against a real in-memory MCP server
whose tools report the ``_meta`` they received. The provider-for-an-
extension path is in ``test_every_call_names_its_caller.py``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.message import SessionMessage

from igni_core.core.activity import ActivityRegistry, BusyKind
from igni_core.core.mcp import tool_errors
from igni_core.core.mcp.request_meta import RequestMetaSession

pytestmark = pytest.mark.asyncio


def _server() -> MCPServer:
    server = MCPServer("meta")
    server.busy_seen = []  # type: ignore[attr-defined]

    @server.tool()
    async def meta(ctx: Context) -> str:  # type: ignore[type-arg]
        received = ctx.request_context.meta
        busy = ActivityRegistry.shared().busy()
        server.busy_seen.append([(b.kind, b.holder, b.reason) for b in busy])  # type: ignore[attr-defined]
        return json.dumps(dict(received) if received else None)

    @server.tool()
    async def fails() -> str:
        from mcp.server.mcpserver.exceptions import ToolError

        raise ToolError("no such ticket")

    @server.tool()
    async def slow() -> str:
        await anyio.sleep(30)
        return "late"

    return server


@asynccontextmanager
async def _session(**kwargs: Any) -> AsyncIterator[tuple[RequestMetaSession, MCPServer]]:
    server = _server()
    to_server_send, to_server_receive = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](8)
    to_client_send, to_client_receive = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](8)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            server._lowlevel_server.run,
            to_server_receive,
            to_client_send,
            server._lowlevel_server.create_initialization_options(),
        )
        async with RequestMetaSession(to_client_receive, to_server_send, **kwargs) as session:
            await session.initialize()
            yield session, server
        tasks.cancel_scope.cancel()


async def _meta(session: RequestMetaSession, **kwargs: Any) -> Any:
    result = await session.call_tool("meta", {}, **kwargs)
    return json.loads(result.content[0].text)  # type: ignore[union-attr]


async def test_the_provider_is_asked_at_every_call():
    answers = iter([{"n": 1}, {"n": 2}])
    async with _session(request_meta=lambda: next(answers)) as (session, _):
        assert await _meta(session) == {"n": 1}
        assert await _meta(session) == {"n": 2}


async def test_the_provider_wins_over_meta_the_caller_passed_on_a_clash():
    async with _session(request_meta=lambda: {"who": "provider", "extra": 1}) as (session, _):
        got = await _meta(session, meta={"who": "caller", "keep": True})
    assert got == {"who": "provider", "extra": 1, "keep": True}


@pytest.mark.parametrize("provided", [None, {}])
async def test_a_provider_with_nothing_to_add_leaves_the_callers_meta(provided):
    async with _session(request_meta=lambda: provided) as (session, _):
        assert await _meta(session, meta={"keep": True}) == {"keep": True}


async def test_with_no_provider_the_call_goes_out_unchanged():
    async with _session() as (session, _):
        assert await _meta(session) is None
        assert await _meta(session, meta={"keep": True}) == {"keep": True}


async def test_the_call_is_busy_for_its_holder_and_tool_and_released_after():
    async with _session(busy_holder="mcp:notes") as (session, server):
        await _meta(session)
        assert server.busy_seen == [[(BusyKind.MCP_CALL, "mcp:notes", "meta")]]  # type: ignore[attr-defined]
        assert ActivityRegistry.shared().busy() == []


async def test_the_default_busy_holder_is_mcp():
    async with _session() as (session, server):
        await _meta(session)
        assert server.busy_seen[0][0][1] == "mcp"  # type: ignore[attr-defined]


async def test_a_tool_that_reports_an_error_is_returned_and_recorded_as_answered():
    async with _session() as (session, _):
        holder: list[tuple[str, bool]] = []
        token = tool_errors._failures.set(holder)
        try:
            result = await session.call_tool("fails", {})
        finally:
            tool_errors._failures.reset(token)
    assert result.is_error
    assert len(holder) == 1
    assert "no such ticket" in holder[0][0]
    assert holder[0][1] is True


async def test_a_call_that_times_out_is_recorded_unanswered_and_raised_and_released():
    async with _session(read_timeout_seconds=0.3) as (session, _):
        holder: list[tuple[str, bool]] = []
        token = tool_errors._failures.set(holder)
        try:
            with pytest.raises(Exception):  # noqa: B017 — the SDK's own timeout error
                await session.call_tool("slow", {})
        finally:
            tool_errors._failures.reset(token)
        assert ActivityRegistry.shared().busy() == []
    assert len(holder) == 1
    assert holder[0][0].startswith("calling slow failed:")
    assert holder[0][1] is False
