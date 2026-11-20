"""Whether a message is handled at all, from the connection it came on.

Four checks, in order, before anything routes it:

* the connection's standing (``backend/standing_policy.py``): a program
  with the lockfile's token decides nothing for the person;
* ``caller``: a message saying it speaks for an extension is honoured only
  where the layer lets a connection do that, for an installed extension,
  and only for what that extension's page may call;
* the platform's calls (:data:`PLATFORM_RPCS`): a layer may take them from
  its control connection alone;
* the calls that open a native dialog (:data:`NATIVE_DIALOG_RPCS`): refused
  where the layer says igni has no screen anybody sees (the cloud's VM).
"""

from __future__ import annotations

from igni_core.backend import standing_policy
from igni_core.backend.rpc_handlers.pool_guard import PAGE_RPCS, PLATFORM_RPCS, POOL_LEVEL_RPCS
from igni_core.backend.schemas_pool import Refusal
from igni_core.core.layer import layers
from igni_core.extensions.registry import ExtensionRefused
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.protocol import messages as msg
from igni_core.protocol.messages import Message
from igni_core.protocol.rpc import RpcAccess, RpcMethod
from igni_core.protocol.schemas.rpc import RpcErrorCode

#: Calls that open one of the operating system's own dialogs on igni's
#: machine, and wait for the person to answer it.
NATIVE_DIALOG_RPCS: frozenset[RpcMethod] = frozenset({RpcMethod.PICK_DIR_NATIVE})


def access_of(method: RpcMethod) -> RpcAccess:
    """Who may call ``method``, as the sets this gate and the standing
    policy enforce: the narrowest that applies."""
    if method in PLATFORM_RPCS:
        return RpcAccess.PLATFORM
    if method in PAGE_RPCS:
        return RpcAccess.PAGE
    if method in standing_policy.WINDOW_ONLY_RPCS:
        return RpcAccess.WINDOW
    if method in standing_policy.APPROVAL_RPCS:
        return RpcAccess.APPROVER
    return RpcAccess.CLIENT


class MessageAdmission:
    """The security gate every message passes before the pool or a session hears it."""

    def __init__(self, extensions: ExtensionRpc) -> None:
        self._extensions = extensions

    @property
    def extensions(self) -> ExtensionRpc:
        """The extension namespace ``caller`` is authorised against.

        One per backend, not one per session: an extension is installed
        into a deployment, and a second session must not see a different
        set of them. Exposed for whoever installs into it.
        """
        return self._extensions

    def refusal(self, message: Message) -> Refusal | None:
        """Why ``message`` is refused, or ``None`` when it may be handled."""
        reason = (
            standing_policy.refusal(message)
            or self._caller_refusal(message)
            or self._platform_refusal(message)
        )
        if reason:
            return Refusal(reason=reason)
        dialog = self._native_dialog_refusal(message)
        return Refusal(reason=dialog, code=RpcErrorCode.UNAVAILABLE) if dialog else None

    def _caller_refusal(self, message: Message) -> str | None:
        """``caller`` is set by igni's window for the extension page it hosts
        (``IgniClient.asExtension``); it is a field of the message, so it is
        honoured only where the layer says a connection may speak for an
        extension (never in the cloud)."""
        if not isinstance(message, msg.RPCRequest) or not message.caller:
            return None
        if not layers.current().connections_speak_for_extensions():
            return f"{message.method}: no connection to this igni speaks for an extension"
        try:
            self._extensions.registry.authorise_caller(
                caller=message.caller,
                method=message.method,
                page_methods=PAGE_RPCS,
                pool_methods=POOL_LEVEL_RPCS,
            )
        except ExtensionRefused as exc:
            return f"{message.method}: {exc}"
        return None

    @staticmethod
    def _platform_refusal(message: Message) -> str | None:
        if not isinstance(message, msg.RPCRequest) or message.method not in PLATFORM_RPCS:
            return None
        if not layers.current().platform_calls_need_control() or message.via_control:
            return None
        return f"{message.method}: only on the platform's control connection"

    @staticmethod
    def _native_dialog_refusal(message: Message) -> str | None:
        """A dialog nobody would see is refused at once: the call would wait
        for an answer that never comes."""
        if not isinstance(message, msg.RPCRequest) or message.method not in NATIVE_DIALOG_RPCS:
            return None
        if layers.current().opens_native_dialogs():
            return None
        return (
            f"{message.method}: this igni runs where nobody sees its screen, "
            "so it cannot open a dialog; choose a folder another way"
        )
