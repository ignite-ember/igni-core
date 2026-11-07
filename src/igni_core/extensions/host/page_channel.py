"""The page's channel to the host model: the ``host_model`` RPC, through the bridge.

A page's request arrives as the RPC's arguments with the window framing
it added (``home_window``). Its signals go back to the windows as
``host_signal`` pushes addressed to the extension; each window posts
them into that extension's frames, and the bridge in each frame calls
the handlers it has.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import ValidationError, field_validator

from igni_core.extensions.host.pushes import WindowPushes
from igni_core.extensions.host.signals import SignalBus, Subscriber
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.wire import ErrorBody, Reply, Request, SignalMessage


class PageRequest(Request):
    """A page's request, and the window framing the page."""

    id: int | str = "page"
    #: Added by the window's side of the bridge, which knows it. A page's
    #: ``host.ui`` is its own window's.
    home_window: str | None = None

    @field_validator("home_window", mode="before")
    @classmethod
    def _home(cls, home: Any) -> str | None:
        return str(home) if home else None


class Handle(Protocol):
    """:meth:`HostModel.handle`, as a page's request reaches it."""

    async def __call__(
        self,
        extension_id: str,
        request: Request,
        *,
        subscriber: Subscriber | None = None,
        home_window: str | None = None,
    ) -> Reply: ...


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
        """One request from an extension's page. A malformed one is a refusal."""
        try:
            request = PageRequest.model_validate(args)
        except ValidationError as exc:
            return Reply(
                id=str(args.get("id", "page")),
                ok=False,
                error=ErrorBody(code=ErrorCode.INVALID, message=f"not a host-model request: {exc}"),
            )
        return await self._handle(
            extension_id,
            request,
            subscriber=self.subscriber_for(extension_id),
            home_window=request.home_window,
        )


__all__ = ["PageChannel", "PageRequest", "PageSink"]
