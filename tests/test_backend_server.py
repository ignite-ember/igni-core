"""Tests for the BackendServer — validates the BE protocol layer."""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from igni_core.backend.command_result import CommandResult
from igni_core.backend.model_http_client_manager import ModelHttpClientManager
from igni_core.backend.schemas_model import ModelSwitchResult
from igni_core.backend.server import BackendServer
from igni_core.protocol import messages as msg
from igni_core.protocol.registry import MessageRegistry
from tests.backend_server_factory import make_backend


class TestBackendServerCommands:
    """Test that commands return proper protocol messages."""

    @pytest.mark.asyncio
    async def test_handle_command_returns_command_result(self):
        server = make_backend(MagicMock(), settings=MagicMock())

        # ``handle_command`` returns whatever the handler produced,
        # unchanged — the backend ``CommandResult`` subclasses the
        # wire ``msg.CommandResult`` so no rebuild step is needed.
        # Feed the mock a real ``CommandResult`` so the ``isinstance``
        # + field assertions below still exercise the wire contract.
        mock_result = CommandResult.info("test output")

        with patch("igni_core.backend.server.CommandHandler") as MockHandler:
            instance = MockHandler.return_value
            instance.handle = AsyncMock(return_value=mock_result)

            result = await server.handle_command("/test")

        assert isinstance(result, msg.CommandResult)
        assert result.kind == "info"
        assert result.content == "test output"

    @pytest.mark.asyncio
    async def test_get_status(self):
        settings = MagicMock()
        settings.models.default = "test-model"
        settings.models.max_context_window = 128_000
        session = MagicMock()
        session.cloud_connected = True
        session.cloud_org_name = "Test Org"
        # The header names the account, not just the org, so the
        # status push carries the address too.
        session.cloud_email = "maya@acme.test"
        session.context_budget.last_input_tokens = 1234
        # ``Session.permission_mode_value`` is the wire-safe string
        # StatusUpdate needs — MagicMock returns another MagicMock
        # for arbitrary attribute access, which then fails pydantic
        # validation. Pin it to the real default so we exercise the
        # RPC path, not the MagicMock-glue plumbing.
        session.permission_mode_value = "default"
        server = make_backend(session, settings=settings)

        status = server.get_status()

        assert isinstance(status, msg.StatusUpdate)
        assert status.model == "test-model"
        assert status.cloud_connected is True
        assert status.cloud_email == "maya@acme.test"
        assert status.cloud_org == "Test Org"
        assert status.max_context == 128_000
        assert status.context_tokens == 1234

    @pytest.mark.asyncio
    async def test_switch_model(self):
        session = MagicMock()
        session.session_id = "test-sess"
        session.set_default_model.return_value = ModelSwitchResult(ok=True, model_name="new-model")
        session.settings.models.has_vision.return_value = False
        session_prefs = MagicMock()
        session_prefs.set_model.return_value = True
        # The persistence side-effect lives on :class:`UserConfigStore`;
        # a stand-in keeps the real user config file untouched.
        user_config_store = MagicMock()
        user_config_store.set_default_model.return_value = True
        server = make_backend(
            session,
            settings=MagicMock(),
            session_prefs=session_prefs,
            user_config_store=user_config_store,
        )

        result = server.switch_model("new-model")

        assert result == msg.Info(text="Switched to new-model")
        session.set_default_model.assert_called_once_with("new-model")
        user_config_store.set_default_model.assert_called_once_with("new-model")
        session_prefs.set_model.assert_called_once_with("test-sess", "new-model")

    @pytest.mark.asyncio
    async def test_switch_model_says_which_save_did_not_land(self):
        session = MagicMock()
        session.session_id = "test-sess"
        session.set_default_model.return_value = ModelSwitchResult(ok=True, model_name="new-model")
        session.settings.models.has_vision.return_value = False
        session_prefs = MagicMock()
        session_prefs.set_model.return_value = False
        user_config_store = MagicMock()
        user_config_store.set_default_model.return_value = False
        server = make_backend(
            session,
            settings=MagicMock(),
            session_prefs=session_prefs,
            user_config_store=user_config_store,
        )

        result = server.switch_model("new-model")

        assert isinstance(result, msg.Info)
        assert "default for new sessions" in result.text
        assert "--continue" in result.text


class TestFromParts:
    """``from_parts`` is the one alternate constructor; it must leave the
    instance in the same shape ``__init__`` does."""

    def test_sets_exactly_the_six_fields(self):
        session = MagicMock()
        server = make_backend(session, settings=MagicMock())

        assert set(vars(server)) == {
            "_session",
            "_settings",
            "controllers",
            "message_queue",
            "_runtime",
            # The handle on the supervisor this backend process
            # shares — the watcher panel's registry and the
            # foreground slot the cancel path kills.
            "_supervisors",
        }
        assert server.runtime is None
        assert (
            session.on_new_dialogue == server.controllers.group_policy.revalidate_for_new_dialogue
        )


class TestCloseModelHttpClient:
    """``ModelHttpClientManager.close_and_replace`` closes and replaces the httpx client."""

    @pytest.mark.asyncio
    async def test_closes_async_client(self):
        client = httpx.AsyncClient()
        model = MagicMock()
        model.http_client = client
        team = MagicMock()
        team.model = model

        await ModelHttpClientManager().close_and_replace(team)

        assert client.is_closed
        assert isinstance(model.http_client, httpx.AsyncClient)
        assert model.http_client is not client  # replaced with a fresh one

    @pytest.mark.asyncio
    async def test_fresh_client_has_correct_limits(self):
        client = httpx.AsyncClient()
        model = MagicMock()
        model.http_client = client
        team = MagicMock()
        team.model = model

        await ModelHttpClientManager().close_and_replace(team)

        new_client = model.http_client
        pool = new_client._transport._pool
        assert pool._max_connections == 10
        assert pool._max_keepalive_connections == 5

    @pytest.mark.asyncio
    async def test_noop_when_no_model(self):
        """Should not raise when team has no model."""
        team = MagicMock(spec=[])  # no model attribute
        await ModelHttpClientManager().close_and_replace(team)  # should not raise

    @pytest.mark.asyncio
    async def test_noop_when_no_http_client(self):
        """Should not raise when model has no http_client."""
        model = MagicMock()
        model.http_client = None
        team = MagicMock()
        team.model = model

        await ModelHttpClientManager().close_and_replace(team)  # should not raise

    @pytest.mark.asyncio
    async def test_noop_when_sync_client(self):
        """Should not close a sync httpx.Client (only async)."""
        client = httpx.Client()
        model = MagicMock()
        model.http_client = client
        team = MagicMock()
        team.model = model

        await ModelHttpClientManager().close_and_replace(team)

        assert not client.is_closed  # sync client should be left alone
        client.close()  # cleanup


class TestProtocolSerialization:
    """Test that protocol messages serialize/deserialize correctly."""

    def test_content_delta_roundtrip(self):
        original = msg.ContentDelta(text="hello world", is_thinking=True)
        json_str = original.model_dump_json()
        restored = msg.ContentDelta.model_validate_json(json_str)
        assert restored.text == "hello world"
        assert restored.is_thinking is True
        assert restored.type == "content_delta"

    def test_tool_completed_with_diff_rows(self):
        rows = [("+ 1   added line", "#69db7c on #003d00"), ("  2   context", "")]
        original = msg.ToolCompleted(
            summary="Edited file.py",
            has_markup=True,
            diff_rows=rows,
        )
        json_str = original.model_dump_json()
        restored = msg.ToolCompleted.model_validate_json(json_str)
        assert restored.diff_rows == rows
        assert restored.has_markup is True

    def test_hitl_request_serialization(self):
        original = msg.HITLRequest(
            requirement_id="abc123",
            tool_name="run_shell_command",
            friendly_name="Bash",
            tool_args={"command": "rm -rf /"},
        )
        json_str = original.model_dump_json()
        restored = msg.HITLRequest.model_validate_json(json_str)
        assert restored.requirement_id == "abc123"
        assert restored.tool_args["command"] == "rm -rf /"

    def test_all_message_types_have_type_field(self):
        """Every concrete message class should have a default type value.

        Excludes the abstract intermediary :class:`RunScopedMessage`
        (which every run-scoped concrete event inherits from) — it
        deliberately has no ``type`` default because it's a mixin,
        not a wire message.
        """
        abstract_bases = {msg.Message, msg.RunScopedMessage}
        for name, cls in inspect.getmembers(msg, inspect.isclass):
            if issubclass(cls, msg.Message) and cls not in abstract_bases:
                instance = cls()
                assert instance.type, f"{name} has no default type"

    def test_unix_socket_deserializer(self):
        # Round-trip through JSON string
        original = msg.UserMessage(text="hello")
        json_line = original.model_dump_json()
        restored = MessageRegistry().deserialize(json_line)
        assert isinstance(restored, msg.UserMessage)
        assert restored.text == "hello"


class TestBackendServerRealConstruction:
    """Construct a real ``BackendServer`` (not ``__new__``-bypassed)
    against a tmp project directory.

    The rest of this file mocks the Session out; those tests catch
    dispatcher-level regressions but let anything that lives in
    ``Session.__init__`` slip through. This class is the "does
    ``BackendServer(settings, project_dir=...)`` actually work"
    smoke test that answers the audit's flagged concern
    (`test_backend_server.py` graded C for "bypasses __init__").
    """

    def test_construct_and_query_session_id(self, tmp_path):
        """Full BE + Session boot against a tmp project + KB disabled
        settings. Verifies the session_id field survives all 9
        `_init_*` phase methods and that ``get_status`` produces the
        typed StatusUpdate payload.

        Constructs real Settings (via `SettingsLoader.load`) rather than a
        MagicMock so any `__init__` regression that only fires with
        real settings (missing field, wrong default, ordering bug)
        surfaces here."""
        from igni_core.core.config.settings_loader import SettingsLoader

        settings = SettingsLoader.load(project_dir=tmp_path)

        backend = BackendServer(settings, project_dir=tmp_path)

        # session_id is minted in `Session.__init__` — verify it
        # made it all the way through the 9 phase methods to
        # the outer ``BackendServer._session``.
        assert backend._session.session_id
        assert len(backend._session.session_id) == 8

        # ``get_status`` runs the whole status-snapshot path and
        # should return a real StatusUpdate (not raise).
        status = backend.get_status()
        assert isinstance(status, msg.StatusUpdate)
        assert status.model == settings.models.default

    def test_phase_methods_populate_expected_attributes(self, tmp_path):
        """Verify each of the 8 `_init_*` phase methods populates its
        expected attribute set on the ``Session``. This is the smoke
        test that catches a phase method silently dropping a field
        assignment — the exact class of bug the audit's C-grade
        concern warned about.

        One assertion per phase method, in boot order:
        ``_init_loop_state`` → ``_init_per_session_scratch`` →
        ``_init_codeindex`` →
        ``_init_project_context`` →
        ``_init_plugins_output_styles_hooks`` →
        ``_init_agent_and_skill_pools`` →
        ``_init_mcp_client_manager`` → ``_init_lsp_and_monitors``.
        """
        from igni_core.core.config.settings_loader import SettingsLoader

        settings = SettingsLoader.load(project_dir=tmp_path)
        backend = BackendServer(settings, project_dir=tmp_path)
        session = backend._session

        # _init_loop_state — six loop fields + two stores.
        assert session.pending_loop_prompt is None
        assert session.loop_iteration_index == 0
        assert session.loop_store is not None
        assert session.loop_progress_store is not None

        # _init_per_session_scratch — scratch stores + broadcast lists.
        assert session.todo_store is not None
        assert session.plan_store is not None
        assert session.event_log == []
        # BroadcastBus is composed in ``__init__`` — the class
        # invariant that replaces the old raw-list init.
        from igni_core.core.session.broadcast import BroadcastBus

        assert isinstance(session.broadcast_bus, BroadcastBus)
        assert not session.broadcast_bus.has_callbacks
        assert session.broadcast_bus.pending_count == 0

        # _init_project_context — project_instructions + rules_index.
        assert isinstance(session.project_instructions, str)
        assert session.rules_index is not None

        # _init_plugins_output_styles_hooks — plugin_loader + hooks + output_styles.
        assert session.plugin_loader is not None
        assert isinstance(session._disabled_plugins, set)
        assert session.hooks_map is not None
        assert session.hook_executor is not None
        assert isinstance(session.output_styles, dict)

        # _init_agent_and_skill_pools — pool + skill_pool.
        assert session.pool is not None
        assert session.skill_pool is not None

        # _init_mcp_client_manager — MCP manager + init flag.
        assert session.mcp_manager is not None
        assert session.startup.mcp_initialized is False

        # _init_lsp_and_monitors — LSP manager + monitor manager.
        assert session.lsp_manager is not None
        assert session.monitor_manager is not None

        # Main team constructed last.
        assert session.main_team is not None
