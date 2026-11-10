"""The server builds its per-run collaborators once, with itself.

:class:`BackendServer` used to build the slash-command router on
every command and the workspace-trust controller on every access to
it. Both are stateless between calls — the router discovers its
markdown commands per call, and the trust controller reads the
folder's decision per call — so building them once changes nothing
about what they answer, and it stops the server from building a fresh
object graph for work it has already done.

What we pin:

* the trust controller is one object for the server's life;
* the command router is built once and used for every command.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from igni_core.backend.command_result import CommandResult
from igni_core.protocol import messages as msg
from tests.backend_server_factory import make_backend


class TestTheTrustController:
    def test_the_same_controller_answers_every_time(self) -> None:
        """One controller for the server's life.

        Before, every read built a new one — the object the caller
        got was never the one the next caller got, so an answer given
        through it was not the same object being asked later.
        """
        server = make_backend(MagicMock(), settings=MagicMock())

        assert server.workspace_trust is server.workspace_trust

    def test_the_controller_is_built_with_the_server(self) -> None:
        """It is built in ``_adopt``, so it exists before anything asks."""
        server = make_backend(MagicMock(), settings=MagicMock())

        assert server.workspace_trust is not None


class TestTheCommandRouter:
    @pytest.mark.asyncio
    async def test_the_router_is_built_once_and_used_for_every_command(self) -> None:
        """One router, every command.

        The router is built with the server, so the patch has to be in
        place before the server is built — a patch applied afterwards
        would replace a name the server never reads again.
        """
        mock_result = CommandResult.info("test output")

        with patch("igni_core.backend.server.CommandHandler") as MockHandler:
            instance = MockHandler.return_value
            instance.handle = AsyncMock(return_value=mock_result)

            server = make_backend(MagicMock(), settings=MagicMock())

            first = await server.handle_command("/one")
            second = await server.handle_command("/two")

        assert MockHandler.call_count == 1, "the router was built more than once"
        assert instance.handle.call_count == 2
        assert instance.handle.await_args_list[0].args == ("/one",)
        assert instance.handle.await_args_list[1].args == ("/two",)
        assert isinstance(first, msg.CommandResult)
        assert isinstance(second, msg.CommandResult)
