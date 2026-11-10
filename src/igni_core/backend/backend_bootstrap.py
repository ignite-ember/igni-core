"""Composition-root builder for :class:`BackendServer`.

Extracted from ``BackendServer.__init__`` — the previous body was
an imperative sequence of state-db lookups, session-preferences
reads, model-restore branches, ``Session`` construction, and
inline imports. Every one of those imports lived under a "late-
import to avoid cycle" comment; the fix is to move the *class
boundary*, not to hide the import.

:class:`BackendBootstrap` accepts the caller-supplied inputs — the
settings, the project directory, the session id to resume, the extra
directories, and the process-wide tables the extensions write into
(prompt contributions, prompt anchors, tool hints) — and exposes the
built collaborators as attributes:

* :attr:`session` — the :class:`Session`.
* :attr:`settings` — the ``Settings`` passed in.
* :attr:`hitl_store` — :class:`PendingRequirementsStore`, durable.
* :attr:`message_queue` — :class:`DurableMessageQueue`, the messages
  typed during a run.
* :attr:`hitl_tracer` — :class:`HITLTracer` built from settings.
* :attr:`pending_store` — :class:`PendingMessageStore`.
* :attr:`session_prefs` — :class:`SessionPreferencesStore`.
* :attr:`user_config_store` — :class:`UserConfigStore`.

``BackendServer.__init__`` shrinks to a one-liner + a controller-
registry build. ``BackendServer.from_parts`` (tests) skips this
file and takes already-built collaborators instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from igni_core.backend.hitl_tracer import HITLTracer
from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.core.approvals.journal import ApprovalJournal
from igni_core.core.config.user_config_store import UserConfigStore
from igni_core.core.db.locator import StorageLocator
from igni_core.core.mcp.hints import ToolHintsRegistry
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.loader import PromptLoader
from igni_core.core.session import Session
from igni_core.core.session.pending_messages import PendingMessageStore
from igni_core.core.session.queued_messages import DurableMessageQueue, QueuedMessageStore
from igni_core.core.session.session_preferences import SessionPreferencesStore
from igni_core.core.tools.process_supervisor import ProcessSupervisor

if TYPE_CHECKING:
    from igni_core.core.config.settings import Settings


class BackendBootstrap:
    """Builds every long-lived collaborator :class:`BackendServer`
    needs before the controller registry can fire.

    Instantiate once per production ``BackendServer`` — every
    attribute is a one-shot construction. ``BackendServer.from_parts``
    never touches this class.
    """

    def __init__(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        resume_session_id: str | None = None,
        additional_dirs: list[Path] | None = None,
        contributions: PromptContributions | None = None,
        prompt_loader: PromptLoader | None = None,
        supervisor: ProcessSupervisor | None = None,
        tool_hints: ToolHintsRegistry | None = None,
    ) -> None:
        resolved_project_dir = project_dir or Path.cwd()

        # Per-session prefs need to be consulted BEFORE the Session
        # builds its main team, since the team binds whatever model
        # is in ``settings.models.default`` at construction time.
        storage = StorageLocator.from_settings(settings)
        self.session_prefs = SessionPreferencesStore(
            storage.project_database(resolved_project_dir),
        )
        if resume_session_id:
            persisted_model = self.session_prefs.get_model(resume_session_id)
            if persisted_model and persisted_model in settings.models.registry:
                # The pool copies these settings for every session it
                # resumes, so a resumed session's model must not become
                # the default the others start on. Copy before changing
                # it: the caller's settings stay as they were given.
                settings = settings.model_copy(deep=True)
                settings.models.default = persisted_model

        self.session = Session(
            settings,
            project_dir=project_dir,
            resume_session_id=resume_session_id,
            additional_dirs=additional_dirs,
            contributions=contributions,
            prompt_loader=prompt_loader,
            supervisor=supervisor,
            tool_hints=tool_hints,
        )
        self.settings = settings

        project_db = storage.project_database(self.session.project_dir)
        session = self.session

        # HITL pause state lives on a dedicated store, not two raw
        # dicts. Both the pending-user bucket AND the evaluator-
        # auto-resolved bucket are behind one class so the "pending
        # XOR auto_resolved" invariant has one place to live. Both
        # it and the sub-agent coordinator write through to the
        # approval journal, and read back what a previous backend
        # left waiting — so a restart does not lose a question.
        journal = ApprovalJournal(project_db)
        self.hitl_store = PendingRequirementsStore(journal, session_id=lambda: session.session_id)
        self.hitl_store.restore(session.session_id)
        session.sub_agent_hitl.attach_journal(journal, session_id=lambda: session.session_id)
        session.sub_agent_hitl.restore(session.session_id)
        self.hitl_tracer = HITLTracer.from_settings(settings)

        # Messages typed while a run works, kept until a run takes them.
        self.message_queue = DurableMessageQueue(
            QueuedMessageStore(project_db), session_id=lambda: session.session_id
        )

        # Pre-persist user messages BEFORE handing them to Agno.
        self.pending_store = PendingMessageStore(project_db)

        # Injected into ModelSwitcher by the controller registry —
        # moved from an inline import in ``BackendServer.model_switcher``
        # to here so the module boundary carries the dependency.
        self.user_config_store = UserConfigStore()
