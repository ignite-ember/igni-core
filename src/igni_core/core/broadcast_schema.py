"""Typed events for the session broadcast bus.

:class:`BroadcastEvent` is the base every push-channel event
subclasses; each concrete event lives in its domain's schema
module and narrows ``channel`` to a ``Literal``. This module stays
a leaf (pydantic and typing only) so any tool can import it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class BroadcastEvent(BaseModel):
    """One push-channel event. ``channel`` is the tag the FE routes
    on; every other field is the event's payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: str

    def to_payload(self) -> dict[str, Any]:
        """The wire payload: every field except ``channel``."""
        return self.model_dump(mode="json", exclude={"channel"})

    def with_run_id(self, run_id: str | None) -> BroadcastEvent:
        """Events that are not run-scoped carry no run_id."""
        return self


class RunScopedBroadcastEvent(BroadcastEvent):
    """An event the post-run drain stamps with the finished run's id."""

    run_id: str | None = Field(default=None, exclude_if=lambda v: v is None)

    def with_run_id(self, run_id: str | None) -> BroadcastEvent:
        """A copy carrying ``run_id``; an explicit run_id already on
        the event wins, and an empty one changes nothing."""
        if not run_id or self.run_id is not None:
            return self
        return self.model_copy(update={"run_id": run_id})


@runtime_checkable
class BroadcastSink(Protocol):
    """Anything that receives broadcast events: bus subscribers and
    the emit callable tools are handed."""

    def __call__(self, event: BroadcastEvent) -> None: ...
