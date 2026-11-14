"""The coordinators are wired to the session, and read what it replaces on every use.

A plugin reload swaps the permission evaluator, compaction and an MCP
rebuild swap the main team, ``/clear`` rotates the session id, and a
workspace-trust change replaces the settings. The coordinators are built
once, so each of those is handed over as a callable: a coordinator that had
captured the value would go on acting on the one that was replaced.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.core.config.permission_eval import PermissionMode
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.session.core import Session
from igni_core.core.session.parts.plugins import PluginBuilder
from tests.session_factory import build_session, stub_project, stub_settings


@pytest.fixture
def session(tmp_path: Path):
    return build_session(tmp_path)


class _Evaluator:
    """An evaluator that records the mode it was put in."""

    mode = PermissionMode.DEFAULT

    def set_mode(self, mode: PermissionMode) -> None:
        self.mode = mode


def test_the_mode_coordinator_flips_the_evaluator_the_session_holds_now(session) -> None:
    replaced = _Evaluator()
    session.permission_evaluator = replaced

    session.mode.set_permission_mode("plan")

    assert replaced.mode is PermissionMode.PLAN


def test_the_mode_coordinator_patches_the_team_the_session_holds_now(
    session, tmp_path: Path
) -> None:
    styles = tmp_path / "proj" / CONFIG_DIR / "output-styles"
    styles.mkdir(parents=True)
    (styles / "terse.md").write_text("---\nname: terse\n---\n\nBe brief.\n")
    PluginBuilder(session).build_plugins_and_styles()
    replaced = SimpleNamespace(instructions=["base"])
    session.main_team = replaced

    session.mode.set_output_style("terse")

    assert replaced.instructions == ["base", "# Output style: terse\n\nBe brief."]


def test_the_mode_coordinator_starts_in_the_configured_mode(tmp_path: Path) -> None:
    project = stub_project(tmp_path / "proj")
    settings = stub_settings(project, home=tmp_path / "home")
    settings.permissions.mode = PermissionMode.ACCEPT_EDITS

    session = Session(settings=settings, project_dir=project)

    assert session.mode.starting_mode is PermissionMode.ACCEPT_EDITS


async def test_approving_a_plan_returns_the_session_to_its_starting_mode(session) -> None:
    session.mode.set_permission_mode("plan")

    result = await session.plan.approve("run-1")

    assert result.ok is True
    assert session.permission_evaluator.mode is session.mode.starting_mode
    assert session.plan_store.decisions["run-1"].value == "approved"


async def test_a_dismissed_plan_is_recorded_and_leaves_plan_mode_alone(session) -> None:
    session.mode.set_permission_mode("plan")

    result = await session.plan.dismiss("run-2")

    assert result.ok is True
    assert session.permission_evaluator.mode is PermissionMode.PLAN
    assert session.plan_store.decisions["run-2"].value == "dismissed"


async def test_compaction_reads_the_session_id_and_user_the_session_has_now(session) -> None:
    asked: list[tuple[str, str]] = []

    class Team:
        model = None
        session_id = ""

        async def aget_session(self, session_id: str, user_id: str):
            asked.append((session_id, user_id))

    session.main_team = Team()
    session.rotate_id("rotated1")

    result = await session.compaction.compact()

    assert asked == [("rotated1", session.user_id)]
    assert result.ok is False
    assert result.status == "Session not found in DB"


async def test_compaction_rebuilds_the_team_through_the_session(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Summary:
        summary = "what was said"

    class Stored:
        summary = Summary()
        runs = [1]

    class Team:
        model = None

        async def aget_session(self, session_id: str, user_id: str):
            return Stored()

    rebuilt: list[str] = []
    session.main_team = Team()
    monkeypatch.setattr(session, "rebuild_main_team", lambda: rebuilt.append("rebuilt"))
    monkeypatch.setattr(
        "igni_core.core.session.compaction.coordinator.RunHistory.save_for",
        _noop_save,
    )
    monkeypatch.setattr(
        "igni_core.core.session.compaction.coordinator.SessionSummaryManager",
        _KeepsSummary,
    )

    await session.compaction.compact()

    assert rebuilt == ["rebuilt"]


async def _noop_save(team, stored) -> None:
    return None


class _KeepsSummary:
    def __init__(self, model) -> None:
        pass

    async def acreate_session_summary(self, session) -> None:
        session.summary.summary = "a new summary"


def test_the_mcp_rebuild_builds_the_pool_it_has_now_and_asks_the_session_for_a_team(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[object] = []
    rebuilt: list[str] = []
    session.pool = SimpleNamespace(build_agents=lambda mcp_clients=None: built.append(mcp_clients))
    monkeypatch.setattr(session, "rebuild_main_team", lambda: rebuilt.append("rebuilt"))

    session.startup.rebuild_mcp()

    assert built == [None]
    assert rebuilt == ["rebuilt"]


async def test_the_marketplace_warmup_reads_the_settings_the_session_has_now(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Path] = []

    class Store:
        def __init__(self, data_dir: Path) -> None:
            opened.append(data_dir)

        def load(self):
            return SimpleNamespace(marketplaces=[])

    async def nothing(*_args) -> None:
        return None

    warmup = session.startup.marketplace
    monkeypatch.setattr(warmup, "_store_for", Store)
    monkeypatch.setattr(warmup, "_auto_register_defaults", nothing)
    elsewhere = tmp_path / "elsewhere"
    session.settings = session.settings.model_copy(deep=True)
    session.settings.storage.data_dir = str(elsewhere)

    await warmup._refresh_all()

    assert opened == [elsewhere]
