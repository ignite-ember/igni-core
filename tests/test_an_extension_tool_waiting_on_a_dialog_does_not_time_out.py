"""A tool call does not run out of time while its extension waits on the person.

igni-extension's README: ``show_dialog`` "waits for the person, with no
timeout". The tool call igni made into the extension had the MCP session's
read timeout (Agno's ten seconds), so a person who took longer to answer got
"Request 'tools/call' timed out" and their answer was lost (campaign R686).
While the host holds a wait on the person for the server, the call's
deadline is not counted.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp import ClientSession, types
from mcp.shared.exceptions import MCPError

from igni_core.core.mcp.person_waits import PersonWaits, person_waits
from igni_core.core.mcp.request_meta import RequestMetaSession

SERVER = "ext:acme.ship"


def _session(deadline: float) -> RequestMetaSession:
    session = RequestMetaSession.__new__(RequestMetaSession)
    session._request_meta = None
    session._busy_holder = f"mcp:{SERVER}"
    session._person_wait_key = SERVER
    session._deadline = deadline
    return session


@pytest.fixture
def slow_server(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The server answers after ``seconds[0]``; records the read timeout it was given."""
    seconds = [1.0]
    given: list[Any] = []

    async def call_tool(self, name, arguments=None, *, meta=None, **kwargs):
        given.append(kwargs.get("read_timeout_seconds"))
        await asyncio.sleep(seconds[0])
        return types.CallToolResult(content=[types.TextContent(type="text", text="shipped")])

    monkeypatch.setattr(ClientSession, "call_tool", call_tool)
    return seconds


async def test_a_call_waiting_on_the_person_outlives_its_deadline(slow_server) -> None:
    with person_waits.hold(SERVER):
        result = await _session(deadline=0.3).call_tool("ship", {})

    assert result.content[0].text == "shipped"


async def test_a_call_nobody_is_asked_about_still_times_out(slow_server) -> None:
    with pytest.raises(MCPError, match="timed out"):
        await _session(deadline=0.3).call_tool("ship", {})


async def test_a_quick_call_is_answered_as_before(slow_server) -> None:
    slow_server[0] = 0.0
    result = await _session(deadline=5).call_tool("ship", {})
    assert result.content[0].text == "shipped"


def test_a_wait_is_held_until_its_last_holder_lets_go() -> None:
    waits = PersonWaits()
    with waits.hold("s"):
        with waits.hold("s"):
            assert waits.waiting("s")
        assert waits.waiting("s")
    assert not waits.waiting("s")


async def test_a_dialog_opened_during_the_call_stops_its_clock(slow_server) -> None:
    """As a real tool does: the call starts, then it opens the dialog."""

    async def open_the_dialog() -> None:
        await asyncio.sleep(0.1)
        with person_waits.hold(SERVER):
            await asyncio.sleep(1.2)

    async def call() -> types.CallToolResult:
        return await _session(deadline=0.3).call_tool("ship", {})

    slow_server[0] = 1.0
    result, _ = await asyncio.gather(call(), open_the_dialog())

    assert result.content[0].text == "shipped"
