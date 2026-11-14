"""What a session is given reaches it by identity; what is not given is made.

``SessionDeps`` is the one bundle of what the caller chose. The
foundation builder puts each of those on the session: the backend
hands every session the process-wide supervisor, prompt table, prompt
contributions and tool hints, and a session built on its own makes
private ones. The cloud catalog is refreshed before the project's
workspace is built, so nothing reads a model from an empty catalog.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from igni_core.core.mcp.hints import ToolHintsRegistry
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.fragments import PromptFragments
from igni_core.core.prompts.loader import PromptLoader
from igni_core.core.session.cloud_catalog import CloudModelCatalog
from igni_core.core.session.core import Session
from igni_core.core.session.parts import SessionDeps
from igni_core.core.session.parts import foundation as foundation_module
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from tests.session_factory import stub_project, stub_settings


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return stub_project(tmp_path / "proj")


@pytest.fixture
def settings(project: Path, tmp_path: Path):
    return stub_settings(project, home=tmp_path / "home")


def test_what_the_caller_gave_is_the_very_object_the_session_holds(
    project: Path, settings, tmp_path: Path
) -> None:
    supervisor = ProcessSupervisor()
    contributions = PromptContributions()
    loader = PromptLoader(PromptFragments())
    hints = ToolHintsRegistry()

    session = Session(
        settings=settings,
        project_dir=project,
        supervisor=supervisor,
        contributions=contributions,
        prompt_loader=loader,
        tool_hints=hints,
    )

    assert session.settings is settings
    assert session.supervisor is supervisor
    assert session.prompt_contributions is contributions
    assert session.prompt_loader is loader
    assert session.tool_hints is hints


def test_a_session_built_on_its_own_makes_private_ones(project: Path, settings) -> None:
    one = Session(settings=settings, project_dir=project)
    two = Session(settings=settings, project_dir=project)

    assert isinstance(one.supervisor, ProcessSupervisor)
    assert isinstance(one.prompt_contributions, PromptContributions)
    assert isinstance(one.prompt_loader, PromptLoader)
    # Nothing a session made for itself is shared with another.
    assert one.supervisor is not two.supervisor
    assert one.prompt_contributions is not two.prompt_contributions
    assert one.prompt_loader is not two.prompt_loader


def test_a_session_given_no_tool_hints_judges_by_the_shared_table(project: Path, settings) -> None:
    session = Session(settings=settings, project_dir=project)

    assert session.tool_hints is ToolHintsRegistry.shared()


def test_no_project_dir_means_the_directory_the_process_runs_in(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.setattr(Path, "cwd", lambda: here)

    session = Session(settings=settings)

    assert session.project_dir == here
    assert session.workspace.primary_dir == here.resolve()


def test_the_workspace_carries_the_additional_dirs(project: Path, settings, tmp_path: Path) -> None:
    extra = tmp_path / "extra"
    extra.mkdir()

    session = Session(settings=settings, project_dir=project, additional_dirs=[extra])

    assert session.workspace.additional_dirs == [extra.resolve()]
    assert session.workspace.all_dirs == [project.resolve(), extra.resolve()]


def test_an_empty_additional_dirs_list_is_a_single_directory_workspace(
    project: Path, settings
) -> None:
    session = Session(settings=settings, project_dir=project, additional_dirs=[])

    assert session.workspace.additional_dirs == []


def test_the_catalog_is_refreshed_before_the_workspace_is_built(
    project: Path, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    refresh = CloudModelCatalog.refresh
    workspace = foundation_module.WorkspaceManager

    def refreshing(self, *args, **kwargs):
        events.append("refresh")
        return refresh(self, *args, **kwargs)

    def workspace_built(*args, **kwargs):
        events.append("workspace")
        return workspace(*args, **kwargs)

    monkeypatch.setattr(CloudModelCatalog, "refresh", refreshing)
    monkeypatch.setattr(foundation_module, "WorkspaceManager", workspace_built)

    session = Session(settings=settings, project_dir=project)

    assert events[:2] == ["refresh", "workspace"]
    assert isinstance(session.cloud_catalog, CloudModelCatalog)


def test_the_deps_are_a_frozen_bundle_whose_extras_default_to_absent(settings) -> None:
    deps = SessionDeps(settings=settings)

    with pytest.raises(dataclasses.FrozenInstanceError):
        deps.project_dir = Path("/elsewhere")  # type: ignore[misc]
    assert deps.project_dir is None
    assert deps.resume_session_id is None
    assert deps.additional_dirs is None
    assert deps.contributions is None
    assert deps.prompt_loader is None
    assert deps.supervisor is None
    assert deps.tool_hints is None
