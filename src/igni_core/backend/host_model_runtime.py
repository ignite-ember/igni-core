"""The host object model over the pool, and the socket processes reach it on.

``docs/HOST_OBJECT_MODEL.md``. One model for every extension, reached by a
process over :class:`HostChannel` and by a page over the ``host_model`` RPC.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.backend.extensions_runtime import ExtensionsRuntime
from igni_core.backend.host_model_wiring import HostModelHubs, HostModelWiring
from igni_core.backend.observed_transport import ObservedTransport
from igni_core.backend.session_orchestrator import SessionOrchestrator
from igni_core.backend.workspace_signals import WorkspaceSignals
from igni_core.core.activity import ActivityRegistry
from igni_core.core.approvals.events import approval_events
from igni_core.core.hooks.extension_gate import extension_hook_gate
from igni_core.extensions.host.channel import HostChannel
from igni_core.extensions.host.model import HostModel
from igni_core.extensions.store import ExtensionStore
from igni_core.notifications.extension_notices import ExtensionNoticeReceiver


class HostModelRuntime:
    """The model, its channel, and the project's git state as workspace signals."""

    def __init__(
        self, *, model: HostModel, channel: HostChannel, signals: WorkspaceSignals
    ) -> None:
        self._model = model
        self._channel = channel
        self._signals = signals

    @classmethod
    def build(
        cls,
        *,
        orchestrator: SessionOrchestrator,
        transport: ObservedTransport,
        runtime: ExtensionsRuntime,
        project_dir: Path,
        store: ExtensionStore,
        notices: ExtensionNoticeReceiver,
    ) -> HostModelRuntime:
        # The workspace poll does no git work until some extension
        # entitled to the workspace is subscribed.
        signals = WorkspaceSignals(project_dir=project_dir)
        model = HostModelWiring.build(
            orchestrator=orchestrator,
            transport=transport,
            registry=runtime.registry,
            hooks=runtime.hooks,
            project_dir=project_dir,
            hubs=HostModelHubs(
                approvals=approval_events,
                activities=ActivityRegistry.shared(),
                install_hooks=extension_hook_gate.install,
            ),
            store=store,
            notices=notices,
            workspace_signals=signals,
        )
        return cls(model=model, channel=HostChannel(model, hooks=runtime.hooks), signals=signals)

    @property
    def model(self) -> HostModel:
        return self._model

    @property
    def channel(self) -> HostChannel:
        return self._channel

    async def start_signals(self) -> None:
        """After the installs: the first poll asks "is anyone listening"."""
        await self._signals.start()

    async def stop(self) -> None:
        await self._channel.stop()
        await self._signals.stop()
