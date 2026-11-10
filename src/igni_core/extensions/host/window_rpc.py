"""``window_model``: a window reading or writing its own model.

The same :class:`~.windows.Windows` an extension's ``host.ui`` writes,
so a window's change and an extension's are one change. One method per
:attr:`WindowRequest.op`, each with its own typed reply.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from igni_core.extensions.host.window_values import Dialog, Toast, WindowModelState
from igni_core.extensions.host.windows import Windows


class WindowRequest(BaseModel):
    """One ``window_model`` request: a window reading or writing its model.

    ``hydrate`` answers the whole model and says the window is open;
    ``write`` sets one field; ``toast`` and ``dismiss_toast`` add and
    remove one toast; ``open_dialog`` shows igni's own dialog and
    ``answer_dialog`` closes the open one with the person's answer
    (``value``; ``None`` for dismissed). ``token`` is the window's own
    name for the write, echoed on the push.
    """

    model_config = ConfigDict(extra="forbid")

    op: Literal["hydrate", "write", "toast", "dismiss_toast", "open_dialog", "answer_dialog"] = (
        Field(description="What the window asks for.")
    )
    window: str = Field(min_length=1, description="The window's own stable id.")
    field: str = Field(default="", description="The field to write, for ``write``.")
    value: Any = Field(default=None, description="The value to write, or the answer to a dialog.")
    token: str = Field(
        default="",
        description="The window's own name for this write, echoed on the push it gets back.",
    )
    dialog_id: str = Field(default="", description="The dialog to answer, for ``answer_dialog``.")
    connection_id: str = Field(
        default="",
        description=(
            "The connection the window came in on, for transports without connections; ignored "
            "over a WebSocket, where the request's own connection is the window's."
        ),
    )
    kind: str = Field(
        default="", description="What kind of window this is, recorded when it opens."
    )


class Hydrated(BaseModel):
    """The answer to ``hydrate``: the window's whole model, its revision, and the fields nobody has set."""

    op: Literal["hydrate"] = Field(
        default="hydrate", description="Always `hydrate`: the request this answers."
    )
    state: WindowModelState = Field(
        description="Every field of the window's model, as the model holds it."
    )
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
    toast: Toast = Field(description="The toast as shown, its `id` included.")
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
    dialog: Dialog = Field(description="The dialog as shown, its `id` included.")
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
        :class:`~igni_core.extensions.host.window_errors.WindowModelError`
        for a write the model refuses."""
        if request.op == "hydrate" or not self._windows.registry.known(request.window):
            # A write from a window that has not hydrated in this process
            # is a reconnect racing its own hydrate: the window is open.
            self._windows.registry.attached(
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
        toast = Toast.model_validate(request.value or {})
        shown = self._windows.toast(window, toast, token=request.token)
        return ToastAdded(toast=shown, rev=self._windows.rev(window))

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
    "WindowRequest",
    "Written",
]
