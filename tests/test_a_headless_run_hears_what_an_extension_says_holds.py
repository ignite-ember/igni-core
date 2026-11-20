"""A headless run hears an extension say which of its conditions hold.

A prompt contribution can carry ``"when": "<condition>"``: CodeIndex's
overrides and edits wait on ``index-covers-head``, which only its process
can judge. It says so with ``notifications/igni/conditions`` on its own
MCP connection. The app's backend routes that notification to the prompt
table; ``igni -m`` and ``--pipe`` started the extension, took its prompt
contributions, and never routed the notification — the MCP SDK dropped it
as unknown, so a ``when`` contribution could never apply to a headless
run, however true its condition was (campaign R772: the index covered
HEAD, the extension said so, and the run's system prompt had none of
CodeIndex's sections).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.backend.headless_extensions import HeadlessExtensions
from igni_core.core.mcp.notification_tap import McpNotificationRouter
from igni_core.extensions.contributions import ExtensionConditionReceiver
from igni_core.extensions.install_service import SERVER_PREFIX
from tests.test_a_headless_run_starts_its_extensions import Mcp, Session, _install_guard


class RoutingMcp(Mcp):
    """The fake manager, with the router the real one carries."""

    def __init__(self) -> None:
        super().__init__()
        self.notifications = McpNotificationRouter()


async def test_the_conditions_an_extension_reports_reach_the_prompt_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")
    _install_guard(tmp_path)
    session = Session(tmp_path)
    session.mcp_manager = RoutingMcp()
    extensions = HeadlessExtensions(session)

    await extensions.start()
    try:
        assert session.mcp_manager.notifications.handles(ExtensionConditionReceiver.METHOD)
        session.mcp_manager.notifications.deliver(
            f"{SERVER_PREFIX}acme.guard",
            ExtensionConditionReceiver.METHOD,
            {"conditions": {"index-covers-head": True}},
        )
        owner = session.prompt_contributions._owners.get("acme.guard")
    finally:
        await extensions.stop()

    assert owner is not None and "index-covers-head" in owner.holding


async def test_a_manager_without_a_router_still_starts(tmp_path: Path) -> None:
    session = Session(tmp_path)
    extensions = HeadlessExtensions(session)

    await extensions.start()
    await extensions.stop()
