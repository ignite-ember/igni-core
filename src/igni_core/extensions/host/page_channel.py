"""The page's channel to the host model: the ``host_model`` RPC, through the bridge.

A page's request arrives as the RPC's arguments with the window framing
it added (``home_window``). Its signals go back to the windows as
``host_signal`` pushes addressed to the extension; each window posts
them into that extension's frames, and the bridge in each frame calls
the handlers it has.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import Field, ValidationError, field_validator

from igni_core.extensions.host.caller import Caller
from igni_core.extensions.host.pushes import WindowPushes
from igni_core.extensions.host.signals import SignalBus, Subscriber
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.wire import ErrorBody, Op, Reply, Request, SignalMessage


class PageRequest(Request):
    """A request from an extension's page to the host model, sent through the ``host_model`` method."""

    id: int | str = Field(
        default="page",
        description="Any id you choose; it is echoed on the reply and never interpreted.",
    )
    # Added by the window's side of the bridge, which knows it.
    home_window: str | None = Field(
        default=None,
        description=(
            "The id of the window showing the page; a page's `host.ui` is its own window's. "
            "Set by the window, not the page."
        ),
    )

    @field_validator("home_window", mode="before")
    @classmethod
    def _home(cls, home: Any) -> str | None:
        return str(home) if home else None


class Handle(Protocol):
    """:meth:`HostModel.handle(request, caller) <.model.HostModel.handle>`,
    as a page's request reaches it."""

    async def __call__(self, request: Request, caller: Caller) -> Reply: ...


class PageSink:
    """An extension's pages, reached through the windows."""

    def __init__(self, pushes: WindowPushes, extension_id: str) -> None:
        self._pushes = pushes
        self._extension_id = extension_id

    def deliver(self, message: SignalMessage) -> None:
        self._pushes.host_signal(self._extension_id, message)


class PageChannel:
    """Answers pages' requests, with one subscriber for all of an extension's pages."""

    def __init__(self, bus: SignalBus, pushes: WindowPushes, handle: Handle) -> None:
        self._bus = bus
        self._pushes = pushes
        self._handle = handle
        self._subscribers: dict[str, Subscriber] = {}

    def subscriber_for(self, extension_id: str) -> Subscriber:
        found = self._subscribers.get(extension_id)
        if found is None:
            found = self._bus.attach(extension_id, PageSink(self._pushes, extension_id))
            self._subscribers[extension_id] = found
        return found

    async def answer(self, extension_id: str, args: dict[str, Any]) -> Reply:
        """One request from an extension's page. A malformed one is a refusal,
        and so is a hook answer: only an extension's process answers one."""
        try:
            request = PageRequest.model_validate(args)
        except ValidationError as exc:
            return Reply.refused(
                str(args.get("id", "page")),
                ErrorBody(code=ErrorCode.INVALID, message=f"not a host-model request: {exc}"),
            )
        if request.op is Op.ANSWER:
            return Reply.refused(
                request.id,
                ErrorBody(
                    code=ErrorCode.INVALID,
                    message="only an extension's process answers a hook event; a page cannot",
                ),
            )
        caller = Caller.page(extension_id, self.subscriber_for(extension_id), request.home_window)
        return await self._handle(request, caller)


__all__ = ["PageChannel", "PageRequest", "PageSink"]
