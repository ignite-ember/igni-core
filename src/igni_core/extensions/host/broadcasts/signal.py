"""What every translator shares: the signal value, its run header, the emitter.

A :class:`Signal` is one published signal's value — its fields are the
host schema's type for :attr:`Signal.WIRE`, field for field
(``tests/test_what_igni_sends_its_windows_reaches_extensions_as_signals.py``
holds them to it). The :class:`Emitter` is the one place a value is
dumped: the bus hands values to sinks as they are, so a model must
become plain data before it is emitted.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Protocol, Self, TypeVar

from pydantic import BaseModel

from igni_core.extensions.host.signals import Content
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.envelope import RunScopedMessage

#: A translator for one outbound message class.
Handler = Callable[[Any], None]
#: A translator for one push channel.
PushHandler = Callable[[msg.PushNotification], None]

K = TypeVar("K")
V = TypeVar("V")


class Emit(Protocol):
    """:meth:`HostModel.emit`, as the translators reach it."""

    def __call__(
        self,
        wire: str,
        value: Any = None,
        *,
        content: Content | None = None,
        only: str | None = None,
    ) -> list[str]: ...


class Signal(BaseModel):
    """One signal's value, in the shape the host schema publishes for :attr:`WIRE`."""

    WIRE: ClassVar[str]

    @classmethod
    def of(cls, message: BaseModel) -> Self:
        """The fields a message carries under the same names (or aliases)."""
        return cls.model_validate(message, from_attributes=True)


class RunRef(Signal):
    """Which run of which session — the header of every run-scoped signal."""

    session_id: str
    run_id: str
    parent_run_id: str

    @classmethod
    def run_of(cls, message: RunScopedMessage) -> Self:
        """A run-scoped message's header and whatever else it names alike."""
        return cls.of(message)


class Emitter:
    """Signals onto the bus, dumped at the seam."""

    def __init__(self, emit: Emit) -> None:
        self._emit = emit

    def send(
        self, signal: Signal, *, content: Content | None = None, only: str | None = None
    ) -> None:
        self._emit(signal.WIRE, signal.model_dump(), content=content, only=only)

    def value(self, wire: str, value: Any, *, content: Content | None = None) -> None:
        """A value another helper already made in the published shape."""
        self._emit(wire, value, content=content)


def merged(*tables: Mapping[K, V]) -> dict[K, V]:
    """One dispatch table from several; a key claimed twice is a bug."""
    out: dict[K, V] = {}
    for table in tables:
        for key, handler in table.items():
            if key in out:
                raise RuntimeError(f"{key!r} is translated twice")
            out[key] = handler
    return out


__all__ = ["Emit", "Emitter", "Handler", "PushHandler", "RunRef", "Signal", "merged"]
