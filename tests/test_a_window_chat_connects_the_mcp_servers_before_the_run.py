"""The window's chat path connects the MCP servers before the run.

Only the CLI's ``SessionMessageHandler`` called ``ensure_mcp``. The
backend's ``RunController`` — the path igni's window uses — never did,
so a chat in the window ran without the configured MCP tools until the
MCP page happened to call ``ensure_mcp`` itself (campaign R209/R669).

The connect rebuilds the team, so it has to happen before the team is
read: a run that read the team first would use the one without the MCP
tools.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from igni_core.backend.run_controller import RunController


class _Session:
    """A session stub that records the order of the two things that matter."""

    def __init__(self, order: list[str], ensure_error: Exception | None = None) -> None:
        self._order = order
        self._ensure_error = ensure_error
        self.project_dir = Path("/tmp")
        self.session_id = "s1"
        self.startup = SimpleNamespace(ensure_mcp=self._ensure_mcp)

    async def _ensure_mcp(self) -> None:
        self._order.append("mcp")
        if self._ensure_error is not None:
            raise self._ensure_error

    @property
    def main_team(self):
        self._order.append("team")
        return MagicMock()


def _controller(session: _Session) -> RunController:
    return RunController(
        session,  # type: ignore[arg-type]
        pending_store=MagicMock(),
        message_queue=None,
        stream_factory=MagicMock(),
        supervisor=MagicMock(),
        gate=SimpleNamespace(reason=""),
    )


async def test_the_run_connects_the_mcp_servers_before_it_reads_the_team() -> None:
    order: list[str] = []
    controller = _controller(_Session(order))

    with contextlib.suppress(Exception):
        await anext(controller._run_locked("hello", None))

    assert order == ["mcp", "team"]


async def test_a_failed_mcp_connect_does_not_end_the_chat() -> None:
    order: list[str] = []
    controller = _controller(_Session(order, ensure_error=RuntimeError("no mcp here")))

    with contextlib.suppress(Exception):
        await anext(controller._run_locked("hello", None))

    assert order == ["mcp", "team"], "the chat carries on without the MCP tools"


async def test_a_session_without_a_startup_phase_is_left_alone() -> None:
    session = SimpleNamespace(project_dir=Path("/tmp"), session_id="s1")
    controller = _controller(session)  # type: ignore[arg-type]

    await controller._ensure_mcp()  # type: ignore[attr-defined]


@pytest.mark.parametrize("error", [RuntimeError("boom"), ValueError("nope")])
async def test_a_broken_startup_does_not_raise_out_of_the_helper(error: Exception) -> None:
    order: list[str] = []
    controller = _controller(_Session(order, ensure_error=error))

    await controller._ensure_mcp()

    assert order == ["mcp"]
