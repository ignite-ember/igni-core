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
    """The answer to ``hydrate``: the window's whole model, its revision, and the fields nobody has set."""

    op: Literal["hydrate"] = Field(
        default="hydrate", description="Always `hydrate`: the request this answers."
    )
    state: dict[str, WireJson] = Field(description="Every field of the window's model, by name.")
    rev: int = Field(description="The window's revision.")
    unset: list[str] = Field(description="The fields still at their defaults, never set.")


class Written(BaseModel):
    """The answer to ``write``: the window's revision after it, and whether the value changed."""

    op: Literal["write"] = Field(
        default="write", description="Always `write`: the request this answers."
    )
    rev: int = Field(description="The window's revision after the write.")
    changed: bool = Field(
        default=True,
        description="False when the field already held the value: nothing changed and no change was sent.",
    )


class ToastAdded(BaseModel):
    """The answer to ``toast``: the toast as shown, with its id, and the window's revision."""

    op: Literal["toast"] = Field(
        default="toast", description="Always `toast`: the request this answers."
    )
    toast: dict[str, WireJson] = Field(description="The toast as shown, its `id` included.")
    rev: int = Field(description="The window's revision after showing it.")


class Revised(BaseModel):
    """The answer to ``dismiss_toast``: the window's new revision."""

    op: Literal["dismiss_toast"] = Field(
        default="dismiss_toast", description="Always `dismiss_toast`: the request this answers."
    )

    rev: int = Field(description="The window's revision after the toast was dismissed.")


class DialogShown(BaseModel):
    """The answer to ``open_dialog``: the dialog as shown, with its id, and the window's revision."""

    op: Literal["open_dialog"] = Field(
        default="open_dialog", description="Always `open_dialog`: the request this answers."
    )
    dialog: dict[str, WireJson] = Field(description="The dialog as shown, its `id` included.")
    rev: int = Field(description="The window's revision after opening it.")


class DialogAnswered(BaseModel):
    """The answer to ``answer_dialog``: whether the dialog was still the open one, and the window's revision."""

    op: Literal["answer_dialog"] = Field(
        default="answer_dialog", description="Always `answer_dialog`: the request this answers."
    )
    answered: bool = Field(
        description="False when that dialog was no longer the open one, so the answer was not taken."
    )
    rev: int = Field(description="The window's revision after the answer.")


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
