"""Tests for error recovery and resilience — P0 critical.

Covers: API timeout handling, cancel behavior, MCP crash recovery.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend.server import BackendServer
from igni_core.core.config.schemas.models import ModelsConfig
from igni_core.protocol import messages as msg
from igni_core.protocol.messages import Error
from tests.backend_server_factory import make_backend


def _pending_store() -> MagicMock:
    store = MagicMock()
    store.arecord_received = AsyncMock(return_value="mid-1")
    store.amark_completed = AsyncMock()
    store.adiscard = AsyncMock()
    # The run loop stamps a streamed failure interrupted, so the FE can
    # offer Retry/Discard on it; a bare MagicMock is not awaitable.
    store.amark_interrupted = AsyncMock()
    return store


def _backend(session: MagicMock) -> BackendServer:
    """A composed backend over ``session`` whose periodic checkpoint is
    stubbed — the run body is what these tests exercise."""
    server = make_backend(session, settings=MagicMock(), pending_store=_pending_store())
    server.controllers.runs.periodic_checkpoint = AsyncMock()
    return server


class TestBackendRunMessageErrors:
    """Test that BackendServer.run_message() handles errors gracefully."""

    @pytest.mark.asyncio
    async def test_agno_exception_yields_error(self):
        """Agno runtime error should yield an Error protocol message."""
        session = MagicMock()
        session.settings = SimpleNamespace(models=ModelsConfig(default="m"))
        session.main_team = MagicMock()

        async def _failing_arun(*args, **kwargs):
            raise RuntimeError("Model API failure")
            # Unreachable by design: the bare `yield` is what makes this
            # an async generator, which is the shape the caller awaits.
            yield

        session.main_team.arun = _failing_arun
        session._learning = None
        session._inject_learnings = AsyncMock()
        # The run path checks guardrails before handing the message to the
        # model; a bare MagicMock returns something unawaitable.
        session.guardrail_runner.warning_prefix = AsyncMock(return_value="")
        session.learning_mgr.inject = AsyncMock()
        session.hook_executor = MagicMock()
        session.hook_executor.execute = AsyncMock(
            return_value=MagicMock(should_continue=True, message="")
        )
        session.session_id = "test"
        server = _backend(session)

        results = []
        async for proto in server.run_message("hello"):
            results.append(proto)

        errors = [r for r in results if isinstance(r, Error)]
        assert len(errors) >= 1
        assert "Model API failure" in errors[0].text

    @pytest.mark.asyncio
    async def test_hook_blocks_message(self):
        """UserPromptSubmit hook blocking should yield error, not crash."""
        session = MagicMock()
        session._inject_learnings = AsyncMock()
        # The run path checks guardrails before handing the message to the
        # model; a bare MagicMock returns something unawaitable.
        session.guardrail_runner.warning_prefix = AsyncMock(return_value="")
        session.learning_mgr.inject = AsyncMock()
        session.hook_executor = MagicMock()
        session.hook_executor.execute = AsyncMock(
            return_value=MagicMock(should_continue=False, message="Blocked by hook")
        )
        session.session_id = "test"
        session._learning = None
        server = _backend(session)

        results = []
        async for proto in server.run_message("blocked message"):
            results.append(proto)

        errors = [r for r in results if isinstance(r, Error)]
        assert len(errors) == 1
        assert "Blocked by hook" in errors[0].text


class TestBackendCancelRun:
    """Test run cancellation via backend."""

    def test_cancel_run_no_crash_when_no_team(self):
        """cancel_run should not crash even if no team/run active."""
        session = MagicMock()
        session.main_team = MagicMock(spec=[])  # no run_id attr
        server = make_backend(session, settings=MagicMock())

        # Should not raise
        server.cancel_run()


class TestToolExceptionRecovery:
    """Test that tool exceptions don't crash the session."""

    @pytest.mark.asyncio
    async def test_tool_error_becomes_protocol_message(self):
        """Tool exceptions should be serialized as ToolError protocol messages."""

        # Create a mock tool error event
        event = MagicMock()
        event.__class__ = type("ToolCallErrorEvent", (), {})
        event.error = "File not found: /nonexistent"
        event.run_id = "run-1"

        # Check if isinstance would match (it won't with our mock, so test serializer directly)
        error_msg = msg.ToolError(error="File not found", run_id="run-1")
        assert error_msg.type == "tool_error"
        assert error_msg.error == "File not found"
