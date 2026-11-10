"""A booted backend serving its windows, and its teardown."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict

from igni_core.backend.backend_notifications import BackendNotifications
from igni_core.backend.extension_startup import ExtensionStartup
from igni_core.backend.extensions_runtime import ExtensionsRuntime
from igni_core.backend.first_window import FirstWindow, FirstWindowOutcome
from igni_core.backend.host_model_runtime import HostModelRuntime
from igni_core.backend.observed_transport import ObservedTransport
from igni_core.backend.service_event_ingress import ServiceEventIngress
from igni_core.backend.session_orchestrator import SessionOrchestrator
from igni_core.backend.supervisor import BackendSupervisor

logger = logging.getLogger(__name__)


class BackendParts(BaseModel):
    """Everything the boot built that serving starts and teardown stops."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    supervisor: BackendSupervisor
    orchestrator: SessionOrchestrator
    transport: ObservedTransport
    #: Wait for the first window without a timeout (a WebSocket backend).
    no_timeout: bool
    notifications: BackendNotifications
    ingress: ServiceEventIngress
    extensions: ExtensionsRuntime
    startup: ExtensionStartup
    host: HostModelRuntime


class ServingBackend:
    """Waits for a window, starts what needs one, serves, then tears down."""

    def __init__(self, parts: BackendParts) -> None:
        self._parts = parts

    async def serve(self) -> None:
        parts = self._parts
        try:
            window = FirstWindow(
                transport=parts.transport, supervisor=parts.supervisor, no_timeout=parts.no_timeout
            )
            if await window.arrives() is FirstWindowOutcome.SHUTDOWN:
                logger.info("Shutdown requested before any window connected")
                return
            await parts.startup.start()
            # After the client is connected, so the counts this pushes
            # have somebody to reach.
            parts.notifications.start()
            parts.ingress.start()
            # After the installs, so the first sweep and the first workspace
            # poll have something to ask about.
            parts.extensions.health.start()
            await parts.host.start_signals()
            logger.info("FE connected, processing messages")
            await parts.orchestrator.serve(parts.supervisor.shutdown_event)
        except Exception as exc:
            logger.error("Backend error: %s", exc, exc_info=True)
        finally:
            for stop in self._teardown():
                await stop()

    def _teardown(self) -> list[Callable[[], Awaitable[None]]]:
        """In order. A stop that raises skips the rest, as it always has."""
        parts = self._parts
        return [
            parts.ingress.stop,
            parts.notifications.stop,
            parts.host.stop,
            parts.extensions.health.stop,
            parts.startup.stop,
            parts.extensions.origins.stop_all,
            parts.supervisor.teardown,
        ]
