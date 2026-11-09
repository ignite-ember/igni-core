"""``window_model`` and ``host_model``: the host object model, as windows and pages reach it."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import Field, JsonValue

from igni_core.backend.pool_rpcs.base import PoolRefusal, PoolRpc, UnavailableRpc
from igni_core.backend.rpc_binding import RpcArgs, RpcArgsError, RpcBinding, rpc
from igni_core.backend.rpc_streams import RUN_STREAM
from igni_core.backend.schemas_pool import PoolAnswer
from igni_core.extensions.host.page_channel import PageRequest
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.window_rpc import WindowReply
from igni_core.extensions.host.windows import WindowModelError, WindowRequest, WindowStateError
from igni_core.extensions.host.wire import ErrorBody, Reply
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel


class WindowRpc(Protocol):
    def handle(self, request: WindowRequest, /) -> WindowReply: ...


class FramingWindows(Protocol):
    def by_connection(self, connection_id: str, /) -> str | None: ...


class PageHostServices(Protocol):
    @property
    def window_rpc(self) -> WindowRpc: ...

    @property
    def windows(self) -> FramingWindows: ...


class PageHostModel(Protocol):
    """What these calls need of :class:`~igni_core.extensions.host.model.HostModel`."""

    @property
    def services(self) -> PageHostServices: ...

    async def handle_page(self, extension_id: str, args: dict[str, Any], /) -> Reply: ...


class WindowModelArgs(RpcArgs):
    """``window_model``: one window reading or writing its own model."""

    #: ``hydrate``: read the whole model, and say the window is open.
    #: ``write``: set ``field`` to ``value``. ``toast`` / ``dismiss_toast``:
    #: show the toast ``value``, or remove the toast whose id is ``value``.
    #: ``open_dialog``: show igni's own dialog, ``value`` its spec.
    #: ``answer_dialog``: close dialog ``dialog_id`` with the answer ``value``
    #: (``null``: dismissed).
    op: Literal["hydrate", "write", "toast", "dismiss_toast", "open_dialog", "answer_dialog"]
    #: The window's id, chosen by the window and stable across its reloads.
    window: str = Field(min_length=1)
    #: The field a ``write`` sets.
    field: str = ""
    #: What the op writes, shows or answers with; its shape is the field's.
    value: JsonValue = None
    #: The writer's own name for this change, echoed on ``window_model_changed``.
    token: str = ""
    #: The dialog an ``answer_dialog`` closes.
    dialog_id: str = ""
    #: What kind of window this is, recorded when it opens.
    kind: str = ""
    #: The window's connection, for a request from a transport without
    #: connections (the terminal's socket, an in-process client). Over a
    #: WebSocket it is the connection the request came on, whatever this says.
    connection_id: str = ""


class HostModelRpcs(PoolRpc):
    """Carries a window's or a page's request to the host model, and its answer back."""

    JSON_DUMPED = frozenset({RpcMethod.HOST_MODEL})

    NO_HOST_MODEL = "window_model: no host model"
    PAGE_ONLY = "host_model: only an extension's page reaches the host model"

    def __init__(self, model: PageHostModel) -> None:
        self._model = model

    @classmethod
    def unmounted(cls) -> list[UnavailableRpc]:
        return [
            UnavailableRpc(RpcMethod.WINDOW_MODEL, cls.NO_HOST_MODEL),
            UnavailableRpc(RpcMethod.HOST_MODEL, cls.PAGE_ONLY),
        ]

    async def answer(self, request: msg.RPCRequest) -> PoolAnswer:
        """A page's malformed request is answered, not failed: the bridge
        hands the page the refusal's ``invalid`` code, which an error string
        would lose."""
        if request.method == RpcMethod.HOST_MODEL and not request.caller:
            return PoolAnswer.failed(self.PAGE_ONLY)
        if request.method == RpcMethod.HOST_MODEL:
            binding = RpcBinding.on(type(self))[RpcMethod.HOST_MODEL][1]
            try:
                binding.validate(request.args)
            except RpcArgsError as exc:
                reply = Reply.refused(
                    str(request.args.get("id", "page")),
                    ErrorBody(
                        code=ErrorCode.INVALID, message=f"not a host-model request: {exc.__cause__}"
                    ),
                )
                return PoolAnswer.ok(reply, dump_mode="json")
        return await super().answer(request)

    @rpc(
        RpcMethod.WINDOW_MODEL,
        args=WindowModelArgs,
        scope=RpcScope.IGNI,
        streams=(PushChannel.WINDOW_MODEL_CHANGED,),
    )
    def window(self, args: WindowModelArgs, request: msg.RPCRequest) -> WindowReply:
        """Read or change the calling window's own model: its fields, toasts and dialog.

        A window spans sessions, so this is not tied to one. ``op`` says what
        to do (see the arguments); ``window`` is the window's own stable id.
        The window is the one on the connection the request came on.

        Returns, by ``op``: ``hydrate`` the whole model (``state``), its
        revision (``rev``) and the fields nobody set (``unset``); ``write``,
        ``dismiss_toast`` the new ``rev``; ``toast`` the toast as shown, with
        its id; ``open_dialog`` the dialog as shown; ``answer_dialog``
        whether that dialog was still the open one (``answered``). Every
        change is pushed to every window as ``window_model_changed``.

        Fails when the model refuses the write (an unknown field, a field
        that is not writable, a value that does not fit its type), or when
        the window's saved state could not be read or written.
        """
        wire = args.model_dump()
        if request.from_client:
            wire["connection_id"] = request.from_client
        window_request = WindowRequest(**wire)
        try:
            return self._model.services.window_rpc.handle(window_request)
        except (WindowModelError, WindowStateError) as exc:
            raise PoolRefusal(f"window_model: {exc}") from exc

    @rpc(
        RpcMethod.HOST_MODEL,
        args=PageRequest,
        scope=RpcScope.IGNI,
        streams=(
            PushChannel.HOST_SIGNAL,
            PushChannel.WINDOW_MODEL_CHANGED,
            PushChannel.OPEN_EXTERNAL,
            msg.UserMessageReceived,
            *RUN_STREAM,
        ),
    )
    async def page(self, args: PageRequest, request: msg.RPCRequest) -> Reply:
        """Make one host-object-model request on behalf of an extension's page.

        Only igni's window makes this call, for a page it frames, with the
        page's extension as the request's ``caller``. The arguments are one
        host-model request (``docs/HOST_OBJECT_MODEL.md``; the members are
        published in ``docs/host-schema.json``): ``op`` (``get``, ``set``,
        ``call``, ``subscribe``, ``unsubscribe``), ``path``, and ``params``,
        ``value``, ``signals``, ``window``, ``session`` as the op needs.
        ``home_window`` is the window framing the page; the window on the
        request's connection wins over it once it has said who it is.

        Returns the host model's reply, success or refusal alike: ``ok`` with
        ``result``, or ``ok: false`` with ``error`` (``code`` and
        ``message``), so the page gets the refusal's code. Signals the page
        subscribed to come later as ``host_signal`` pushes addressed to its
        extension; a member that changes a window's model or opens a link
        pushes ``window_model_changed`` or ``open_external``; one that posts
        to a chat starts a run there, which streams as any run does.

        Fails only when the request has no ``caller`` or this igni mounts no
        host model.
        """
        if not request.caller:
            raise PoolRefusal(self.PAGE_ONLY)
        wire = args.model_dump(mode="json", exclude_unset=True)
        windows = self._model.services.windows
        framing = windows.by_connection(request.from_client) if request.from_client else None
        if framing:
            wire["home_window"] = framing
        return await self._model.handle_page(request.caller, wire)


__all__ = ["HostModelRpcs", "PageHostModel", "WindowModelArgs"]
