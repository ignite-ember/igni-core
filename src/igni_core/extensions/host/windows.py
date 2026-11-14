"""igni's windows, and the model each one's views bind to.

A window is a view of igni — a desktop window, an IDE panel, a browser
tab. Each has a stable id that survives reloads: the ``client_id`` the
web client keeps (``clientState.ts``'s ``ensureClientId``).

**The window's model.** Everything a window shows — its page stack,
the panel over its chat, its sidebar, the preview, the selection, the
composer, toasts, the dialog, where its transcript is scrolled, whether
it has focus, and which session it is bound to — is a field of the
model, owned here. The web client holds nothing of its own: it hydrates
from this on connect (the ``window_model`` RPC), binds its views to the
copy it keeps, and writes back through the same RPC; an extension writes
through ``host.ui``. Both reach :meth:`Windows.write` (or
:meth:`Windows.toast`, :meth:`Windows.open_dialog`, …), so a change is
the same change whoever made it: stored, pushed to every window as
``window_model_changed``, and told to the host model, which signals it.

**Order is the backend's.** Writes are applied in the order they reach
this process — one event loop, one sequence — and each push carries the
window's revision and the writer's token. A window shows its own write
at once and keeps showing it until its echo arrives; whatever arrives
last from here is what every window shows. ``clientState.ts`` documents
the client's half.

**What survives what.** The page stack, the sidebar, the bound session
and folder, and each session's draft and composer mode are written to
the :class:`~igni_core.core.session.client_state.ClientStateStore`, so
they survive igni restarting. The rest — panel, preview, selection,
toasts, dialog, scroll, focus — lives as long as this backend does,
and survives a window reloading: a dialog an extension is waiting on is
still there when the window comes back.

**The active window** is the one the person last did something in.
``host.ui`` addresses it. The *focused* window is the one that says it
has focus; when one does, it is also the active one.

**The model's parts.** The windows themselves are
:class:`~.window_registry.WindowRegistry`; where a window's values are
kept is :class:`~.window_state.WindowState`; what each field of the
model holds is :class:`~.window_fields.WindowFields`; the dialogs on
screen are :class:`~.window_dialogs.DialogBook`; and who is told of a
change is :class:`~.window_changes.ChangeNotifier`.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from igni_core.core.session.client_state import ClientStateStoreProtocol
from igni_core.extensions.host.types import SchemaTypes
from igni_core.extensions.host.window_changes import (
    ChangeListener,
    ChangeNotifier,
    Push,
    WindowChange,
    WindowListener,
)
from igni_core.extensions.host.window_dialogs import DialogBook
from igni_core.extensions.host.window_errors import WindowModelError
from igni_core.extensions.host.window_field import WindowFieldName
from igni_core.extensions.host.window_fields import WindowFields
from igni_core.extensions.host.window_registry import WindowRegistry
from igni_core.extensions.host.window_state import UiKeys, WindowState
from igni_core.extensions.host.window_values import (
    MAX_TOASTS,
    Dialog,
    Page,
    Toast,
    WindowModelState,
)


class Windows:
    """Every window this backend has seen, and the model of each.

    A facade over named collaborators: the windows themselves
    (:attr:`registry`), where each one's values are kept, what each
    field of the model holds, the dialogs on screen, and who is told of
    a change.
    """

    def __init__(self, store: ClientStateStoreProtocol, *, types: SchemaTypes, push: Push) -> None:
        self._changes = ChangeNotifier(push)
        self._state = WindowState(store)
        self._fields = WindowFields(types, self._state.lasting)
        self._dialogs = DialogBook()
        self._registry = WindowRegistry(self._changes, self._state.lasting, self._state.transient)

    # ── Listeners ─────────────────────────────────────────────────

    def on_change(self, listener: ChangeListener) -> None:
        """A listener told every change to any window's model."""
        self._changes.on_change(listener)

    def on_window(self, listener: WindowListener) -> None:
        """``listener("opened" | "closed" | "focus_changed", window)``."""
        self._changes.on_window(listener)

    @property
    def registry(self) -> WindowRegistry:
        """The windows themselves: which are open, which is active."""
        return self._registry

    # ── Reading ───────────────────────────────────────────────────

    def rev(self, window_id: str) -> int:
        """The window's revision: how many changes it has had."""
        return self._changes.rev(window_id)

    def session(self, window_id: str) -> str | None:
        """The session this window shows, if it shows one."""
        return self._state.lasting.read(window_id).get(UiKeys.SESSION) or None

    def get(self, window_id: str, name: str) -> Any:
        """One field of a window's model, as the model holds it."""
        return self._fields.handler(name).read(self._state.context(window_id))

    def snapshot(self, window_id: str) -> WindowModelState:
        """Every field of a window's model — what a window hydrates from."""
        context = self._state.context(window_id)
        values = {handler.name: handler.read(context) for handler in self._fields.handlers()}
        return WindowModelState.model_validate(values)

    def unset(self, window_id: str) -> list[str]:
        """Lasting fields this window has never written: the window
        applies its own defaults to them (a wide window opens with its
        sidebar out, a narrow one without)."""
        stored = self._state.lasting.read(window_id)
        return [] if UiKeys.SIDEBAR in stored else ["sidebar"]

    # ── Writing ───────────────────────────────────────────────────

    def write(self, window_id: str, name: str, value: Any, *, token: str = "") -> WindowChange:
        """Set one field of a window's model — the view's write and an
        extension's alike. Validated against the field's schema type;
        a write of the value already there is not a change."""
        handler = self._fields.handler(name)
        if not handler.writable:
            raise WindowModelError(f"{name!r} changes through its own operations, not a write")
        checked = handler.check(value)
        context = self._state.context(window_id)
        if (
            not handler.always_changes
            and handler.read(context) == checked
            and name not in self.unset(window_id)
        ):
            # A field never stored still keeps what is written, even
            # when it equals the default: the window applies its own
            # default to a field that is unset, and would apply it
            # again on its next load over what the person chose.
            return WindowChange(
                window=window_id, field=name, value=checked, rev=self.rev(window_id)
            )
        handler.keep(context, checked)
        if name == WindowFieldName.FOCUSED:
            self._registry.focus_window(window_id, bool(checked))
        else:
            self._registry.touch(window_id)
        change = self._changes.announce(window_id, name, handler.read(context), token=token)
        if name == WindowFieldName.SESSION:
            # The draft is the session's: binding another session shows
            # another draft.
            self._changes.announce(
                window_id,
                WindowFieldName.COMPOSER,
                self.get(window_id, WindowFieldName.COMPOSER),
                token=token,
            )
        return change

    def toast(self, window_id: str, toast: Toast, *, token: str = "") -> Toast:
        """Show a toast. Its id is the host's, so two writers cannot collide."""
        raw = {**toast.model_dump(), "id": f"t-{uuid.uuid4().hex[:12]}"}
        if raw.get("target") is not None:
            raw["target"] = Page.of(raw["target"]).model_dump()
        checked = self._fields.check("toast", "Toast", raw)
        shown = Toast(
            id=checked["id"],
            text=checked["text"],
            kind=checked.get("kind") or "info",
            body=checked.get("body"),
            target=checked.get("target"),
            source=checked.get("source"),
        )
        context = self._state.context(window_id)
        while len(context.transient.toasts) >= MAX_TOASTS:
            # The oldest goes, and says so: a toast nobody saw leave is
            # one an extension would wait on.
            self.dismiss_toast(window_id, context.transient.toasts[0].id, token=token)
        context.transient.toasts = [*context.transient.toasts, shown]
        self._changes.announce(
            window_id,
            WindowFieldName.TOASTS,
            list(context.transient.toasts),
            token=token,
            event="toast_shown",
            item=shown,
        )
        return shown

    def dismiss_toast(self, window_id: str, toast_id: str, *, token: str = "") -> Toast | None:
        """Take a toast off the screen: the one that went, if it was there."""
        context = self._state.context(window_id)
        gone = next((t for t in context.transient.toasts if t.id == toast_id), None)
        if gone is None:
            return None
        context.transient.toasts = [t for t in context.transient.toasts if t.id != toast_id]
        self._changes.announce(
            window_id,
            WindowFieldName.TOASTS,
            list(context.transient.toasts),
            token=token,
            event="toast_dismissed",
            item=gone,
        )
        return gone

    def toast_of(self, window_id: str, toast_id: str) -> Toast | None:
        """The toast on screen with this id, if it is there."""
        context = self._state.context(window_id)
        return next((t for t in context.transient.toasts if t.id == toast_id), None)

    def open_dialog(
        self, window_id: str, spec: Any, *, source: str | None = None, token: str = ""
    ) -> tuple[Dialog, asyncio.Future[Any]]:
        """Show a dialog; the future is the person's answer.

        One dialog at a time: the one already open closes unanswered
        (``None``), as if dismissed. Deterministic, and nobody waits on
        a dialog that can no longer be answered.
        """
        context = self._state.context(window_id)
        open_dialog = context.transient.dialog
        if open_dialog is not None:
            self.answer_dialog(window_id, open_dialog.id, None, token=token)
        dialog, answer = self._dialogs.open(window_id, spec, source=source)
        context.transient.dialog = dialog
        self._registry.touch(window_id)
        self._changes.announce(
            window_id,
            WindowFieldName.DIALOG,
            dialog,
            token=token,
            event="dialog_opened",
            item=dialog,
        )
        return dialog, answer

    def answer_dialog(
        self, window_id: str, dialog_id: str, answer: Any, *, token: str = ""
    ) -> bool:
        """The person answered (or dismissed, ``None``). ``False`` if that
        dialog is not the one open — answered already, or replaced."""
        context = self._state.context(window_id)
        closed = self._dialogs.close(window_id, dialog_id, answer)
        if closed is None:
            return False
        context.transient.dialog = None
        self._changes.announce(
            window_id,
            WindowFieldName.DIALOG,
            None,
            token=token,
            event="dialog_closed",
            item=closed,
            answer=answer,
        )
        return True

    def cancel_dialogs(self) -> None:
        """Shutting down: nobody will answer."""
        self._dialogs.cancel()


__all__ = ["Windows"]
