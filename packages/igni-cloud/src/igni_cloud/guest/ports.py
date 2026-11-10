"""What the guest contract needs of the backend it serves, and nothing more.

:class:`~igni_cloud.guest.contract.CloudGuest` is handed the backend's
host model, transport, orchestrator and notification centre at
:meth:`~igni_cloud.guest.contract.CloudGuest.serving`. Each is typed here
by the few members the guest uses, so the contract states them instead
of probing for them, and a test hands it something that has just those.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from igni_cloud.guest.resume import Restored
from igni_core.backend.activity import Activity
from igni_core.backend.pool_rpcs.base import PoolRpc
from igni_core.extensions.host.activity import ActivityListener
from igni_core.protocol import messages as msg


class GuestTransport(Protocol):
    """The backend's root transport (:class:`~igni_core.backend.observed_transport.ObservedTransport`)."""

    async def send(self, message: msg.Message) -> None: ...

    def observe(self, observer: Callable[[msg.Message], None]) -> None:
        """Be shown every message on its way out."""
        ...

    @property
    def client_count(self) -> int: ...

    def watch_connections(self, watcher: Callable[[str, str], None]) -> None:
        """Be told ``("attached" | "detached", client_id)`` for every view."""
        ...


class ActivitySource(Protocol):
    def activity(self) -> Activity:
        """``app.activity``: running, waiting on a person, or idle."""
        ...


class ActivityListening(Protocol):
    def listen(self, listener: ActivityListener) -> None:
        """Be told the new ``app.activity`` whenever it changes."""
        ...


class HostServices(Protocol):
    @property
    def backend(self) -> ActivitySource: ...

    @property
    def activity_watch(self) -> ActivityListening: ...


class HostModelPort(Protocol):
    """The host object model (:class:`~igni_core.extensions.host.model.HostModel`)."""

    @property
    def services(self) -> HostServices: ...


class PoolRpcMount(Protocol):
    """Where pool-level RPCs are mounted (:class:`~igni_core.backend.session_orchestrator.SessionOrchestrator`)."""

    def mount(self, rpc: PoolRpc) -> None: ...


class ResumingNotifications(Protocol):
    """The notification centre (:class:`~igni_core.notifications.center.NotificationCenter`)."""

    async def resumed(self, restored: Restored, /) -> None:
        """Catch up after a restore, on a fresh connection."""
        ...


__all__ = [
    "ActivityListening",
    "ActivitySource",
    "GuestTransport",
    "HostModelPort",
    "HostServices",
    "PoolRpcMount",
    "ResumingNotifications",
]
