"""A published signal's value, and the one seam that puts it on the bus.

A :class:`Signal` is one published signal's value — its fields are the
host schema's type for :attr:`Signal.WIRE`, field for field
(``tests/test_what_igni_sends_its_windows_reaches_extensions_as_signals.py``
holds them to it). The :class:`Emitter` is the one place a value is
dumped: the bus hands values to sinks as they are, so a model must
become plain data before it is emitted.

It lives beside the host model rather than in :mod:`.broadcasts`: the
values the members answer with (:mod:`.values`), the observations and
the window signals are signals too, and the broadcasts package imports
those values — a signal base inside it would be a cycle.
"""

from __future__ import annotations

from typing import Any, ClassVar, Protocol, Self

from pydantic import BaseModel

from igni_core.extensions.host.plain import plain
from igni_core.extensions.host.signals import Content


class Emit(Protocol):
    """:meth:`SignalBus.emit`, as the producers reach it."""

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
    def of(cls, message: Any) -> Self:
        """The fields a message carries under the same names (or aliases)."""
        return cls.model_validate(message, from_attributes=True)


class Emitter:
    """Signals onto the bus, dumped at the seam."""

    def __init__(self, emit: Emit) -> None:
        self._emit = emit

    def send(
        self, signal: Signal, *, content: Content | None = None, only: str | None = None
    ) -> None:
        self._emit(signal.WIRE, signal.model_dump(), content=content, only=only)

    def value(self, wire: str, value: Any, *, content: Content | None = None) -> None:
        """A value in the published shape — plain data or a model, dumped here."""
        self._emit(wire, plain(value), content=content)


__all__ = ["Emit", "Emitter", "Signal"]
