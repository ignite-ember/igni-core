"""Plugin monitor primitive — background processes scoped to the
session lifetime (Claude Code parity, row 33).

Plugins declare long-running helpers (file watchers, build daemons,
log tailers) via ``.monitors.json``; a session-level
:class:`MonitorManager` launches them at session start, holds them
in a rolling buffer, and shuts them down on session close. Agent-
facing tools live in ``core.tools.monitors``.
"""

from igni_core.core.monitors.config import MonitorConfig, load_monitor_config
from igni_core.core.monitors.handle import MonitorHandle
from igni_core.core.monitors.manager import MonitorManager
from igni_core.core.monitors.models import (
    MonitorControlResult,
    MonitorSnapshot,
    MonitorStatus,
    RestartDecision,
)
from igni_core.core.monitors.supervisor import (
    MonitorSupervisor,
    RestartPolicyEvaluator,
)

__all__ = [
    "MonitorConfig",
    "MonitorControlResult",
    "MonitorHandle",
    "MonitorManager",
    "MonitorSnapshot",
    "MonitorStatus",
    "MonitorSupervisor",
    "RestartDecision",
    "RestartPolicyEvaluator",
    "load_monitor_config",
]
