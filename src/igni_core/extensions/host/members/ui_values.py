"""What a member names when it writes into a window's model: the kinds a
window may show, and the dialogs an extension may ask a window to show.

The values themselves — a page, a panel, a draft — are the model's own
(:mod:`igni_core.extensions.host.window_values`); this is the vocabulary
around them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.choice import Choice
from igni_core.extensions.host.spec import ErrorCode


class PanelKind(Choice):
    """The panels a window has, by kind."""

    NOTIFICATIONS = "notifications"
    SEARCH = "search"
    LOGIN = "login"
    DIR_PICKER = "dir-picker"

    @classmethod
    def refusal(cls, value: str) -> str:
        return f"no panel {value!r}: one of {', '.join(sorted(k.value for k in cls))}"


class ToastKind(Choice):
    """The kinds of toast, in the order a refusal names them."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"

    @classmethod
    def refusal(cls, value: str) -> str:
        return f"toast kind {value!r}: one of {', '.join(k.value for k in cls)}"


class DialogButton(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    primary: bool = False


class DialogInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = ""
    placeholder: str = ""
    value: str = ""


class DialogSpec(BaseModel):
    """What an extension may show in a dialog. Text only: igni draws it."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    body: str = ""
    buttons: list[DialogButton] = Field(
        default_factory=lambda: [DialogButton(id="ok", label="OK", primary=True)],
        min_length=1,
        max_length=4,
    )
    input: DialogInput | None = None

    @classmethod
    def of(cls, value: Any) -> ExtensionDialog:
        """An extension's spec, checked, as the dialog a window opens."""
        try:
            spec = cls.model_validate(value)
        except ValidationError as exc:
            raise HostModelError(ErrorCode.INVALID, f"ui.show_dialog(spec): {exc}") from None
        return ExtensionDialog.model_validate(spec.model_dump())


class _ExtensionKind(BaseModel):
    kind: Literal["extension"] = "extension"


class ExtensionDialog(DialogSpec, _ExtensionKind):
    """A dialog an extension opened. ``kind`` comes first: pydantic orders
    fields from the last base to the first."""


__all__ = [
    "DialogButton",
    "DialogInput",
    "DialogSpec",
    "ExtensionDialog",
    "PanelKind",
    "ToastKind",
]
