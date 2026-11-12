"""``BackendNotifications.build``: the center, the two MCP receivers, the sign-in hook.

Built from real collaborators, as the composition root builds it: an
extension's notice sent on its own MCP connection reaches the center and
moves the badge, a sign-in schedules the center's refresh, and a backend
with no MCP manager still builds.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.backend.backend_notifications import BackendNotifications
from igni_core.backend.extensions_runtime import ExtensionsRuntime
from igni_core.backend.igni_server_access import IgniServerAccess
from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.core.config.settings import Settings
from igni_core.core.mcp.notification_tap import McpNotificationRouter
from igni_core.core.prompts import PromptFragments, PromptLoader
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.extensions.contributions import ExtensionConditionReceiver
from igni_core.extensions.install_service import server_name
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.registry import ExtensionState
from igni_core.extensions.store import ExtensionStore
from igni_core.notifications.center import NotificationCenter
from igni_core.notifications.extension_notices import ExtensionNoticeReceiver


class _Push:
    def __init__(self) -> None:
        self.changes: list[Any] = []

    def notifications_changed(self, change: Any) -> None:
        self.changes.append(change)


class _Backend:
    pass


class _Rig:
    def __init__(self, tmp_path: Path, *, with_mcp: bool = True) -> None:
        self.router = McpNotificationRouter()
        self.mcp = SimpleNamespace(notifications=self.router) if with_mcp else None
        self.push = _Push()
        settings = Settings.model_validate(
            {
                "api_url": "",
                "auth": {"credentials_file": str(tmp_path / "credentials.json")},
                "storage": {"data_dir": str(tmp_path / "data")},
            }
        )
        self.runtime = ExtensionsRuntime.build(
            mcp=self.mcp,
            plugins_dir=tmp_path / "plugins",
            store=ExtensionStore(),
            prompts=PromptContributions(),
            prompt_loader=PromptLoader(PromptFragments()),
        )
        self.login = LoginCoordinator(backend=_Backend(), push_bridge=self.push)  # type: ignore[arg-type]
        self.notifications = BackendNotifications.build(
            settings=settings,
            access=IgniServerAccess(
                credentials_file=settings.auth.credentials_file, api_url=lambda: ""
            ),
            push_bridge=self.push,  # type: ignore[arg-type]
            runtime=self.runtime,
            mcp=self.mcp,
            login=self.login,
        )


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


def _install(rig: _Rig, extension_id: str) -> None:
    rig.runtime.registry.install(
        ExtensionManifest.model_validate(
            {
                "id": extension_id,
                "version": "1.0.0",
                "contract": ">=1.0 <2",
                "name": extension_id,
                "trust": "unsigned",
                "dev": {"command": ["python", "server.py"]},
                "contributes": {"pages": [{"kind": "acme.scan", "label": "scan"}]},
            }
        ),
        state=ExtensionState.READY,
    )


def test_both_extension_notification_methods_are_taken_off_the_connection(tmp_path):
    rig = _Rig(tmp_path)

    assert rig.router.handles(ExtensionNoticeReceiver.METHOD)
    assert rig.router.handles(ExtensionConditionReceiver.METHOD)


def test_an_extension_notice_on_its_own_connection_moves_the_badge(tmp_path):
    rig = _Rig(tmp_path)
    _install(rig, "acme.scanner")

    rig.router.deliver(
        server_name("acme.scanner"),
        ExtensionNoticeReceiver.METHOD,
        {
            "kind": "export_ready",
            "title": "Runbook.pdf",
            "event_key": "e1",
            "target": {"page": "acme.scan"},
        },
    )

    assert rig.notifications.center.counts().by_source == {"acme.scanner": 1}
    assert rig.push.changes[-1].counts.by_source == {"acme.scanner": 1}


def test_a_notice_from_an_extension_the_registry_does_not_know_is_not_kept(tmp_path):
    rig = _Rig(tmp_path)

    rig.router.deliver(
        server_name("acme.stranger"),
        ExtensionNoticeReceiver.METHOD,
        {"kind": "export_ready", "title": "x", "event_key": "e1"},
    )

    assert rig.notifications.center.counts().unread == 0


def test_without_an_mcp_manager_it_still_builds_with_its_center_and_receiver(tmp_path):
    rig = _Rig(tmp_path, with_mcp=False)

    assert rig.notifications.center.counts().unread == 0
    assert isinstance(rig.notifications.notices, ExtensionNoticeReceiver)


async def test_a_sign_in_schedules_the_centers_refresh_and_stop_ends_it(tmp_path, monkeypatch):
    scheduled: list[str] = []
    monkeypatch.setattr(
        NotificationCenter, "schedule_signed_in", lambda self: scheduled.append("signed in")
    )
    rig = _Rig(tmp_path)

    rig.login._notify_signed_in()

    assert scheduled == ["signed in"]
    await rig.notifications.stop()
