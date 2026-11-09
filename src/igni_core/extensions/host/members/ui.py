"""``host.ui``: one window's model, written through the same
:class:`~igni_core.extensions.host.windows.Windows` the window itself
writes — so an extension's change is pushed to the window, and
signalled, exactly as the person's is."""

from __future__ import annotations

from typing import Any

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.tag import call, get, put
from igni_core.extensions.host.members.window_values import (
    Composer,
    DialogSpec,
    Page,
    Panel,
    PanelKind,
    PreviewTarget,
    ScrollPosition,
    Sidebar,
    ToastKind,
    ToastRequest,
    WindowFieldName,
)
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.windows import WindowModelError, WindowStateError


class WindowModelMembers(Members):
    """Base: reading and writing one field of the call's window."""

    def read(self, c: Call, field: WindowFieldName) -> Any:
        return self._read(c.require_window(), field)

    def write(self, c: Call, field: WindowFieldName, value: Any) -> Any:
        window = c.require_window()
        try:
            self.host.windows.write(window, field.value, value)
        except WindowModelError as exc:
            raise HostModelError(ErrorCode.INVALID, str(exc)) from None
        except WindowStateError as exc:
            raise HostModelError(ErrorCode.FAILED, str(exc)) from None
        return self._read(window, field)

    def _read(self, window: str, field: WindowFieldName) -> Any:
        """One field of a window already resolved.

        The window is resolved once per call: a write reads the field
        back to answer with it, and it is the same window the write
        went to.
        """
        try:
            return self.host.windows.get(window, field.value)
        except WindowStateError as exc:
            raise HostModelError(ErrorCode.FAILED, str(exc)) from None


class UiNavigationMembers(WindowModelMembers):
    """Where the window is: its route, its panel, its sidebar."""

    @get("ui.route")
    async def route(self, c: Call) -> Any:
        return self.read(c, WindowFieldName.ROUTE)

    @put("ui.route")
    async def set_route(self, c: Call) -> None:
        self.write(c, WindowFieldName.ROUTE, c.value or [])

    @call("ui.navigate")
    async def navigate(self, c: Call) -> Any:
        page = Page.named(c.params["page"], c.params.get("params"))
        return self.write(c, WindowFieldName.ROUTE, [page.model_dump()])

    @call("ui.back")
    async def back(self, c: Call) -> Any:
        return self.write(c, WindowFieldName.ROUTE, self.read(c, WindowFieldName.ROUTE)[:-1])

    @get("ui.panel")
    async def panel(self, c: Call) -> Any:
        return self.read(c, WindowFieldName.PANEL)

    @put("ui.panel")
    async def set_panel(self, c: Call) -> None:
        self.write(c, WindowFieldName.PANEL, c.value)

    @call("ui.open_panel")
    async def open_panel(self, c: Call) -> Any:
        kind = PanelKind.of(str(c.params["kind"]))
        panel = Panel(kind=kind.value, params=c.params.get("params") or {})
        return self.write(c, WindowFieldName.PANEL, panel.model_dump())

    @call("ui.close_panel")
    async def close_panel(self, c: Call) -> None:
        self.write(c, WindowFieldName.PANEL, None)

    @get("ui.sidebar")
    async def sidebar(self, c: Call) -> Any:
        return self.read(c, WindowFieldName.SIDEBAR)

    @put("ui.sidebar")
    async def set_sidebar_value(self, c: Call) -> None:
        self.write(c, WindowFieldName.SIDEBAR, c.value)

    @call("ui.set_sidebar")
    async def set_sidebar(self, c: Call) -> Any:
        sidebar = Sidebar.merged(
            self.read(c, WindowFieldName.SIDEBAR),
            open=bool(c.params["open"]),
            width=c.params.get("width"),
            section=c.params.get("section"),
        )
        return self.write(c, WindowFieldName.SIDEBAR, sidebar.model_dump())


class UiContentMembers(WindowModelMembers):
    """What the window shows: its preview, selection, draft and scroll."""

    @get("ui.preview")
    async def preview(self, c: Call) -> Any:
        return self.read(c, WindowFieldName.PREVIEW)

    @put("ui.preview")
    async def set_preview(self, c: Call) -> None:
        if c.value is not None:
            c.grants.require_workspace_path(str(c.value.get("path", "")))
        self.write(c, WindowFieldName.PREVIEW, c.value)

    @call("ui.open_preview")
    async def open_preview(self, c: Call) -> Any:
        path = str(c.params["path"])
        c.grants.require_workspace_path(path)
        return self.write(c, WindowFieldName.PREVIEW, PreviewTarget(path=path).model_dump())

    @call("ui.close_preview")
    async def close_preview(self, c: Call) -> None:
        self.write(c, WindowFieldName.PREVIEW, None)

    @get("ui.selection")
    async def selection(self, c: Call) -> Any:
        return self.read(c, WindowFieldName.SELECTION)

    @put("ui.selection")
    async def set_selection(self, c: Call) -> None:
        self.write(c, WindowFieldName.SELECTION, c.value)

    @call("ui.select")
    async def select(self, c: Call) -> Any:
        return self.write(c, WindowFieldName.SELECTION, c.params["selection"])

    @get("ui.composer")
    async def composer(self, c: Call) -> Any:
        return self.read(c, WindowFieldName.COMPOSER)

    @put("ui.composer")
    async def set_composer_value(self, c: Call) -> None:
        self.write(c, WindowFieldName.COMPOSER, c.value)

    @call("ui.set_composer")
    async def set_composer(self, c: Call) -> Any:
        mode = c.params.get("mode")
        composer = Composer.with_attachments(
            str(c.params["text"]),
            c.params.get("attachments") or [],
            mode,
            None if mode else self.read(c, WindowFieldName.COMPOSER).get("mode"),
        )
        return self.write(c, WindowFieldName.COMPOSER, composer.model_dump())

    @get("ui.scroll")
    async def scroll(self, c: Call) -> Any:
        return self.read(c, WindowFieldName.SCROLL)

    @call("ui.scroll_to")
    async def scroll_to(self, c: Call) -> Any:
        position = ScrollPosition.to(str(c.params["message_id"]))
        return self.write(c, WindowFieldName.SCROLL, position.model_dump())


class UiNoticeMembers(WindowModelMembers):
    """What the window tells the person: toasts and dialogs."""

    @get("ui.toasts")
    async def toasts(self, c: Call) -> Any:
        return self.read(c, WindowFieldName.TOASTS)

    @call("ui.toast")
    async def toast(self, c: Call) -> Any:
        kind = ToastKind.of(c.params.get("kind") or "info")
        request = ToastRequest(
            text=c.params["text"],
            kind=kind.value,
            body=c.params.get("body"),
            target=c.params.get("target"),
            source=c.extension_id,
        )
        return self.host.windows.toast(c.require_window(), request.model_dump())

    @call("ui.dismiss_toast")
    async def dismiss_toast(self, c: Call) -> None:
        window = c.require_window()
        toast_id = str(c.params["toast_id"])
        found = self.host.windows.toast_of(window, toast_id)
        if found is None:
            raise HostModelError(ErrorCode.NOT_FOUND, f"no toast {toast_id!r} is on screen")
        if found.get("source") != c.extension_id:
            raise HostModelError(ErrorCode.REFUSED, "an extension dismisses only its own toasts")
        self.host.windows.dismiss_toast(window, toast_id)

    @get("ui.dialog")
    async def dialog(self, c: Call) -> Any:
        return self.read(c, WindowFieldName.DIALOG)

    @call("ui.show_dialog")
    async def show_dialog(self, c: Call) -> Any:
        spec = DialogSpec.of(c.params["spec"]).model_dump(mode="json")
        _dialog, answer = self.host.windows.open_dialog(
            c.require_window(), spec, source=c.extension_id
        )
        return await answer


__all__ = ["UiContentMembers", "UiNavigationMembers", "UiNoticeMembers", "WindowModelMembers"]
