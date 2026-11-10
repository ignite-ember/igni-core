"""The fields of a window's model, each with its own handler.

The table is the model's field list: a name that is not in it is not a
field of a window's model.
"""

from __future__ import annotations

from typing import Any

from igni_core.extensions.host.types import SchemaTypes
from igni_core.extensions.host.window_errors import WindowModelError
from igni_core.extensions.host.window_field import FieldHandler, WindowFieldName, check_type
from igni_core.extensions.host.window_field_handlers import (
    ComposerField,
    ModelField,
    PanelField,
    ReadOnlyField,
    RouteField,
    ScrollField,
    SidebarField,
    StoredText,
    TransientField,
)
from igni_core.extensions.host.window_state import LastingState, UiKeys
from igni_core.extensions.host.window_values import Panel, Preview, Scroll, Selection


class WindowFields:
    """The fields of a window's model, each with its own handler."""

    def __init__(self, types: SchemaTypes, state: LastingState) -> None:
        self._types = types
        self._handlers: dict[WindowFieldName, FieldHandler] = {
            WindowFieldName.SESSION: StoredText(
                WindowFieldName.SESSION, "str", types, state, key=UiKeys.SESSION
            ),
            WindowFieldName.PROJECT_DIR: StoredText(
                WindowFieldName.PROJECT_DIR, "str", types, state, key=UiKeys.PROJECT_DIR
            ),
            WindowFieldName.ROUTE: RouteField(WindowFieldName.ROUTE, "list[Page]", types, state),
            WindowFieldName.PANEL: PanelField(
                WindowFieldName.PANEL, "Panel | None", types, state, model=Panel
            ),
            WindowFieldName.SIDEBAR: SidebarField(WindowFieldName.SIDEBAR, "Sidebar", types, state),
            WindowFieldName.PREVIEW: ModelField(
                WindowFieldName.PREVIEW, "Preview | None", types, state, model=Preview
            ),
            WindowFieldName.SELECTION: ModelField(
                WindowFieldName.SELECTION, "SelectionState", types, state, model=Selection
            ),
            WindowFieldName.COMPOSER: ComposerField(
                WindowFieldName.COMPOSER, "Composer", types, state
            ),
            WindowFieldName.TOASTS: ReadOnlyField(
                WindowFieldName.TOASTS, "list[Toast]", types, state
            ),
            WindowFieldName.DIALOG: ReadOnlyField(
                WindowFieldName.DIALOG, "Dialog | None", types, state
            ),
            WindowFieldName.SCROLL: ScrollField(
                WindowFieldName.SCROLL, "Scroll", types, state, model=Scroll
            ),
            WindowFieldName.FOCUSED: TransientField(WindowFieldName.FOCUSED, "bool", types, state),
        }

    def names(self) -> tuple[str, ...]:
        """Every field of a window's model, by name."""
        return tuple(str(name) for name in self._handlers)

    def check(self, name: str, expression: str, value: Any) -> Any:
        """A value checked against the schema's type, as plain data."""
        return check_type(self._types, name, expression, value)

    def handlers(self) -> tuple[FieldHandler, ...]:
        """Every field's handler, in the model's field order."""
        return tuple(self._handlers.values())

    def handler(self, name: str) -> FieldHandler:
        """The handler of the field with this name."""
        try:
            key = WindowFieldName(name)
        except ValueError:
            raise WindowModelError(f"a window's model has no field {name!r}") from None
        return self._handlers[key]


__all__ = ["WindowFields"]
