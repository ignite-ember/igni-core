"""The host model over a session pool, told everything it listens to."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from igni_core.backend.activity_nudge import ActivityNudge
from igni_core.backend.host_backend import OrchestratorHostBackend
from igni_core.backend.observed_transport import ObservedTransport
from igni_core.backend.pool_rpcs import HostModelRpcs
from igni_core.backend.rpc_watch import RpcWatch
from igni_core.backend.session_orchestrator import SessionOrchestrator
from igni_core.backend.workspace_signals import WorkspaceSignals
from igni_core.core.activity import ActivityRegistry
from igni_core.core.approvals.events import ApprovalEvents
from igni_core.core.hooks.extension_gate import ExtensionHookAnswerer
from igni_core.extensions.host.hooks import ExtensionHooks
from igni_core.extensions.host.model import HostModel
from igni_core.extensions.host.model_config import HostModelConfig
from igni_core.extensions.registry import ExtensionRegistry
from igni_core.extensions.store import ExtensionStore
from igni_core.notifications.extension_notices import ExtensionNoticeReceiver


class HostModelHubs(BaseModel):
    """The process-wide hubs the host model listens to and answers through.

    Handed in by whoever builds the backend, so the wiring reaches no
    singleton of its own.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    #: Every approval that appears or goes away, wherever it was answered.
    approvals: ApprovalEvents
    #: Busy leases and wakes (``core/activity``).
    activities: ActivityRegistry
    #: Where igni's hook events find the extensions that hook them.
    install_hooks: Callable[[ExtensionHookAnswerer | None], None]


class HostModelWiring:
    """The host model over a session pool, told everything it listens to.

    One place for the wiring so the backend and its tests build the
    same thing: four taps — every outbound message, every window RPC
    (to signal writes), every registry change, and the workspace poll —
    and the window connections closing.
    """

    @staticmethod
    def build(
        *,
        orchestrator: SessionOrchestrator,
        transport: ObservedTransport,
        registry: ExtensionRegistry,
        hooks: ExtensionHooks,
        project_dir: Path,
        hubs: HostModelHubs,
        store: ExtensionStore | None = None,
        notices: ExtensionNoticeReceiver | None = None,
        workspace_signals: WorkspaceSignals | None = None,
        strict: bool = False,
    ) -> HostModel:
        backend = OrchestratorHostBackend(
            orchestrator=orchestrator,
            transport=transport,
            project_dir=project_dir,
            client_state=orchestrator.client_state,
            activities=hubs.activities,
        )
        model = HostModel.build(
            HostModelConfig(
                backend=backend, registry=registry, store=store, notices=notices, strict=strict
            )
        )
        orchestrator.mount(HostModelRpcs(model))
        # igni's hook events reach the extensions that hook them.
        hubs.install_hooks(hooks)
        transport.observe(model.broadcasts.outbound)
        watch = RpcWatch(model.broadcasts.after_rpc)
        orchestrator.watch_inbound(watch.inbound)
        transport.observe(watch.outbound)
        registry.watch(model.services.observations.extension_changed)
        # An app following approvals through the API is told of every
        # one that appears or goes away, wherever it was answered.
        hubs.approvals.watch(model.broadcasts.approval_changed)
        if workspace_signals is not None:
            workspace_signals.attach_model(model)
        # Busy leases and wakes move the activity too (``core/activity``).
        with contextlib.suppress(RuntimeError):
            hubs.activities.watch(ActivityNudge(model, asyncio.get_running_loop()))

        def closed(event: str, connection_id: str) -> None:
            if event == "detached":
                model.services.windows.registry.detached(connection_id)

        transport.watch_connections(closed)
        return model


__all__ = ["HostModelHubs", "HostModelWiring"]
