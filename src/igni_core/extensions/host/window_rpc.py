"""``window_model``: a window reading or writing its own model.

The same :class:`~.windows.Windows` an extension's ``host.ui`` writes,
so a window's change and an extension's are one change. One method per
:attr:`WindowRequest.op`, each with its own typed reply.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, Field

from igni_core.extensions.host.windows import WindowRequest, Windows
from igni_core.protocol.json_value import WireJson


class Hydrated(BaseModel):
    """The whole model, its revision, and the fields nobody set."""

    op: Literal["hydrate"] = "hydrate"
    state: dict[str, WireJson]
    rev: int
    unset: list[str]


class Written(BaseModel):
    """A field written: the window's revision after it, and whether the
    value changed — a write of the value already there changes nothing,
    and pushes nothing."""

    op: Literal["write"] = "write"
    rev: int
    changed: bool = True


class ToastAdded(BaseModel):
    """The toast as shown, with its id, and the window's revision."""

    op: Literal["toast"] = "toast"
    toast: dict[str, WireJson]
    rev: int


class Revised(BaseModel):
    """A toast dismissed: the window's new revision."""

    op: Literal["dismiss_toast"] = "dismiss_toast"

    rev: int


class DialogShown(BaseModel):
    """The dialog as shown, with its id, and the window's revision."""

    op: Literal["open_dialog"] = "open_dialog"
    dialog: dict[str, WireJson]
    rev: int


class DialogAnswered(BaseModel):
    """Whether the dialog answered was still the open one, and the window's revision."""

    op: Literal["answer_dialog"] = "answer_dialog"
    #: ``False`` when that dialog is not the one open any more.
    answered: bool
    rev: int


#: One ``window_model`` answer, told apart by the ``op`` it answers.
WindowReply: TypeAlias = Annotated[
    Hydrated | Written | ToastAdded | Revised | DialogShown | DialogAnswered,
    Field(discriminator="op"),
]


class WindowModelRpc:
    """Answers each ``window_model`` op through one table."""

    def __init__(self, windows: Windows) -> None:
        self._windows = windows
        self._ops: dict[str, Callable[[WindowRequest], WindowReply]] = {
            "hydrate": self._hydrate,
            "write": self._write,
            "toast": self._toast,
            "dismiss_toast": self._dismiss_toast,
            "open_dialog": self._open_dialog,
            "answer_dialog": self._answer_dialog,
        }

    def ops(self) -> frozenset[str]:
        """Every op answered, for the coverage test."""
        return frozenset(self._ops)

    def handle(self, request: WindowRequest) -> WindowReply:
        """Answer one request. Raises
        :class:`~igni_core.extensions.host.windows.WindowModelError`
        for a write the model refuses."""
        if request.op == "hydrate" or not self._windows.known(request.window):
            # A write from a window that has not hydrated in this process
            # is a reconnect racing its own hydrate: the window is open.
            self._windows.attached(
                request.window, connection_id=request.connection_id, kind=request.kind
            )
        return self._ops[request.op](request)

    def _hydrate(self, request: WindowRequest) -> WindowReply:
        window = request.window
        return Hydrated(
            state=self._windows.snapshot(window),
            rev=self._windows.rev(window),
            unset=self._windows.unset(window),
        )

    def _write(self, request: WindowRequest) -> WindowReply:
        before = self._windows.rev(request.window)
        change = self._windows.write(
            request.window, request.field, request.value, token=request.token
        )
        return Written(rev=change.rev, changed=change.rev != before)

    def _toast(self, request: WindowRequest) -> WindowReply:
        window = request.window
        toast = self._windows.toast(window, dict(request.value or {}), token=request.token)
        return ToastAdded(toast=toast, rev=self._windows.rev(window))

    def _dismiss_toast(self, request: WindowRequest) -> WindowReply:
        window = request.window
        self._windows.dismiss_toast(window, str(request.value or ""), token=request.token)
        return Revised(rev=self._windows.rev(window))

    def _open_dialog(self, request: WindowRequest) -> WindowReply:
        window = request.window
        dialog, _answer = self._windows.open_dialog(window, request.value, token=request.token)
        return DialogShown(dialog=dialog, rev=self._windows.rev(window))

    def _answer_dialog(self, request: WindowRequest) -> WindowReply:
        window = request.window
        answered = self._windows.answer_dialog(
            window, request.dialog_id, request.value, token=request.token
        )
        return DialogAnswered(answered=answered, rev=self._windows.rev(window))


__all__ = [
    "DialogAnswered",
    "DialogShown",
    "Hydrated",
    "Revised",
    "ToastAdded",
    "WindowModelRpc",
    "WindowReply",
    "Written",
]
