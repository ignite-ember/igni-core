"""One way for tests to get a composed :class:`BackendServer`.

Builds a real :class:`Controllers` bag through
:meth:`ControllerRegistry.build` over the session the test supplies and
hands it to :meth:`BackendServer.from_parts` — the same wiring
production uses, minus :class:`BackendBootstrap`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from igni_core.backend.controller_registry import ControllerRegistry
from igni_core.backend.hitl_tracer import HITLTracer
from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.backend.server import BackendServer
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.session.pending_messages import PendingMessageStore
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.process_supervisor_locator import SupervisorLocator


def make_backend(
    session: Any,
    *,
    settings: Any = None,
    hitl_store: PendingRequirementsStore | None = None,
    pending_store: PendingMessageStore | None = None,
    session_prefs: Any = None,
    message_queue: Any = None,
    user_config_store: Any = None,
    tmp_path: Path | None = None,
) -> BackendServer:
    """A :class:`BackendServer` composed over ``session``.

    Missing collaborators get real defaults where one is cheap: an empty
    :class:`PendingRequirementsStore`, a disabled :class:`HITLTracer`,
    and — when ``tmp_path`` is given — a :class:`PendingMessageStore`
    on it. ``settings`` falls back to the session's own.
    """
    if settings is None:
        settings = getattr(session, "settings", None)
    if isinstance(session, MagicMock) and isinstance(session.cloud_auth.access_token, MagicMock):
        # A stand-in session has no stored token: building the controllers
        # must not start a group-policy fetch with a mock one.
        session.cloud_auth.access_token = None
    if pending_store is None and tmp_path is not None:
        pending_store = PendingMessageStore(tmp_path / "pending.db")
    supervisor = _supervisor_for(session)
    gate = RunGate()
    controllers = ControllerRegistry.build(
        session=session,
        settings=settings,
        hitl_store=hitl_store if hitl_store is not None else PendingRequirementsStore(),
        pending_store=pending_store,  # type: ignore[arg-type]
        session_prefs=session_prefs if session_prefs is not None else MagicMock(),
        user_config_store=user_config_store if user_config_store is not None else MagicMock(),
        hitl_tracer=HITLTracer(enabled=False),
        message_queue=message_queue,
        supervisor=supervisor,
        gate=gate,
    )
    return BackendServer.from_parts(
        session=session,
        settings=settings,
        controllers=controllers,
        message_queue=message_queue,
        supervisors=SupervisorLocator(supervisor),
        gate=gate,
    )


def _supervisor_for(session: Any) -> ProcessSupervisor:
    """The supervisor this backend shares with ``session``.

    A real session carries the supervisor its backend process gave
    it — reuse that one, so the watcher panel and the session's shell
    toolkit see the same processes. A stand-in session (a mock) has
    none, so the backend gets a supervisor of its own.
    """
    candidate = getattr(session, "supervisor", None)
    if isinstance(candidate, ProcessSupervisor):
        return candidate
    return ProcessSupervisor()
