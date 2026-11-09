"""What every session of the pool shares, and how a session's own wiring is built from it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.message_dispatcher import MessageDispatcher
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.rpc_router import RpcRouter
from igni_core.backend.schemas_sessions import BackendLike
from igni_core.backend.session_pool import SessionRuntime
from igni_core.backend.session_stamping_transport import SessionStampingTransport
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.fragments import PromptFragments
from igni_core.core.prompts.loader import PromptLoader
from igni_core.core.tools.process_supervisor_locator import SupervisorLocator
from igni_core.extensions.registry import ExtensionRegistry
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.store import ExtensionStore


def _no_extensions() -> ExtensionRpc:
    return ExtensionRpc(ExtensionRegistry())


@dataclass(frozen=True, kw_only=True)
class PoolWiring:
    """The per-backend collaborators every runtime is wired to.

    A frozen dataclass rather than a Pydantic model for the reason
    :class:`~igni_core.backend.rpc_handlers.base.RpcHandlerContext` is
    one: the fields are runtime objects. There is one login flow per
    backend process, one extension namespace (a second session must not
    see a different set of extensions), one notification center, one
    table of what extensions have done to the prompts and one table of
    the anchors they fill; the root ``transport`` is stamped with each
    session's id on the way out.

    ``supervisors`` is the handle on the supervisor the backend
    process shares — the registry the watcher panel reads and the
    foreground slot the cancel path kills. Every session of the pool
    is wired to it, so one backend process shows one watcher panel.
    """

    transport: Any
    push_bridge: PushNotificationBridge
    login: LoginCoordinator
    supervisors: SupervisorLocator
    contributions: PromptContributions = field(default_factory=PromptContributions)
    prompt_loader: PromptLoader = field(default_factory=lambda: PromptLoader(PromptFragments()))
    extensions: ExtensionRpc = field(default_factory=_no_extensions)
    origins: Any = None
    extension_store: ExtensionStore | None = None
    notifications: Any = None

    def stamped(self, backend: BackendLike) -> SessionStampingTransport:
        """The root transport, stamping ``backend``'s session id on everything it sends."""
        return SessionStampingTransport(self.transport, backend)

    def router_for(self, backend: Any, bridge: PushNotificationBridge) -> RpcRouter:
        """A runtime's RPC router: its own backend and bridge, the shared rest."""
        return RpcRouter(
            backend=backend,
            login=self.login,
            push=bridge,
            extensions=self.extensions,
            origins=self.origins,
            extension_store=self.extension_store,
            notifications=self.notifications,
        )

    def dispatcher_for(self, runtime: SessionRuntime) -> MessageDispatcher:
        """A dispatcher over ``runtime``; it carries no state, so one per call is cheap."""
        return MessageDispatcher(
            backend=runtime.backend,
            transport=runtime.transport,
            rpc_table=runtime.rpc_table,
            queue=runtime.queue,
            login=self.login,
            extensions=self.extensions,
        )
