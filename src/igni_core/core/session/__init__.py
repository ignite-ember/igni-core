"""Session package — interactive conversation loop with full subsystem integration."""

from typing import TYPE_CHECKING, Any

from igni_core.core.session.cloud_auth import SessionCloudAuth
from igni_core.core.session.cloud_catalog import CloudModelCatalog
from igni_core.core.session.compaction import (
    CompactionCoordinator,
    ContextBreakdownReporter,
    FallbackSummariser,
)
from igni_core.core.session.core import Session
from igni_core.core.session.event_log import SessionEventLog
from igni_core.core.session.identity import SessionIdentity
from igni_core.core.session.interactive import run_session_interactive
from igni_core.core.session.learning_ops import SessionLearningManager
from igni_core.core.session.loop_ops import LoopController
from igni_core.core.session.mcp_ops import McpLifecycleCoordinator, McpLifecycleDeps
from igni_core.core.session.mcp_resolver import MCPToolResolver
from igni_core.core.session.memory_ops import SessionMemoryManager
from igni_core.core.session.message_handler import MessageHandlerDeps, SessionMessageHandler
from igni_core.core.session.message_outcome import (
    OutcomeKind,
    TurnFailure,
    TurnOutcome,
    TurnResult,
)
from igni_core.core.session.persistence import SessionPersistence
from igni_core.core.session.plan_ops import PlanCoordinator
from igni_core.core.session.plugin_reload import PluginReloadOrchestrator
from igni_core.core.session.reminders import PendingReminderQueue
from igni_core.core.session.run_debug import RunMessagesDebugDumper
from igni_core.core.session.schemas import (
    CompactResult,
    ContextBreakdown,
    InteractiveBanner,
    LoopAdvance,
    LoopPhase,
    McpClientBundle,
    McpInitResult,
    McpServerStatus,
    MessageMedia,
    OutputStyleChangedEvent,
    PermissionModeChangedEvent,
    PlanDecidedEvent,
    PlanDecisionResult,
    PluginReloadCounts,
    PostCompactHookPayload,
    PreCompactHookPayload,
    SessionLifecyclePayload,
    StopFailureHookPayload,
    StopHookPayload,
    UserPromptSubmitHookPayload,
)
from igni_core.core.session.session_run import SessionRun
from igni_core.core.session.startup import SessionStartupCoordinator
from igni_core.core.session.state_ops import (
    OutputStyleInstructionsPatcher,
    RuntimeModeCoordinator,
)
from igni_core.core.session.tool_hook_factory import ToolEventHookFactory

if TYPE_CHECKING:
    # Imported for type checkers only; at runtime these two are resolved
    # by ``__getattr__`` below. See the note on that function.
    from igni_core.core.session.runner import run_single_message
    from igni_core.core.session.single_message_run import SingleMessageRun


def __getattr__(name: str) -> Any:
    """Resolve the two exports that reach the extension host, on first use.

    ``single_message_run`` installs the headless extensions, and the
    extension host's channel reaches back into this package. Importing
    either of them when this package is first imported closes that
    loop: whoever imports the channel first gets a half-built module
    and an ``ImportError`` naming it.

    So they are exported on first use instead of at import time. The
    names are the same, ``__all__`` still lists them, and
    ``from igni_core.core.session import SingleMessageRun`` still works
    — it is only the moment of the import that moved.
    """
    if name == "SingleMessageRun":
        from igni_core.core.session.single_message_run import SingleMessageRun

        return SingleMessageRun
    if name == "run_single_message":
        from igni_core.core.session.runner import run_single_message

        return run_single_message
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CloudModelCatalog",
    "CompactResult",
    "CompactionCoordinator",
    "ContextBreakdown",
    "ContextBreakdownReporter",
    "FallbackSummariser",
    "InteractiveBanner",
    "LoopAdvance",
    "LoopController",
    "LoopPhase",
    "MCPToolResolver",
    "McpClientBundle",
    "McpInitResult",
    "McpLifecycleCoordinator",
    "McpLifecycleDeps",
    "McpServerStatus",
    "MessageHandlerDeps",
    "MessageMedia",
    "OutputStyleChangedEvent",
    "OutputStyleInstructionsPatcher",
    "OutcomeKind",
    "PendingReminderQueue",
    "PermissionModeChangedEvent",
    "PlanCoordinator",
    "PlanDecidedEvent",
    "PlanDecisionResult",
    "PluginReloadCounts",
    "PluginReloadOrchestrator",
    "PostCompactHookPayload",
    "PreCompactHookPayload",
    "RunMessagesDebugDumper",
    "RuntimeModeCoordinator",
    "Session",
    "SessionCloudAuth",
    "SessionEventLog",
    "SessionIdentity",
    "SessionLearningManager",
    "SessionLifecyclePayload",
    "SessionMemoryManager",
    "SessionMessageHandler",
    "SessionPersistence",
    "SessionRun",
    "SessionStartupCoordinator",
    "SingleMessageRun",
    "StopFailureHookPayload",
    "StopHookPayload",
    "ToolEventHookFactory",
    "TurnFailure",
    "TurnOutcome",
    "TurnResult",
    "UserPromptSubmitHookPayload",
    "run_session_interactive",
    "run_single_message",
]
