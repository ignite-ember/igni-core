"""The coordinators a session builds in ``__init__``, for a test session built without it.

A test that wants the mode commands, plan decisions or output styles
without plugins, a database and a main team builds the session with
``Session.__new__``, sets the collaborators it cares about, and then
calls :func:`wire_coordinators` — which builds the real
:class:`RuntimeModeCoordinator` and :class:`PlanCoordinator` from those
collaborators the way ``Session._init_coordinators`` does. Call it after
the collaborators are set: the coordinators are handed the objects, not
the session.

:func:`compaction_over` does the same for compaction, over a stub.
"""

from __future__ import annotations

from igni_core.core.session.compaction import CompactionCoordinator, TeamSessionSource
from igni_core.core.session.core import Session
from igni_core.core.session.output_style_selection import OutputStyleSelection
from igni_core.core.session.plan_ops import PlanCoordinator, PlanDeps
from igni_core.core.session.state_ops import RuntimeModeCoordinator, RuntimeModeDeps


def wire_coordinators(session: Session) -> Session:
    """Build ``session.mode`` and ``session.plan`` from what ``session`` carries.

    ``broadcast_bus`` is required. ``output_style`` defaults to an empty
    selection; ``plan_store``, ``persistence``, ``main_team`` and
    ``settings`` may be missing, as on a session that never had them.
    """
    if not hasattr(session, "output_style"):
        session.output_style = OutputStyleSelection()
    settings = getattr(session, "settings", None)
    session.mode = RuntimeModeCoordinator(
        RuntimeModeDeps(
            evaluator_ref=lambda: session.permission_evaluator,
            output_style=session.output_style,
            team_ref=lambda: getattr(session, "main_team", None),
            bus=session.broadcast_bus,
            permissions=settings.permissions if settings is not None else None,
        )
    )
    session.plan = PlanCoordinator(
        PlanDeps(
            plan_store=getattr(session, "plan_store", None),
            persistence=getattr(session, "persistence", None),
            bus=session.broadcast_bus,
            set_permission_mode=session.mode.set_permission_mode,
            starting_mode=lambda: session.mode.starting_mode,
        )
    )
    return session


def compaction_over(stub) -> CompactionCoordinator:
    """A :class:`CompactionCoordinator` over a session-shaped stub's
    ``main_team``, ``session_id``, ``user_id``, ``hook_executor`` and
    ``rebuild_main_team`` — each read when used, as on a session."""
    return CompactionCoordinator(
        TeamSessionSource(
            team_ref=lambda: stub.main_team,
            session_id_ref=lambda: stub.session_id,
            user_id_ref=lambda: stub.user_id,
        ),
        hook_executor_ref=lambda: stub.hook_executor,
        rebuild_team=lambda: stub.rebuild_main_team(),
    )
