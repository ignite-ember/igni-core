"""The coordinators — the session's behaviour, each in one class.

The mode coordinator owns the two flips (output style, permission
mode); the plan coordinator owns the plan decisions; the compaction
coordinator owns the context window; the startup coordinator owns the
background warmups and the first MCP connect.

Each is handed only what it reads. What the session replaces while it
runs (the main team, the evaluator, the pool, the settings, the id)
goes in as a callable read on every use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.core.session.compaction import (
    CompactionCoordinator,
    TeamSessionSource,
)
from igni_core.core.session.plan_ops import PlanCoordinator, PlanDeps
from igni_core.core.session.startup import (
    MarketplaceWarmupPhase,
    McpInitDeps,
    McpInitPhase,
    SessionStartupCoordinator,
)
from igni_core.core.session.state_ops import RuntimeModeCoordinator, RuntimeModeDeps

if TYPE_CHECKING:
    from igni_core.core.session.core import Session


class CoordinatorBuilder:
    """The coordinators — the session's behaviour, one class each."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def build(self) -> None:
        """The mode, plan, compaction and startup coordinators."""
        session = self._session
        session.mode = RuntimeModeCoordinator(
            RuntimeModeDeps(
                evaluator_ref=lambda: session.permission_evaluator,
                output_style=session.output_style,
                team_ref=lambda: session.main_team,
                bus=session.broadcast_bus,
                permissions=session.settings.permissions,
            )
        )
        session.plan = PlanCoordinator(
            PlanDeps(
                plan_store=session.plan_store,
                persistence=session.persistence,
                bus=session.broadcast_bus,
                set_permission_mode=session.mode.set_permission_mode,
                starting_mode=lambda: session.mode.starting_mode,
            )
        )
        session.compaction = CompactionCoordinator(
            TeamSessionSource(
                team_ref=lambda: session.main_team,
                session_id_ref=lambda: session.session_id,
                user_id_ref=lambda: session.user_id,
            ),
            hook_executor_ref=lambda: session.hook_executor,
            rebuild_team=lambda: session.rebuild_main_team(),
        )
        session.startup = SessionStartupCoordinator(
            MarketplaceWarmupPhase(settings_ref=lambda: session.settings),
            McpInitPhase(
                McpInitDeps(
                    mcp_manager=session.mcp_manager,
                    failures=session.mcp_failures,
                    display=session.display,
                    pool_ref=lambda: session.pool,
                    rebuild_team=lambda: session.rebuild_main_team(),
                )
            ),
        )


__all__ = ["CoordinatorBuilder"]
