"""The handler each field of a window's model has.

A field that lasts is kept in the client-state store, so it survives
igni restarting; a field that does not lives in this process, as long as
the backend does. This is where each one says how it is read, checked
and kept.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from igni_core.extensions.host.types import SchemaTypes
from igni_core.extensions.host.window_errors import WindowModelError
from igni_core.extensions.host.window_field import FieldHandler, WindowFieldName
from igni_core.extensions.host.window_state import FieldContext, LastingState, UiKeys
from igni_core.extensions.host.window_values import (
    MODES,
    Composer,
    Page,
    Panel,
    Scroll,
    Sidebar,
    references,
)


class StoredText(FieldHandler):
    """A field that lasts as one string in the store."""

    def __init__(
        self,
        name: WindowFieldName,
        type_expression: str,
        types: SchemaTypes,
        state: LastingState,
        *,
        key: str,
    ) -> None:
        super().__init__(name, type_expression, types, state)
        self._key = key

    def read(self, ctx: FieldContext) -> str:
        return self._state.read(ctx.window_id).get(self._key, "")

    def keep(self, ctx: FieldContext, value: str) -> None:
        self._state.write(ctx.window_id, self._key, str(value))


class RouteField(FieldHandler):
    """The window's page stack: what is kept as one JSON string."""

    @property
    def always_changes(self) -> bool:
        # The same route twice is still news: "take me there" from a
        # link to the page already open returns it to its top.
        return True

    def read(self, ctx: FieldContext) -> list[Page]:
        return self._pages(self._state.read(ctx.window_id).get(UiKeys.PAGE, ""))

    def keep(self, ctx: FieldContext, value: list[Page]) -> None:
        pages = json.dumps([page.model_dump() for page in value]) if value else ""
        self._state.write(ctx.window_id, UiKeys.PAGE, pages)

    def _normalise(self, value: Any) -> Any:
        return [Page.of(page) for page in value]

    @staticmethod
    def _pages(raw: str) -> list[Page]:
        try:
            parsed = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        pages: list[Page] = []
        for entry in parsed:
            if not isinstance(entry, dict) or not isinstance(entry.get("kind"), str):
                break
            pages.append(Page.of(entry))
        return pages


class SidebarField(FieldHandler):
    """The window's sidebar: open or not, its width and its section."""

    def read(self, ctx: FieldContext) -> Sidebar:
        stored = self._state.read(ctx.window_id)
        width = stored.get(UiKeys.SIDEBAR_WIDTH) or ""
        return Sidebar(
            open=stored.get(UiKeys.SIDEBAR) == "true",
            width=int(width) if width.isdigit() else None,
            section=stored.get(UiKeys.SIDEBAR_SECTION) or None,
        )

    def keep(self, ctx: FieldContext, value: Sidebar) -> None:
        self._state.write(ctx.window_id, UiKeys.SIDEBAR, "true" if value.open else "false")
        width = value.width
        self._state.write(ctx.window_id, UiKeys.SIDEBAR_WIDTH, str(width) if width else "")
        self._state.write(ctx.window_id, UiKeys.SIDEBAR_SECTION, value.section or "")

    def _normalise(self, value: Any) -> Any:
        width = value.get("width")
        if width is not None and not 180 <= int(width) <= 600:
            raise WindowModelError(f"sidebar width {width} is outside 180 to 600 pixels")
        section = value.get("section")
        if section not in (None, "links", "sessions"):
            raise WindowModelError(f"sidebar section {section!r} is not `links` or `sessions`")
        return Sidebar(open=bool(value.get("open")), width=width, section=section)


class ComposerField(FieldHandler):
    """The window's draft: the session's, so it moves with the session."""

    def read(self, ctx: FieldContext) -> Composer:
        stored = self._state.read(ctx.window_id)
        session = stored.get(UiKeys.SESSION, "")
        if not session:
            return Composer()
        text = stored.get(UiKeys.draft(session), "")
        return Composer(
            text=text,
            attachments=references(text),
            mode=stored.get(UiKeys.mode(session), "") or "chat",
        )

    def keep(self, ctx: FieldContext, value: Composer) -> None:
        session = self._state.read(ctx.window_id).get(UiKeys.SESSION, "")
        if not session:
            raise WindowModelError(
                f"window {ctx.window_id} shows no session yet: no draft to write"
            )
        self._state.write(ctx.window_id, UiKeys.draft(session), value.text)
        mode = "" if value.mode == "chat" else value.mode
        self._state.write(ctx.window_id, UiKeys.mode(session), mode)

    def _normalise(self, value: Any) -> Any:
        mode = value.get("mode") or "chat"
        if mode not in MODES:
            raise WindowModelError(f"composer mode {mode!r} is not one of {', '.join(MODES)}")
        text = str(value.get("text") or "")
        return Composer(text=text, attachments=references(text), mode=mode)


class ModelField(FieldHandler):
    """A field the model holds as a model, in this process only."""

    def __init__(
        self,
        name: WindowFieldName,
        type_expression: str,
        types: SchemaTypes,
        state: LastingState,
        *,
        model: type[BaseModel],
    ) -> None:
        super().__init__(name, type_expression, types, state)
        self._model = model

    def read(self, ctx: FieldContext) -> Any:
        return getattr(ctx.transient, self._name)

    def keep(self, ctx: FieldContext, value: Any) -> None:
        setattr(ctx.transient, self._name, value)

    def _normalise(self, value: Any) -> Any:
        if value is None:
            # An optional field with nothing on it.
            return None
        return self._model.model_validate(value)


class PanelField(ModelField):
    """The panel over a window's chat: its kind and its parameters."""

    def _normalise(self, value: Any) -> Any:
        if value is None:
            return None
        return Panel.model_validate({**value, "params": value.get("params") or {}})


class ScrollField(ModelField):
    """Where a window's transcript is scrolled to."""

    def _normalise(self, value: Any) -> Any:
        return Scroll.model_validate({k: v for k, v in value.items() if k != "window"})


class TransientField(FieldHandler):
    """A field the model holds as it is, in this process only."""

    def read(self, ctx: FieldContext) -> Any:
        return getattr(ctx.transient, self._name)

    def keep(self, ctx: FieldContext, value: Any) -> None:
        setattr(ctx.transient, self._name, value)


class ReadOnlyField(TransientField):
    """A field that changes through its own operations, not a write."""

    @property
    def writable(self) -> bool:
        return False


__all__ = [
    "ComposerField",
    "ModelField",
    "PanelField",
    "ReadOnlyField",
    "RouteField",
    "ScrollField",
    "SidebarField",
    "StoredText",
    "TransientField",
]
