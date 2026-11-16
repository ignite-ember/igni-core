"""The backend hands the project's record of what waits to the session list.

The session list says which sessions have a run waiting for the person
(campaign R647). That answer comes from the approval journal — the
project's record of what waits on the person — and the journal only
reaches the list if the backend hands it over: the bootstrap keeps it,
the server passes it on, and the controller registry wires it into the
sessions controller.

This file pins that hand-over at each seam, because a broken wire is
silent: the list still answers, it just never says a session is paused.

What we pin:

* the bootstrap keeps the project's journal;
* the server hands that journal to the controller registry;
* the registry wires it into the sessions controller, so the list marks
  the session that waits.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from igni_core.backend.backend_bootstrap import BackendBootstrap
from igni_core.backend.controller_registry import ControllerRegistry
from igni_core.backend.hitl_tracer import HITLTracer
from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.backend.server import BackendServer
from igni_core.core.approvals.journal import ApprovalJournal
from igni_core.core.approvals.records import ApprovalOrigin
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.config.settings import Settings
from igni_core.core.db.locator import StorageLocator
from igni_core.core.session.pending_messages import PendingMessageStore
from igni_core.core.session.schemas import SessionListRow
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.protocol import messages as msg

SESSION = "sess-1"


def _requirement(call_id: str = "call-1") -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_call_id=call_id,
            tool_name="run_shell_command",
            tool_args={"command": "echo hello"},
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


def _journal(settings: Settings, project: Path) -> ApprovalJournal:
    return ApprovalJournal(StorageLocator.from_settings(settings).project_database(project))


def _record(journal: ApprovalJournal, session_id: str = SESSION) -> None:
    journal.record(
        requirement_id="req-1",
        session_id=session_id,
        run_id="run-1",
        origin=ApprovalOrigin.MAIN,
        requirement=_requirement(),
        request=msg.HITLRequest(requirement_id="req-1", tool_name="run_shell_command"),
    )


def test_the_bootstrap_keeps_the_projects_record(settings: Settings, project: Path) -> None:
    """The bootstrap holds the journal the list will ask."""
    journal = _journal(settings, project)
    _record(journal)

    def session(*args, **kw):
        return SimpleNamespace(
            session_id=SESSION,
            project_dir=project,
            sub_agent_hitl=MagicMock(),
        )

    with patch("igni_core.backend.backend_bootstrap.Session", side_effect=session):
        boot = BackendBootstrap(settings, project_dir=project)

    assert isinstance(boot.journal, ApprovalJournal)
    assert boot.journal.waiting_sessions() == {SESSION}


def test_the_server_hands_its_record_to_the_controller_registry(
    settings: Settings, project: Path
) -> None:
    """The server passes the journal on, so the registry can wire it."""
    journal = _journal(settings, project)

    bootstrap = SimpleNamespace(
        session=SimpleNamespace(session_id=SESSION, project_dir=project),
        settings=settings,
        hitl_store=PendingRequirementsStore(),
        pending_store=PendingMessageStore(project / "pending.db"),
        session_prefs=MagicMock(),
        user_config_store=MagicMock(),
        hitl_tracer=HITLTracer(enabled=False),
        message_queue=None,
        journal=journal,
    )

    with (
        patch("igni_core.backend.server.BackendBootstrap", return_value=bootstrap),
        patch("igni_core.backend.server.ControllerRegistry") as registry,
    ):
        BackendServer(settings, project_dir=project)

    assert registry.build.call_args.kwargs["journal"] is journal


async def test_the_registry_wires_the_record_into_the_session_list(
    settings: Settings, project: Path
) -> None:
    """The list the person reads marks the session that waits."""
    journal = _journal(settings, project)
    _record(journal)

    session = MagicMock()
    session.cloud_auth.access_token = None
    session.persistence.list_sessions = AsyncMock(
        return_value=[SessionListRow(session_id=SESSION, name="One", run_count=1)]
    )

    controllers = ControllerRegistry.build(
        session=session,
        settings=settings,
        hitl_store=PendingRequirementsStore(),
        pending_store=PendingMessageStore(project / "pending.db"),
        session_prefs=MagicMock(),
        user_config_store=MagicMock(),
        hitl_tracer=HITLTracer(enabled=False),
        message_queue=None,
        supervisor=ProcessSupervisor(),
        gate=RunGate(),
        journal=journal,
    )

    listed = await controllers.sessions.list_sessions()

    assert [entry.has_paused_run for entry in listed.sessions] == [True]
