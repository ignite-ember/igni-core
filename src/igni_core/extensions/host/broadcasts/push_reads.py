"""A push's payload, read through the model that produced it.

igni's pushes are its own wire: a channel names the kind of event and
the payload carries that event's fields. The producer builds the
payload from a model — the typed pushes have one per channel
(:mod:`igni_core.protocol.schemas.push_payloads`), a broadcast has the
event's own class — so the host reads it back through *that* model.
One model per shape means one place a field is named: a field renamed
on the wire is fixed in the model that carries it, once, and the host
and the producer cannot drift apart.

A payload that does not fit its model is dropped, and said so. It is
not read half-way, and nothing is invented to stand in for what it did
not carry: an extension is told what igni actually pushed, or nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)
ValueT = TypeVar("ValueT")


def read_push(
    model: type[ModelT], payload: Mapping[str, Any], *, channel: str
) -> ModelT | None:
    """The payload as the model that produced it; ``None`` when it does not fit."""
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        _dropped(channel, model.__name__, exc)
        return None


def read_push_from(
    models: tuple[type[BaseModel], ...], payload: Mapping[str, Any], *, channel: str
) -> BaseModel | None:
    """The payload as the first of the models that fits it; ``None`` when none does.

    For the channel two producers share: the coordinator's own mode
    flip and the plan tool's follow-up carry different fields, and only
    one of them fits each payload.
    """
    failure: ValidationError | None = None
    for model in models:
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            failure = exc
    if failure is not None:
        _dropped(channel, " or ".join(model.__name__ for model in models), failure)
    return None


def read_union(
    adapter: TypeAdapter[ValueT], payload: Mapping[str, Any], *, channel: str
) -> ValueT | None:
    """The payload as the member of a union it names; ``None`` when it names none."""
    try:
        return adapter.validate_python(payload)
    except ValidationError as exc:
        _dropped(channel, "the events it names", exc)
        return None


def _dropped(channel: str, what: str, exc: ValidationError) -> None:
    """Say that a push was dropped, and why it did not fit."""
    errors = exc.errors()
    if not errors:
        logger.warning("host model: the %s push does not fit %s", channel, what)
        return
    first = errors[0]
    where = ".".join(str(part) for part in first["loc"])
    logger.warning(
        "host model: the %s push does not fit %s (%s: %s)",
        channel,
        what,
        where or "the payload",
        first["msg"],
    )


__all__ = ["read_push", "read_push_from", "read_union"]
