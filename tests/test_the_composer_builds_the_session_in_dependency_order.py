"""The composer is the order the session's builders run in.

The order decides which builder may read what: the hooks are built before
the MCP manager exists, the identity before the persister that keys its
rows, the tooling before the one main team that sees it, and the message
handler last. A builder that fails stops the composition where it is.
The package's public surface is the composer and the deps it takes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.session import parts
from igni_core.core.session.core import Session
from igni_core.core.session.parts.agents import AgentsBuilder
from igni_core.core.session.parts.composer import SessionComposer
from igni_core.core.session.parts.coordinators import CoordinatorBuilder
from igni_core.core.session.parts.foundation import FoundationBuilder, SessionDeps
from igni_core.core.session.parts.mcp import McpBuilder
from igni_core.core.session.parts.persistence import PersistenceBuilder
from igni_core.core.session.parts.plugins import PluginBuilder
from igni_core.core.session.parts.project import ProjectBuilder
from igni_core.core.session.parts.runtime import RuntimeBuilder
from igni_core.core.session.parts.stores import StoresBuilder
from igni_core.core.session.parts.tooling import ToolingBuilder
from tests.session_factory import stub_project, stub_settings

#: Every builder step, as ``(class, method)``.
STEPS = [
    (FoundationBuilder, "build"),
    (StoresBuilder, "build_loop_state"),
    (StoresBuilder, "build_scratch"),
    (ProjectBuilder, "build_group_policy"),
    (ProjectBuilder, "run_initializer"),
    (PersistenceBuilder, "build_database"),
    (RuntimeBuilder, "build_runtime_services"),
    (PluginBuilder, "build_hooks"),
    (ProjectBuilder, "build_context"),
    (PluginBuilder, "build_plugins_and_styles"),
    (AgentsBuilder, "build_pools"),
    (RuntimeBuilder, "build_cloud_auth"),
    (RuntimeBuilder, "build_identity"),
    (McpBuilder, "build_mcp"),
    (ToolingBuilder, "build"),
    (PersistenceBuilder, "build_persistence"),
    (CoordinatorBuilder, "build"),
    (McpBuilder, "build_lifecycle"),
    (PluginBuilder, "build_reload_orchestrator"),
    (AgentsBuilder, "build_main_team"),
    (RuntimeBuilder, "build_message_handler"),
]


@pytest.fixture
def settings(tmp_path: Path):
    project = stub_project(tmp_path / "proj")
    return stub_settings(project, home=tmp_path / "home")


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every builder step that runs, in the order it ran, still doing its work."""
    ran: list[str] = []
    for cls, method in STEPS:
        original = getattr(cls, method)

        def recording(self, *args, _o=original, _n=f"{cls.__name__}.{method}", **kwargs):
            ran.append(_n)
            return _o(self, *args, **kwargs)

        monkeypatch.setattr(cls, method, recording)
    return ran


def test_the_builders_run_in_dependency_order(settings, tmp_path: Path, steps: list[str]) -> None:
    Session(settings=settings, project_dir=tmp_path / "proj")

    assert steps == [f"{cls.__name__}.{method}" for cls, method in STEPS]


def test_the_resumed_session_id_reaches_the_identity(settings, tmp_path: Path) -> None:
    session = Session(
        settings=settings, project_dir=tmp_path / "proj", resume_session_id="abc12345"
    )

    assert session.session_id == "abc12345"
    assert session.session_named is True


def test_a_builder_that_fails_stops_the_composition_there(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    later: list[str] = []

    def broken(self) -> None:
        raise RuntimeError("the tooling could not be built")

    original_persistence = PersistenceBuilder.build_persistence

    def persistence(self) -> None:
        later.append("persistence")
        original_persistence(self)

    monkeypatch.setattr(ToolingBuilder, "build", broken)
    monkeypatch.setattr(PersistenceBuilder, "build_persistence", persistence)

    with pytest.raises(RuntimeError, match="the tooling could not be built"):
        Session(settings=settings, project_dir=tmp_path / "proj")

    assert later == []


def test_the_package_exports_the_composer_and_the_deps_only() -> None:
    assert parts.__all__ == ["SessionComposer", "SessionDeps"]
    assert parts.SessionComposer is SessionComposer
    assert parts.SessionDeps is SessionDeps
    assert not hasattr(parts, "AgentsBuilder")
    assert not hasattr(parts, "FoundationBuilder")
