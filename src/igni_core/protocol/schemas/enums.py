"""Wire-contract enums for the protocol layer.

Every enum here replaces a previously free-string field on a
:class:`~igni_core.protocol.messages.Message` subclass. All are
``StrEnum`` so Pydantic emits the underlying string on the wire
— existing FE clients keying off literals like ``"markdown"`` /
``"quit"`` are unaffected, but producers gain autocomplete +
typo-checking.

The *ingest-side* enums — the ones parsed from a wire string or a
replayed event-log row — carry a ``UNKNOWN`` safety-valve member
plus a ``_missing_`` override that maps unrecognised strings to it
rather than raising ``ValueError``. That is
:class:`OrchestrationTaskStatus`, :class:`HITLAction`,
:class:`HITLChoice`, :class:`SchedulerEventType`,
:class:`PermissionModeName` and :class:`PushChannel` — critical for
forward-compat when a newer BE emits a value this client doesn't
recognise, and for surviving the persisted event log when replayed
after an upgrade.

:class:`CommandResultKind` and :class:`CommandAction` deliberately
do NOT: a ``CommandResult`` is built in-process by this backend's
own command handlers and never parsed back off the wire here, so an
unlisted value is a producer bug that should fail loudly at
construction, not degrade to ``UNKNOWN`` and render as blank text.

Behaviour lives on the enum itself (``is_terminal`` / ``is_active``
/ ``is_confirm`` / ``is_persistent``) so dispatch sites never
compare against magic strings — the invariant is one method call.
"""

from __future__ import annotations

from enum import StrEnum


class CommandResultKind(StrEnum):
    """How a client should show a command result's ``content``.

    * ``markdown``: render as Markdown (long-form text such as help).
    * ``info``: a single, quiet line (status, confirmations).
    * ``error``: a single line shown as an error.
    * ``action``: nothing to show; act on the result's ``action``
      instead (open a panel, a picker, ...).
    """

    MARKDOWN = "markdown"
    INFO = "info"
    ERROR = "error"
    ACTION = "action"


class CommandAction(StrEnum):
    """What a command result asks the client to do.

    * empty: nothing; just show ``content``.
    * ``quit``: close the client.
    * ``clear``: clear the conversation view.
    * ``fork``: the session was duplicated; ``content`` is the new session's id.
    * ``sessions``: show the saved sessions.
    * ``model``: show the model picker.
    * ``model_switched``: the model changed; refresh what shows it.
    * ``login`` / ``logout``: start signing in / out of igni cloud.
    * ``help``: show help.
    * ``mcp``, ``plugins``, ``extensions``, ``agents``, ``skills``,
      ``hooks``, ``loop``, ``schedule``: open the page that manages them.
    * ``watcher``: open the background-process panel.
    * ``compact``: compact the conversation.
    * ``ctx``: open the context-breakdown page.
    * ``programs``: open the approver-token page.
    * ``run_prompt``: send ``content`` to the agent as a prompt.
    """

    # Deliberately no ``UNKNOWN`` member and no ``_missing_`` override
    # (unlike the ingest-side enums): every ``CommandAction`` is stamped
    # in-process by this backend's own command handlers, so an unlisted
    # value is a producer typo and should raise at construction.

    NONE = ""  # default — no action, just render content
    QUIT = "quit"
    CLEAR = "clear"
    FORK = "fork"  # session was duplicated; ``content`` carries new id
    SESSIONS = "sessions"
    MODEL = "model"  # show picker
    MODEL_SWITCHED = "model_switched"  # direct switch happened — refresh bar
    LOGIN = "login"
    LOGOUT = "logout"
    HELP = "help"
    MCP = "mcp"
    PLUGINS = "plugins"
    EXTENSIONS = "extensions"
    AGENTS = "agents"
    SKILLS = "skills"
    HOOKS = "hooks"
    LOOP = "loop"
    SCHEDULE = "schedule"
    WATCHER = "watcher"  # opens the background-process panel
    COMPACT = "compact"
    CTX = "ctx"  # opens the context-breakdown page
    PROGRAMS = "programs"  # opens the approver-token page
    RUN_PROMPT = "run_prompt"


class OrchestrationTaskStatus(StrEnum):
    """Status of one task inside a team run.

    ``unknown`` stands for any status this version does not recognise.
    """

    # Named ``OrchestrationTaskStatus`` (not ``TaskStatus``) to avoid
    # colliding with ``core.scheduler.models.TaskStatus``.

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> OrchestrationTaskStatus:
        """Route unrecognised wire strings to ``UNKNOWN``.

        Prevents ``TaskSnapshot.from_agno`` (and any event-log
        replay) from raising when Agno emits a new status literal
        we haven't listed. The dispatcher's ``UNKNOWN`` branch is
        the safe forward-compat surface.
        """
        return cls.UNKNOWN

    def is_terminal(self) -> bool:
        """``True`` when this status means the task has stopped
        making progress (completed / failed / cancelled).

        Behaviour on the enum so dispatch sites don't repeat the
        ``status in {"completed", "failed", "cancelled"}``
        comparison.
        """
        return self in (self.COMPLETED, self.FAILED, self.CANCELLED)

    def is_active(self) -> bool:
        """``True`` when the task is still in flight (pending /
        running). Inverse of :meth:`is_terminal` modulo
        ``UNKNOWN`` (which is treated as neither — the safe
        default)."""
        return self in (self.PENDING, self.RUNNING)


class HITLAction(StrEnum):
    """The person's answer to an approval request: ``confirm`` runs the tool call, ``reject`` refuses it.

    ``unknown`` stands for any value this version does not recognise.
    """

    # Same string values as ``backend.schemas_hitl.HitlAction``, the
    # domain-internal twin.

    CONFIRM = "confirm"
    REJECT = "reject"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> HITLAction:
        return cls.UNKNOWN

    def is_confirm(self) -> bool:
        """``True`` when the user approved. Behaviour on the enum
        so callers don't repeat ``action == "confirm"``."""
        return self is self.CONFIRM


class HITLChoice(StrEnum):
    """How long the answer to an approval request stands.

    * ``once``: this call only; no rule is saved.
    * ``always``: save a rule allowing this exact call (same tool and arguments).
    * ``similar``: save a rule allowing calls like it.
    * ``deny``: save a rule refusing it.
    * ``unknown``: a value this version does not recognise.
    """

    ONCE = "once"
    ALWAYS = "always"
    SIMILAR = "similar"
    DENY = "deny"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> HITLChoice:
        return cls.UNKNOWN

    def is_persistent(self) -> bool:
        """``True`` when this choice should be saved as a
        permission rule.

        ``ONCE`` returns ``False``; everything else (always /
        similar / deny) is persistent. Callers use this instead
        of comparing against ``"once"``."""
        return self in (self.ALWAYS, self.SIMILAR, self.DENY)


class SchedulerEventType(StrEnum):
    """What happened to a scheduled task.

    * ``started``: it began.
    * ``completed``: it finished.
    * ``failed``: it failed. ``error`` means the same.
    * ``unknown``: a value this version does not recognise.
    """

    # ``ERROR`` is kept as an alias for the older ``"error"`` literal
    # some paths still emit on failure.

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    ERROR = "error"  # legacy alias for FAILED, still emitted by some paths
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> SchedulerEventType:
        return cls.UNKNOWN

    def is_terminal(self) -> bool:
        """``True`` when the event marks the task finished
        (completed or failed / error)."""
        return self in (self.COMPLETED, self.FAILED, self.ERROR)


class PermissionModeName(StrEnum):
    """When igni asks the person before running a tool.

    * ``default``: ask for anything not already allowed by a rule.
    * ``dontAsk``: never ask; refuse anything not already allowed.
    * ``acceptEdits``: allow file edits without asking.
    * ``bypassPermissions``: run everything without asking, unless a rule
      says to refuse or to ask.
    * ``plan``: plan only; source files are not edited.
    * ``unknown``: a value this version does not recognise.
    """

    # Mirror of ``core.config.permission_eval.PermissionMode``; the two
    # MUST stay in sync (tests/protocol/test_permission_mode_mirror).

    DEFAULT = "default"
    DONT_ASK = "dontAsk"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS_PERMISSIONS = "bypassPermissions"
    PLAN = "plan"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> PermissionModeName:
        return cls.UNKNOWN


class PushChannel(StrEnum):
    """Every channel a push notification is sent on; each has its own payload.

    ``unknown`` stands for any channel this version does not recognise.
    """

    # Each channel has one payload model (``backend.push_payloads.PUSH_PAYLOADS``).
    # A broadcast-bus event names its channel as a ``Literal``; a test
    # holds each one to a member here.

    SCHEDULER_STARTED = "scheduler_started"
    SCHEDULER_COMPLETED = "scheduler_completed"
    ORCHESTRATE_EVENT = "orchestrate_event"
    LOGIN_STATUS = "login_status"
    LOGIN_RESULT = "login_result"
    SESSION_NAMED = "session_named"
    PERMISSION_MODE_CHANGED = "permission_mode_changed"
    OUTPUT_STYLE_CHANGED = "output_style_changed"
    PLAN_SUBMITTED = "plan_submitted"
    FILE_EDITED = "file_edited"
    #: The floor's unread counts moved — igni server's notifications,
    #: an extension's local notice, or something marked read. Every
    #: badge reads this; the list is fetched again only by whoever is
    #: showing it.
    NOTIFICATIONS_CHANGED = "notifications_changed"
    PROCESS_STARTED = "process_started"
    PROCESS_LINE = "process_line"
    PROCESS_EXITED = "process_exited"
    BACKGROUND_PROCESS_DONE = "background_process_done"
    # Bridged from the session broadcast bus / workflow runner rather
    # than a ``push_*`` factory — which is exactly why they were
    # missing while the FE routed on every one of them.
    WORKFLOW_EVENT = "workflow_event"  # backend/workflow_run.py (WorkflowEventSink)
    TODOS_UPDATED = "todos_updated"  # core/tools/todo.py
    PLAN_DECIDED = "plan_decided"  # core/session/plan_ops.py
    HTTP_RETRY_ATTEMPT = "http_retry_attempt"  # core/tools/http_fetcher.py
    HTTP_RETRY_SUCCEEDED = "http_retry_succeeded"  # core/tools/http_fetcher.py
    VISUALIZATION_ACTION_DISPATCHED = (
        "visualization_action_dispatched"  # backend/visualization_action_bus.py
    )
    #: A host-model signal for an extension's page: the window posts
    #: it into that extension's frames. See
    #: :mod:`igni_core.extensions.host`.
    HOST_SIGNAL = "host_signal"
    #: A window's model changed — by the window itself, another
    #: window, or an extension through ``host.ui``. Every window gets
    #: it; the one it is about applies it. The payload is
    #: :class:`~igni_core.extensions.host.windows.WindowChange`.
    WINDOW_MODEL_CHANGED = "window_model_changed"
    #: An extension asked a window to open a link in the person's
    #: browser (``host.app.open_external``). Every window gets it; the
    #: one it names opens it. The payload is
    #: :class:`~igni_core.extensions.host.pushes.ExternalOpen`.
    OPEN_EXTERNAL = "open_external"
    #: The folder's workspace-trust status (``core/workspace_trust``): sent
    #: after someone answers the trust dialog, so every window showing it for
    #: that folder updates; a window opens the dialog while ``state`` is
    #: ``ask``. The payload is
    #: :class:`~igni_core.backend.workspace_trust_controller.WorkspaceTrustStatus`.
    WORKSPACE_TRUST_REQUIRED = "workspace_trust_required"
    #: A cloud VM's own credential changed state (``vm_sign_in``, its
    #: renewals): the layer's credential keeper pushes it. The payload is
    #: :class:`~igni_core.backend.schemas_pool.VmCredentialState`.
    VM_CREDENTIAL = "vm_credential"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> PushChannel:
        return cls.UNKNOWN


__all__ = [
    "CommandResultKind",
    "CommandAction",
    "OrchestrationTaskStatus",
    "HITLAction",
    "HITLChoice",
    "SchedulerEventType",
    "PermissionModeName",
    "PushChannel",
]
