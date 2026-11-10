"""Who is asking: the extension a request comes from, and where it lives."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SkipValidation

from igni_core.extensions.host.signals import Subscriber


class Caller(BaseModel):
    """The origin of one request to the host model.

    ``subscriber`` is the connection signals go back on; ``None`` for a
    caller that cannot subscribe. ``home_window`` is the window the
    caller lives in: a page's own window; ``None`` for a process, whose
    ``host.ui`` is the active window.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    extension_id: str
    subscriber: SkipValidation[Subscriber | None] = None
    home_window: str | None = None

    @classmethod
    def process(cls, extension_id: str, subscriber: Subscriber) -> Caller:
        """An extension's process, on its loopback socket."""
        return cls(extension_id=extension_id, subscriber=subscriber)

    @classmethod
    def page(cls, extension_id: str, subscriber: Subscriber, home_window: str | None) -> Caller:
        """An extension's page, through the bridge, in the window that frames it."""
        return cls(extension_id=extension_id, subscriber=subscriber, home_window=home_window)


__all__ = ["Caller"]
