"""``host.ui``: one window's model, written through the same
:class:`~igni_core.extensions.host.windows.Windows` the window itself
writes — so an extension's change is pushed to the window, and
signalled, exactly as the person's is."""

from __future__ import annotations

from typing import Any

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.base import Call, Members, call, get, put
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
)
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.windows import WindowModelError


class WindowModelMembers(Members):
    """Base: reading and writing one field of the call's window."""

    def read(self, c: Call, name: str) -> Any:
        return self.host.windows.get(self.window(c), name)

    def write(self, c: Call, name: str, value: Any) -> Any:
        window = self.window(c)
        try:
            self.host.windows.write(window, name, value)
        except WindowModelError as exc:
            raise HostModelError(ErrorCode.INVALID, str(exc)) from None
        return self.host.windows.get(window, name)


class UiNavigationMembers(WindowModelMembers):
    """Where the window is: its route, its panel, its sidebar."""

    @get("ui.route")
    async def route(self, c: Call) -> Any:
        return self.read(c, "route")

    @put("ui.route")
    async def set_route(self, c: Call) -> None:
        self.write(c, "route", c.value or [])

    @call("ui.navigate")
    async def navigate(self, c: Call) -> Any:
        page = Page.named(c.params["page"], c.params.get("params"))
        return self.write(c, "route", [page.model_dump()])

    @call("ui.back")
    async def back(self, c: Call) -> Any:
        return self.write(c, "route", self.read(c, "route")[:-1])

    @get("ui.panel")
    async def panel(self, c: Call) -> Any:
        return self.read(c, "panel")

    @put("ui.panel")
    async def set_panel(self, c: Call) -> None:
        self.write(c, "panel", c.value)

    @call("ui.open_panel")
    async def open_panel(self, c: Call) -> Any:
        kind = PanelKind.of(str(c.params["kind"]))
        panel = Panel(kind=kind.value, params=c.params.get("params") or {})
        return self.write(c, "panel", panel.model_dump())

    @call("ui.close_panel")
    async def close_panel(self, c: Call) -> None:
        self.write(c, "panel", None)

    @get("ui.sidebar")
    async def sidebar(self, c: Call) -> Any:
        return self.read(c, "sidebar")

    @put("ui.sidebar")
    async def set_sidebar_value(self, c: Call) -> None:
        self.write(c, "sidebar", c.value)

    @call("ui.set_sidebar")
    async def set_sidebar(self, c: Call) -> Any:
        sidebar = Sidebar.merged(
            self.read(c, "sidebar"),
            open=bool(c.params["open"]),
            width=c.params.get("width"),
            section=c.params.get("section"),
        )
        return self.write(c, "sidebar", sidebar.model_dump())


class UiContentMembers(WindowModelMembers):
    """What the window shows: its preview, selection, draft and scroll."""

    @get("ui.preview")
    async def preview(self, c: Call) -> Any:
        return self.read(c, "preview")

    @put("ui.preview")
    async def set_preview(self, c: Call) -> None:
        if c.value is not None:
            self.within_workspace(c, str(c.value.get("path", "")))
        self.write(c, "preview", c.value)

    @call("ui.open_preview")
    async def open_preview(self, c: Call) -> Any:
        path = str(c.params["path"])
        self.within_workspace(c, path)
        return self.write(c, "preview", PreviewTarget(path=path).model_dump())

    @call("ui.close_preview")
    async def close_preview(self, c: Call) -> None:
        self.write(c, "preview", None)

    def within_workspace(self, c: Call, path: str) -> None:
        """A preview shows a file's contents to the person, not to the
        extension — but it is still the extension pointing at the
        project, so the path must be one ``workspacePaths`` allows."""
        if not c.grants.workspace.granted or not c.grants.workspace.permits(path):
            raise HostModelError(
                ErrorCode.REFUSED,
                f"'{path}' is outside what '{c.extension_id}' was granted of the workspace",
            )

    @get("ui.selection")
    async def selection(self, c: Call) -> Any:
        return self.read(c, "selection")

    @put("ui.selection")
    async def set_selection(self, c: Call) -> None:
        self.write(c, "selection", c.value)

    @call("ui.select")
    async def select(self, c: Call) -> Any:
        return self.write(c, "selection", c.params["selection"])

    @get("ui.composer")
    async def composer(self, c: Call) -> Any:
        return self.read(c, "composer")

    @put("ui.composer")
    async def set_composer_value(self, c: Call) -> None:
        self.write(c, "composer", c.value)

    @call("ui.set_composer")
    async def set_composer(self, c: Call) -> Any:
        mode = c.params.get("mode")
        composer = Composer.with_attachments(
            str(c.params["text"]),
            c.params.get("attachments") or [],
            mode,
            None if mode else self.read(c, "composer").get("mode"),
        )
        return self.write(c, "composer", composer.model_dump())

    @get("ui.scroll")
    async def scroll(self, c: Call) -> Any:
        return self.read(c, "scroll")

    @call("ui.scroll_to")
    async def scroll_to(self, c: Call) -> Any:
        position = ScrollPosition.to(str(c.params["message_id"]))
        return self.write(c, "scroll", position.model_dump())


class UiNoticeMembers(WindowModelMembers):
    """What the window tells the person: toasts and dialogs."""

    @get("ui.toasts")
    async def toasts(self, c: Call) -> Any:
        return self.read(c, "toasts")

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
        return self.host.windows.toast(self.window(c), request.model_dump())

    @call("ui.dismiss_toast")
    async def dismiss_toast(self, c: Call) -> None:
        window = self.window(c)
        toast_id = str(c.params["toast_id"])
        found = self.host.windows.toast_of(window, toast_id)
        if found is None:
            raise HostModelError(ErrorCode.NOT_FOUND, f"no toast {toast_id!r} is on screen")
        if found.get("source") != c.extension_id:
            raise HostModelError(ErrorCode.REFUSED, "an extension dismisses only its own toasts")
        self.host.windows.dismiss_toast(window, toast_id)

    @get("ui.dialog")
    async def dialog(self, c: Call) -> Any:
        return self.read(c, "dialog")

    @call("ui.show_dialog")
    async def show_dialog(self, c: Call) -> Any:
        spec = DialogSpec.of(c.params["spec"]).model_dump(mode="json")
        _dialog, answer = self.host.windows.open_dialog(self.window(c), spec, source=c.extension_id)
        return await answer


__all__ = ["UiContentMembers", "UiNavigationMembers", "UiNoticeMembers", "WindowModelMembers"]
