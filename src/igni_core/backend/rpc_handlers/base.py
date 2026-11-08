"""Base class + registration decorator for per-domain RPC handlers.

Every handler class subclasses :class:`RpcHandler` and tags each
method with :func:`rpc`, which stashes an :class:`RpcBinding` on the
function object. :meth:`RpcHandler.methods` walks the class and
returns one :data:`RpcCall` per binding.

The table is the wire edge: a frontend sends a JSON object
(:data:`WireArgs`) and gets back whatever
:meth:`~igni_core.backend.rpc_result.RpcResult.serialize` makes of the
handler's value. A handler that declares an arguments model,
``@rpc(RpcMethod.X, args=XArgs)``, never sees that object: its
:class:`RpcBinding` validates it into ``XArgs`` first, so the method
is typed ``(self, args: XArgs) -> Result`` and a malformed call fails
as a ``ValidationError`` before reaching it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from igni_core.backend.captured_shell_runner import CapturedShellRunner
from igni_core.backend.controller_registry import Controllers
from igni_core.backend.file_completion_service import FileCompletionService
from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.server import BackendServer
from igni_core.backend.workflow_runner import WorkflowRunner
from igni_core.extensions.origin import ExtensionOrigins
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.store import ExtensionStore
from igni_core.notifications.center import NotificationCenter
from igni_core.protocol.rpc import RpcMethod

#: The JSON object an RPC call carries on the wire, before any handler
#: has validated it.
WireArgs = dict[str, Any]

#: One dispatch-table entry: wire arguments in, the handler's value (or
#: its coroutine) out, for :class:`RpcResult` to serialize.
RpcCall = Callable[[WireArgs], Any]

# Attribute name the decorator uses to tag handler methods with their
# :class:`RpcBinding`. Private + underscored so a regular method name
# collision is impossible.
_RPC_BINDING_ATTR = "_igni_rpc_binding"

_F = TypeVar("_F", bound=Callable[..., Any])


@dataclass(frozen=True)
class RpcBinding:
    """Which :class:`RpcMethod` a handler method implements, and the
    model its wire arguments validate into (``None``: the method reads
    the raw :data:`WireArgs` itself)."""

    method: RpcMethod
    args: type[BaseModel] | None = None

    def call(self, bound: Callable[..., Any]) -> RpcCall:
        """The table entry for ``bound``, validating its arguments first
        when this binding has a model."""
        model = self.args
        if model is None:
            return bound
        return lambda wire: bound(model.model_validate(wire))

    @classmethod
    def of(cls, attr: object) -> RpcBinding | None:
        """The binding :func:`rpc` put on ``attr``, if any."""
        binding = getattr(attr, _RPC_BINDING_ATTR, None)
        return binding if isinstance(binding, cls) else None


def rpc(method: RpcMethod, *, args: type[BaseModel] | None = None) -> Callable[[_F], _F]:
    """Tag a handler method as the implementation of ``method``.

    With ``args``, the wire arguments are validated into that model and
    the method receives the model. The decorator returns the function
    unchanged; the tag lives as an attribute on it, which
    :meth:`RpcHandler.methods` reads to build the dispatch subtable."""

    binding = RpcBinding(method, args)

    def decorator(fn: _F) -> _F:
        setattr(fn, _RPC_BINDING_ATTR, binding)
        return fn

    return decorator


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
        table: dict[RpcMethod, RpcCall] = {}
        for name in dir(type(self)):
            if name.startswith("__"):
                continue
            binding = RpcBinding.of(getattr(type(self), name, None))
            if binding is None:
                continue
            if binding.method in table:
                raise RuntimeError(
                    f"duplicate @rpc({binding.method!r}) binding in {type(self).__name__}"
                )
            table[binding.method] = binding.call(getattr(self, name))
        return table
