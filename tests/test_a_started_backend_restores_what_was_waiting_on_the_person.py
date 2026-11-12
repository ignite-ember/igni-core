"""``BackendBootstrap``: the stores a backend builds at start, and what they hold.

A restart must not lose a question: the approvals the previous backend
left waiting, the main agent's and a sub-agent's, are read back from the
journal into the stores the bootstrap builds for the session it resumes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from igni_core.backend.backend_bootstrap import BackendBootstrap
from igni_core.backend.pause_handler import PauseHandler
from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.core.approvals.journal import ApprovalJournal
from igni_core.core.config.settings import Settings
from igni_core.core.db.locator import StorageLocator
from igni_core.core.session.pending_messages import PendingMessageStore
from igni_core.core.session.queued_messages import DurableMessageQueue
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator

SESSION = "sess-1"


def _requirement(call_id: str, command: str) -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_call_id=call_id,
            tool_name="run_shell_command",
            tool_args={"command": command},
            requires_confirmation=True,
        )
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    settings = Settings()
    settings.storage.data_dir = str(tmp_path / "data")
    return settings


@pytest.fixture
def project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    return project


def _boot(settings: Settings, project: Path | None, **kwargs):
    """A bootstrap whose Session is a stand-in that carries a real sub-agent coordinator."""
    built: dict = {}

    def session(*args, **kw):
        built["settings"] = args[0]
        return SimpleNamespace(
            session_id=SESSION,
            project_dir=kw.get("project_dir") or project or Path.cwd(),
            sub_agent_hitl=SubAgentHITLCoordinator(),
        )

    with patch("igni_core.backend.backend_bootstrap.Session", side_effect=session):
        return BackendBootstrap(settings, project_dir=project, **kwargs), built


def test_the_approvals_a_previous_backend_left_waiting_are_asked_again(settings, project):
    journal = ApprovalJournal(StorageLocator.from_settings(settings).project_database(project))
    PauseHandler(
        evaluator=None, store=PendingRequirementsStore(journal, session_id=lambda: SESSION)
    ).handle(
        SimpleNamespace(run_id="run-1", active_requirements=[_requirement("c1", "make deploy")])
    )
    sub = SubAgentHITLCoordinator()
    sub.attach_journal(journal, session_id=lambda: SESSION)

    import asyncio

    sub_id = asyncio.run(
        sub.push_requirement(
            _requirement("c2", "rm -rf build"), run_id="sub-run", agent_path=["architect"]
        )
    )

    boot, _ = _boot(settings, project)

    (main,) = boot.hitl_store.waiting_messages()
    assert main.requirements[0].tool_args == {"command": "make deploy"}
    (sub_request,) = boot.session.sub_agent_hitl.waiting_requests()
    assert sub_request.requirements[0].requirement_id == sub_id


def test_a_fresh_start_has_nothing_waiting(settings, project):
    boot, _ = _boot(settings, project)

    assert boot.hitl_store.waiting_messages() == []
    assert boot.session.sub_agent_hitl.waiting_requests() == []


def test_the_queue_and_the_pending_store_are_the_projects(settings, project):
    boot, _ = _boot(settings, project)

    assert isinstance(boot.message_queue, DurableMessageQueue)
    assert isinstance(boot.pending_store, PendingMessageStore)
    assert boot.settings is settings


def test_no_project_dir_means_the_working_directory(settings, tmp_path, monkeypatch):
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    journal = ApprovalJournal(StorageLocator.from_settings(settings).project_database(Path.cwd()))
    PauseHandler(
        evaluator=None, store=PendingRequirementsStore(journal, session_id=lambda: SESSION)
    ).handle(SimpleNamespace(run_id="run-1", active_requirements=[_requirement("c1", "ls")]))

    boot, _ = _boot(settings, None)

    assert len(boot.hitl_store.waiting_messages()) == 1


def test_a_resumed_sessions_model_is_given_on_a_copy(settings, project):
    from igni_core.core.session.session_preferences import SessionPreferencesStore

    settings.models.default = "usual"
    settings.models.registry = {
        "usual": {"url": "x", "model_id": "x"},
        "its-own": {"url": "y", "model_id": "y"},
    }
    SessionPreferencesStore(
        StorageLocator.from_settings(settings).project_database(project)
    ).set_model(SESSION, "its-own")

    boot, built = _boot(settings, project, resume_session_id=SESSION)

    assert built["settings"].models.default == "its-own"
    assert boot.settings.models.default == "its-own"
    assert settings.models.default == "usual", "the pool copies the caller's settings"
    assert boot.settings is not settings


def test_no_project_dir_looks_up_a_resumed_sessions_model_in_the_working_directory(
    settings, tmp_path, monkeypatch
):
    from igni_core.core.session.session_preferences import SessionPreferencesStore

    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    settings.models.default = "usual"
    settings.models.registry = {
        "usual": {"url": "x", "model_id": "x"},
        "its-own": {"url": "y", "model_id": "y"},
    }
    SessionPreferencesStore(
        StorageLocator.from_settings(settings).project_database(Path.cwd())
    ).set_model(SESSION, "its-own")

    boot, _ = _boot(settings, None, resume_session_id=SESSION)

    assert boot.settings.models.default == "its-own"
