"""Tests for session/commands.py — slash command dispatch (delegates to shared CommandHandler)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from igni_core.backend.command_handler import CommandHandler
from igni_core.backend.command_result import UnknownCommandResult
from igni_core.core.session.commands import InteractiveCommandDispatcher
from igni_core.core.session.memory_ops import MemoryReadResult
from igni_core.core.session.schemas import SessionListRow


async def dispatch(session, command):
    """Test shim so existing tests keep their call shape."""
    return await InteractiveCommandDispatcher(session).dispatch(command)


def _make_session():
    """Create a mock Session with enough attributes for CommandHandler.

    ``session.display`` is a MagicMock — all ``print_*`` methods
    are auto-created attributes, so calls to ``display.print_info(...)``
    from inside ``_render_result`` silently succeed and expose
    ``call_args`` for assertions."""
    session = MagicMock()
    session._ensure_knowledge = AsyncMock()
    session.pool.list_agents.return_value = []
    session.pool.ephemeral.list_agents.return_value = []
    session.skill_pool.list_skills.return_value = []
    session.skill_pool.match_user_command.return_value = None
    session.hooks_map = {}
    session.session_id = "abc12345"
    session.settings.models.default = "test-model"
    session.settings.models.registry = {"test-model": MagicMock()}
    session.settings.permissions.file_write = "ask"
    session.settings.permissions.shell_execute = "ask"
    # ``data_dir``, not ``backend`` — ``StorageConfig`` has had no
    # ``backend`` field for a long time, and MagicMock answering the
    # attribute anyway is what kept ``/config`` green here while it
    # raised for every real user.
    session.settings.storage.data_dir = "/tmp/igni-test-data"
    session.settings.memory.enable_agentic_memory = False
    session.settings.learning.enabled = False
    session.settings.reasoning.enabled = False
    session.settings.guardrails.pii_detection = False
    session.settings.guardrails.prompt_injection = False
    session.settings.guardrails.moderation = False
    session.settings.orchestration.max_total_agents = 20
    session.settings.orchestration.max_nesting_depth = 3
    session.memory_mgr.get_memories = AsyncMock(return_value=MemoryReadResult.ok([]))
    session.mcp_manager.list_servers.return_value = []
    session.mcp_manager.list_connected.return_value = []
    session.code_index_sync.sync_now = AsyncMock()
    return session


class TestDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_known_command(self):
        session = _make_session()
        result = await dispatch(session, "/agents")
        assert result is True

    @pytest.mark.asyncio
    async def test_dispatch_unknown_command(self):
        session = _make_session()
        result = await dispatch(session, "/nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_an_unknown_command_is_answered_with_the_typed_result(self):
        """The REPL asks the RESULT whether the command was understood.

        It used to grep ``result.content`` for "Unknown command" — prose
        matching that breaks the day the message is reworded. The router
        now returns :class:`UnknownCommandResult`, and the dispatcher
        asks the result instead of the sentence.
        """
        session = _make_session()
        result = await CommandHandler(session).handle("/nonexistent")
        assert isinstance(result, UnknownCommandResult)
        assert result.is_unknown_command() is True
        assert await dispatch(session, "/nonexistent") is False

    @pytest.mark.asyncio
    async def test_dispatch_sessions_lists_typed_rows(self):
        """``/sessions`` reads the typed rows, not dict keys."""
        session = _make_session()
        session.persistence.list_sessions = AsyncMock(
            return_value=[
                SessionListRow(session_id="s1", name="My session"),
                SessionListRow(session_id="s2"),
            ]
        )
        result = await dispatch(session, "/sessions")
        assert result is True
        printed = session.display.print_markdown.call_args[0][0]
        assert "- My session" in printed
        assert "- s2" in printed

    @pytest.mark.asyncio
    async def test_dispatch_help(self):
        session = _make_session()
        result = await dispatch(session, "/help")
        assert result is True

    @pytest.mark.asyncio
    async def test_dispatch_help_with_topic(self):
        session = _make_session()
        result = await dispatch(session, "/help schedule")
        assert result is True
        printed = session.display.print_markdown.call_args[0][0]
        assert "Schedule" in printed

    @pytest.mark.asyncio
    async def test_dispatch_config(self):
        session = _make_session()
        from igni_core.core.auth.schemas import LoadCredentialsResult

        with patch(
            "igni_core.core.auth.credentials.CredentialsStore.load",
            return_value=LoadCredentialsResult(ok=False, reason="no_file"),
        ):
            result = await dispatch(session, "/config")
        assert result is True
        printed = session.display.print_markdown.call_args[0][0]
        assert "test-model" in printed

    @pytest.mark.asyncio
    async def test_dispatch_clear(self):
        session = _make_session()
        result = await dispatch(session, "/clear")
        assert result is True
        # /clear delegates rotation to Session.rotate_id (owner of
        # the three-attribute invariant); assert the delegation.
        session.rotate_id.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_mcp_action(self):
        session = _make_session()
        result = await dispatch(session, "/mcp")
        assert result is True
        printed = session.display.print_info.call_args[0][0]
        assert "No MCP servers configured" in printed

    @pytest.mark.asyncio
    async def test_dispatch_model_action(self):
        session = _make_session()
        result = await dispatch(session, "/model")
        assert result is True
        printed = session.display.print_markdown.call_args[0][0]
        assert "test-model" in printed

    @pytest.mark.asyncio
    async def test_dispatch_compact(self):
        session = _make_session()
        from igni_core.core.session.schemas import CompactResult

        session.compaction.force_compact = AsyncMock(
            return_value=CompactResult(
                ok=True,
                status="Context compacted.",
                summary="Summary of conversation",
            )
        )
        result = await dispatch(session, "/compact")
        assert result is True
        session.compaction.force_compact.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_bug(self):
        session = _make_session()
        with patch("webbrowser.open") as mock_open:
            result = await dispatch(session, "/bug")
        assert result is True
        mock_open.assert_called_once()
        assert "github" in mock_open.call_args[0][0]
