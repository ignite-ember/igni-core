"""Who is relaying questions to a person right now.

A question only reaches a person while a forwarder is running: the
backend's stream multiplexer opens a relay for the length of a run and
closes it after. A tool call that needs an approval while no relay is
open has nobody to ask — a headless run, a CI job — and is refused
rather than left waiting for ever.
"""

from __future__ import annotations


class RelayCounter:
    """How many forwarders are relaying questions to a person."""

    def __init__(self) -> None:
        self._open = 0

    def open(self) -> None:
        """A forwarder starts relaying."""
        self._open += 1

    def close(self) -> None:
        """A forwarder has stopped relaying."""
        self._open = max(0, self._open - 1)

    @property
    def has_relay(self) -> bool:
        """Whether a question pushed now would reach a person."""
        return self._open > 0


__all__ = ["RelayCounter"]
