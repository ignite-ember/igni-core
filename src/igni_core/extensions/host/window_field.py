"""One field of a window's model: what it holds, and the contract its
handler keeps.

A field says what the schema's type for it is, whether a view may write
it, and how it is read, checked and kept. The handlers that fill in the
how are in :mod:`.window_field_handlers`; the list of them is in
:mod:`.window_fields`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from igni_core.extensions.host.plain import plain
from igni_core.extensions.host.types import SchemaTypes
from igni_core.extensions.host.window_errors import WindowModelError
from igni_core.extensions.host.window_state import FieldContext, LastingState


class WindowFieldName(StrEnum):
    """The fields of a window's model, by name."""

    SESSION = "session"
    PROJECT_DIR = "project_dir"
    ROUTE = "route"
    PANEL = "panel"
    SIDEBAR = "sidebar"
    PREVIEW = "preview"
    SELECTION = "selection"
    COMPOSER = "composer"
    TOASTS = "toasts"
    DIALOG = "dialog"
    SCROLL = "scroll"
    FOCUSED = "focused"


def unset_nones(value: Any) -> Any:
    """A key holding ``None`` as a key left out. Every optional field of
    the schema's types defaults to ``None``, so this changes no meaning —
    and a value read back from the model (where an absent part is
    ``None``) can be written again as it was read."""
    if isinstance(value, dict):
        return {k: unset_nones(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [unset_nones(v) for v in value]
    return value


def check_type(types: SchemaTypes, name: str, expression: str, value: Any) -> Any:
    """The value as the schema's type describes it, or a refusal.

    A model is dumped first: the schema checks plain data, and the
    model's own types are how it says what the data is.
    """
    try:
        return types.validate_plain(expression, unset_nones(plain(value)))
    except ValidationError as exc:
        raise WindowModelError(f"{name}: expected {expression}: {exc}") from None


class FieldHandler(ABC):
    """One field of a window's model: how it is read, checked and kept.

    ``check`` is the model's contract with a writer: the value as the
    model holds it, checked against the schema's type for the field and
    then normalised.
    """

    def __init__(
        self,
        name: WindowFieldName,
        type_expression: str,
        types: SchemaTypes,
        state: LastingState,
    ) -> None:
        self._name = str(name)
        self._type = type_expression
        self._types = types
        self._state = state

    @property
    def name(self) -> str:
        """The field's name, as the model names it."""
        return self._name

    @property
    def writable(self) -> bool:
        """Whether a view may write this field."""
        return True

    @property
    def always_changes(self) -> bool:
        """Whether a write of the value already there is still a change."""
        return False

    @abstractmethod
    def read(self, ctx: FieldContext) -> Any:
        """The field's value now."""

    def check(self, value: Any) -> Any:
        """The value as the model holds it: checked, then normalised."""
        return self._normalise(self._validated(value))

    @abstractmethod
    def keep(self, ctx: FieldContext, value: Any) -> None:
        """Where the value goes: the store, or this process."""

    def _validated(self, value: Any) -> Any:
        """The value as the schema's type for this field describes it."""
        return check_type(self._types, self._name, self._type, value)

    def _normalise(self, value: Any) -> Any:
        """The value as the model keeps it, once the schema has had it."""
        return value


__all__ = ["FieldHandler", "WindowFieldName", "check_type", "unset_nones"]
