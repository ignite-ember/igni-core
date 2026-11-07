"""What every translator shares: the run header, the handler shapes, the merge.

The signal value and the emitter live in :mod:`..signal_values`, beside
the host model, because values outside this package are signals too.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Self, TypeVar

from igni_core.extensions.host.signal_values import Signal
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.envelope import RunScopedMessage

#: A translator for one outbound message class.
Handler = Callable[[Any], None]
#: A translator for one push channel.
PushHandler = Callable[[msg.PushNotification], None]

K = TypeVar("K")
V = TypeVar("V")


class RunRef(Signal):
    """Which run of which session — the header of every run-scoped signal."""

    session_id: str
    run_id: str
    parent_run_id: str

    @classmethod
    def run_of(cls, message: RunScopedMessage) -> Self:
        """A run-scoped message's header and whatever else it names alike."""
        return cls.of(message)


def merged(*tables: Mapping[K, V]) -> dict[K, V]:
    """One dispatch table from several; a key claimed twice is a bug."""
    out: dict[K, V] = {}
    for table in tables:
        for key, handler in table.items():
            if key in out:
                raise RuntimeError(f"{key!r} is translated twice")
            out[key] = handler
    return out


__all__ = ["Handler", "PushHandler", "RunRef", "merged"]
