"""An RPC's answer, read through the model that produced it.

igni's backend answers an RPC with a model of its own — a plugin's
details, an MCP server's row, a scheduled task — and the host reads
that answer back through the *same* model. One model per shape means
one place a field is named: a field renamed in the backend is fixed
in the model that carries it, once, and the host and the backend
cannot drift apart.

An answer that does not fit its model is refused, and said so. It is
not handed on half-read, and nothing is invented to stand in for what
it did not carry: the extension is told what igni actually answered,
or that igni answered something the host does not understand.
"""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.spec import ErrorCode

ModelT = TypeVar("ModelT", bound=BaseModel)


def read_answer(model: type[ModelT], answer: Any, *, method: str) -> ModelT:
    """The answer as the model that produced it; a refusal when it does not fit."""
    try:
        return model.model_validate(answer)
    except ValidationError as exc:
        raise HostModelError(ErrorCode.FAILED, _why(method, model.__name__, exc)) from None


def read_answers(model: type[ModelT], answer: Any, *, method: str) -> list[ModelT]:
    """A list of answers, each read through the model that produced it."""
    if not isinstance(answer, list):
        raise HostModelError(
            ErrorCode.FAILED, f"{method} answered with {type(answer).__name__}, not a list"
        )
    return [read_answer(model, one, method=method) for one in answer]


def read_list(adapter: TypeAdapter[Any], answer: Any, *, method: str, what: str) -> list[Any]:
    """A list of answers, read through the type that produced them.

    For the answers one type does not name: every slash command,
    whichever kind it is, and one union names them all; the skill
    names, which are strings and nothing more.
    """
    try:
        return list(adapter.validate_python(answer))
    except ValidationError as exc:
        raise HostModelError(ErrorCode.FAILED, _why(method, what, exc)) from None


def _why(method: str, what: str, exc: ValidationError) -> str:
    """Why the answer did not fit, in a sentence."""
    errors = exc.errors()
    if not errors:
        return f"{method} answered with something {what} does not describe"
    first = errors[0]
    where = ".".join(str(part) for part in first["loc"])
    return f"{method} answered with something {what} does not describe ({where}: {first['msg']})"


__all__ = ["read_answer", "read_answers", "read_list"]
