"""The session's own services — who it is, who it talks to, what it records.

The audit log, the display, the approval channel and the guardrails
are the session's outward faces: what it records, what it shows, how it
asks, and what it refuses. The cloud credentials and the identity make
it a session in someone's account rather than a local process, and the
message handler is the headless pipeline a turn runs through.
"""

from __future__ import annotations

import getpass
import uuid
from typing import TYPE_CHECKING

from igni_core.core.auth.credentials import CloudCredentials
from igni_core.core.guardrails.runner import GuardrailRunner
from igni_core.core.session.cloud_auth import SessionCloudAuth
from igni_core.core.session.identity import SessionIdentity
from igni_core.core.session.message_handler import MessageHandlerDeps, SessionMessageHandler
from igni_core.core.session.persistence.db_protocol import ScopedAgnoDb
from igni_core.core.session.tool_approval import ToolCallApprovalChannel
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.core.utils.audit import AuditLogger
from igni_core.core.utils.display import DisplayManager

if TYPE_CHECKING:
    from igni_core.core.session.core import Session


class RuntimeBuilder:
    """The session's outward faces, its identity and its message pipeline."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def build_runtime_services(self) -> None:
        """The audit log, the display, the approval channel and the guardrails."""
        session = self._session
        session.audit = AuditLogger(session.settings)
        session.display = DisplayManager()
        # Sub-agents spawned by the orchestrator pause inside the parent's
        # tool execution; this coordinator carries those pauses (and the
        # session's own single-call questions) to the person.
        session.sub_agent_hitl = SubAgentHITLCoordinator()
        session.approval = ToolCallApprovalChannel(session.sub_agent_hitl)
        session.guardrail_runner = GuardrailRunner(session.settings)

    def build_cloud_auth(self) -> None:
        """The cloud credentials, and the team rebuild they trigger.

        Composed before ``main_team`` so the first agent build can read
        the access token; every credential change rebuilds the team.
        """
        session = self._session
        session.cloud_auth = SessionCloudAuth(
            creds=CloudCredentials(session.settings.auth.credentials_file),
            server_url=session.settings.api_url,
            rebuild_team=session.rebuild_main_team,
            catalog=session.cloud_catalog,
        )

    def build_identity(self, resume_session_id: str | None) -> None:
        """The session's id, whether it was named, and whose it is."""
        session = self._session
        session.identity = SessionIdentity(
            session_id=resume_session_id or str(uuid.uuid4())[:8],
            session_named=bool(resume_session_id),
            # In the cloud, Agno's rows are told apart by ``user_id``,
            # which the scoped db fixes to the session's scope.
            user_id=self._scoped_user_id() or getpass.getuser(),
            # Both are read late: the team is built last, and the
            # persister after the identity that names its rows. The
            # lookups are what makes the late binding explicit — the
            # attributes are not there yet when this runs.
            main_team_ref=lambda: getattr(session, "main_team", None),
            persistence_ref=lambda: getattr(session, "persistence", None),
        )

    def _scoped_user_id(self) -> str | None:
        """The scope a shared database's Agno storage fixes ``user_id`` to.

        ``None`` on the desktop, whose storage has no scope of its own.
        """
        db = self._session.db
        if isinstance(db, ScopedAgnoDb):
            return db.scoped_user_id or None
        return None

    def build_message_handler(self) -> None:
        """The headless message pipeline a turn runs through.

        Built once and never rebuilt, so everything the session can change
        under it is handed over as a stable owner or a callable.
        """
        session = self._session
        session.message_handler = SessionMessageHandler(
            MessageHandlerDeps(
                hooks=session.hooks,
                audit=session.audit,
                display=session.display,
                guardrail_runner=session.guardrail_runner,
                team_ref=lambda: session.main_team,
                pending_reminders_drain=session.reminders.drain,
                compact_hook=session.compaction.compact_if_needed,
                ensure_mcp=session.startup.ensure_mcp,
                session_id_ref=lambda: session.session_id,
                budget=session.context_budget,
                # Replaced with the tool-hook factory on every plugin reload.
                permission_evaluator_ref=lambda: session.permission_evaluator,
            )
        )


__all__ = ["RuntimeBuilder"]
