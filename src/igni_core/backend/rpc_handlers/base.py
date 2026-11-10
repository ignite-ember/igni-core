"""Base class for the per-domain RPC handlers.

Every handler class subclasses :class:`RpcHandler` and tags each
method with :func:`~igni_core.backend.rpc_binding.rpc`, which stashes an
:class:`~igni_core.backend.rpc_binding.RpcBinding` on the function
object. :meth:`RpcHandler.methods` walks the class and returns one
:data:`~igni_core.backend.rpc_binding.RpcCall` per binding.

The table is the wire edge: a frontend sends a JSON object and gets back
whatever :meth:`~igni_core.backend.rpc_result.RpcResult.serialize` makes
of the handler's value. A handler never sees that object: its binding
validates it into the arguments model first, so every method is typed
``(self, args: XArgs) -> Result`` and a malformed call fails as an
:class:`~igni_core.backend.rpc_binding.RpcArgsError` before reaching it.
"""

from __future__ import annotations

from dataclasses import dataclass

from igni_core.backend.captured_shell_runner import CapturedShellRunner
from igni_core.backend.controller_registry import Controllers
from igni_core.backend.file_completion_service import FileCompletionService
from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.rpc_binding import RpcBinding, RpcCall
from igni_core.backend.server import BackendServer
from igni_core.backend.workflow_runner import WorkflowRunner
from igni_core.extensions.origin import ExtensionOrigins
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.store import ExtensionStore
from igni_core.notifications.center import NotificationCenter
from igni_core.protocol.rpc import RpcMethod


@dataclass(frozen=True, kw_only=True)
class RpcHandlerContext:
    """Bundle of collaborators every handler subclass needs.

    A frozen dataclass rather than a Pydantic model because the fields
    are runtime objects (backend, coordinators, services). Passed to
    every subclass' constructor as a single ``ctx=`` arg so the
    constructor shape stays uniform; subclasses read only the fields
    they care about.
    """

    backend: BackendServer
    login: LoginCoordinator
    push: PushNotificationBridge
    # Panel-cluster services, one per router so each session's file
    # index is warm for its own project.
    file_completion: FileCompletionService
    shell_runner: CapturedShellRunner
    # The session's workflow runner, rooted at the backend's project
    # and pushing through the router's bridge.
    workflows: WorkflowRunner
    # The extension surfaces. ``extensions`` owns the registry every
    # dispatcher authorises against; ``origins`` knows which loopback
    # port each extension's UI is on; ``extension_store`` holds the
    # installed bundles and their settings. All optional: a headless
    # deployment installs no UI and a test builds none of them.
    extensions: ExtensionRpc | None = None
    origins: ExtensionOrigins | None = None
    extension_store: ExtensionStore | None = None
    # The floor's notification center. Process-level, like
    # ``extensions``: one person, one set of notifications, whichever
    # session asks.
    notifications: NotificationCenter | None = None


class RpcHandler:
    """Base class for per-domain RPC handlers.

    Subclasses declare methods tagged with :func:`rpc` and receive an
    :class:`RpcHandlerContext` in their constructor, stored as
    ``self._ctx`` so subclasses reach ``self._ctx.backend`` for the RPC
    target with no reach-back into a router or another handler.
    """

    def __init__(self, ctx: RpcHandlerContext) -> None:
        self._ctx = ctx

    @property
    def _controllers(self) -> Controllers:
        """The backend's controller bag, for RPCs with no facade method."""
        return self._ctx.backend.controllers

    def methods(self) -> dict[RpcMethod, RpcCall]:
        """Return ``{RpcMethod.X: call}`` for every ``@rpc``-tagged method.

        Reads the tags off the class (not the instance) so inherited
        tags come through the MRO.
        """
        return {
            method: binding.call(getattr(self, name))
            for method, (name, binding) in RpcBinding.on(type(self)).items()
        }
