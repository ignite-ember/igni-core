"""The collaborators the host model's members and broadcasts share, composed once.

:class:`HostServices` is the record every :class:`~.members.Members`
class, the broadcasts and the session targets are built with, in place
of the whole :class:`~.model.HostModel` — so none of them reaches back
into the model that composes them. :meth:`HostServices.build` is the
composition root: it builds each collaborator with what it needs and
wires the windows' listeners.

It is wide (sixteen collaborators) on purpose for now: each members
class still takes the record rather than the two or three parts it
uses. Narrowing that is the next step.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SkipValidation

from igni_core.core.decisions import Laya
from igni_core.extensions.busy import ExtensionBusy
from igni_core.extensions.host.backend import HostBackend
from igni_core.extensions.host.backend_gateway import HostBackendGateway
from igni_core.extensions.host.broadcasts.activity import ActivityWatch
from igni_core.extensions.host.extension_directory import ExtensionDirectory
from igni_core.extensions.host.host_state import HostState
from igni_core.extensions.host.local_notices import LocalNotices
from igni_core.extensions.host.observations import HostObservations
from igni_core.extensions.host.pushes import WindowPushes
from igni_core.extensions.host.signal_values import Emitter
from igni_core.extensions.host.signals import SignalBus
from igni_core.extensions.host.spec import HostSchema
from igni_core.extensions.host.types import SchemaTypes
from igni_core.extensions.host.window_rpc import WindowModelRpc
from igni_core.extensions.host.window_signals import WindowSignals
from igni_core.extensions.host.windows import Windows
from igni_core.extensions.host.work import BackgroundWork
from igni_core.extensions.registry import ExtensionRegistry
from igni_core.extensions.store import ExtensionStore
from igni_core.extensions.web_home import ExtensionWebHome
from igni_core.notifications.extension_notices import ExtensionNoticeReceiver


class HostServices(BaseModel):
    """Everything a member, a broadcast translator or a session target reaches.

    Not frozen: a test brings its own ``decisions``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: The schema every request and signal is checked against.
    host_schema: HostSchema
    types: SchemaTypes
    backend: SkipValidation[HostBackend]
    gateway: HostBackendGateway
    #: What the host observed and the backend does not keep.
    state: HostState
    bus: SignalBus
    windows: Windows
    #: ``window_model``: a window reading or writing its own model.
    window_rpc: WindowModelRpc
    pushes: WindowPushes
    extensions: ExtensionDirectory
    notices: LocalNotices
    #: Laya, for ``host.decide``.
    decisions: SkipValidation[Laya]
    #: Extensions that said they are working (``self.busy``).
    busy: ExtensionBusy
    work: BackgroundWork
    #: ``app.activity``, told when it moves.
    activity_watch: ActivityWatch
    observations: HostObservations

    @classmethod
    def build(
        cls,
        *,
        backend: HostBackend,
        registry: ExtensionRegistry,
        store: ExtensionStore | None,
        notices: ExtensionNoticeReceiver | None,
        decisions: Laya | None,
        schema: HostSchema,
    ) -> HostServices:
        """
        Args:
            notices: For ``host.notifications.notify`` — the same check a
                notice arriving over MCP passes.
            decisions: The process's shared Laya unless a test brings its own.
        """
        types = SchemaTypes(schema)
        gateway = HostBackendGateway(backend)
        store = store if store is not None else ExtensionStore()
        extensions = ExtensionDirectory(registry, store, ExtensionWebHome(store), gateway)
        bus = SignalBus(schema, extensions.grants_for)
        emitter = Emitter(bus.emit)
        busy = ExtensionBusy()
        observations = HostObservations(emitter, busy)
        pushes = WindowPushes(backend.broadcast)
        windows = Windows(backend.client_state, types=types, push=pushes.window_changed)
        window_signals = WindowSignals(emitter, observations.session_switched)
        windows.on_change(window_signals.changed)
        windows.on_window(window_signals.window_moved)
        return cls(
            host_schema=schema,
            types=types,
            backend=backend,
            gateway=gateway,
            state=HostState(),
            bus=bus,
            windows=windows,
            window_rpc=WindowModelRpc(windows),
            pushes=pushes,
            extensions=extensions,
            notices=LocalNotices(notices),
            decisions=decisions if decisions is not None else Laya.shared(),
            busy=busy,
            work=BackgroundWork(),
            # Read through the backend each time, so a test can replace it.
            activity_watch=ActivityWatch(lambda: backend.activity(), bus.emit),
            observations=observations,
        )


__all__ = ["HostServices"]
