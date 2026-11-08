"""``window_model`` and ``host_model``: the host object model, as windows and pages reach it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from igni_core.backend.pool_rpcs.base import PoolRpc, UnavailableRpc
from igni_core.backend.schemas_pool import PoolAnswer
from igni_core.extensions.host.windows import WindowModelError, WindowRequest
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


class WindowRpc(Protocol):
    def handle(self, request: WindowRequest, /) -> BaseModel: ...


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

    async def handle_page(self, extension_id: str, args: dict[str, Any], /) -> BaseModel: ...


class HostModelRpcs(PoolRpc):
    """Carries a window's or a page's request to the host model, and its answer back."""

    methods = frozenset({RpcMethod.WINDOW_MODEL, RpcMethod.HOST_MODEL})

    NO_HOST_MODEL = "window_model: no host model"
    PAGE_ONLY = "host_model: only an extension's page reaches the host model"

    def __init__(self, model: PageHostModel) -> None:
        self._model = model
        self._by_method: dict[str, Callable[[msg.RPCRequest], Awaitable[PoolAnswer]]] = {
            RpcMethod.WINDOW_MODEL: self._window,
            RpcMethod.HOST_MODEL: self._page,
        }

    @classmethod
    def unmounted(cls) -> list[UnavailableRpc]:
        return [
            UnavailableRpc(RpcMethod.WINDOW_MODEL, cls.NO_HOST_MODEL),
            UnavailableRpc(RpcMethod.HOST_MODEL, cls.PAGE_ONLY),
        ]

    async def answer(self, request: msg.RPCRequest) -> PoolAnswer:
        return await self._by_method[request.method](request)

    async def _window(self, request: msg.RPCRequest) -> PoolAnswer:
        """A window reading or writing its own model.

        Not tied to any session — a window spans them. A refused write is
        an error, so the window drops its optimistic copy and shows the
        model's. A window's connection is the one it spoke on, never one it
        names.
        """
        args = dict(request.args)
        if request.from_client:
            args["connection_id"] = request.from_client
        try:
            reply = self._model.services.window_rpc.handle(WindowRequest.model_validate(args))
        except (ValidationError, WindowModelError) as exc:
            return PoolAnswer.failed(f"window_model: {exc}")
        return PoolAnswer.ok(reply)

    async def _page(self, request: msg.RPCRequest) -> PoolAnswer:
        """An extension's page, through the bridge, with the frame's extension
        as ``caller`` — set by the host side of the bridge, never by the frame.

        The model's reply, success or refusal, is the result: the bridge
        needs the refusal's code, which an error string would lose. The
        page's window is the one framing it — the window on the connection
        that forwarded this — and the argument stands only until that window
        has said who it is.
        """
        if not request.caller:
            return PoolAnswer.failed(self.PAGE_ONLY)
        args = dict(request.args)
        windows = self._model.services.windows
        framing = windows.by_connection(request.from_client) if request.from_client else None
        if framing:
            args["home_window"] = framing
        reply = await self._model.handle_page(request.caller, args)
        return PoolAnswer.ok(reply, dump_mode="json")
