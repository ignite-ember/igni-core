"""What members write into a window's model, and the links and dialogs
an extension may ask a window to show.

Each payload dumps to exactly the keys the window's model takes for its
field (:mod:`igni_core.extensions.host.windows`), ``None`` keys included.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.windows import references


class ExternalLink(BaseModel):
    """A link an extension asks igni to open in the person's browser.

    Absolute ``https`` only, with a host and no credentials: nothing
    that runs on this machine (``file:``, a custom scheme), nothing
    readable in transit, and nothing that hides who it signs in as.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def _https(cls, url: str) -> str:
        if any(ch.isspace() or ord(ch) < 0x20 for ch in url):
            raise ValueError("a link has no whitespace or control characters")
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise ValueError(f"only https links open outside igni, not {parts.scheme or url!r}")
        if not parts.hostname:
            raise ValueError("a link names its host")
        if parts.username is not None or parts.password is not None:
            raise ValueError("a link carries no credentials")
        return url


class PanelKind(StrEnum):
    """The panels a window has, by kind."""

    NOTIFICATIONS = "notifications"
    SEARCH = "search"
    LOGIN = "login"
    DIR_PICKER = "dir-picker"

    @classmethod
    def of(cls, kind: str) -> Self:
        if kind not in {k.value for k in cls}:
            raise HostModelError(
                ErrorCode.INVALID,
                f"no panel {kind!r}: one of {', '.join(sorted(k.value for k in cls))}",
            )
        return cls(kind)


class ToastKind(StrEnum):
    """The kinds of toast, in the order a refusal names them."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"

    @classmethod
    def of(cls, kind: str) -> Self:
        if kind not in {k.value for k in cls}:
            raise HostModelError(
                ErrorCode.INVALID, f"toast kind {kind!r}: one of {', '.join(k.value for k in cls)}"
            )
        return cls(kind)


class Page(BaseModel):
    """One page of a window's route."""

    kind: str
    label: str
    params: dict[str, Any]

    @classmethod
    def named(cls, page: str, params: dict[str, Any] | None) -> Page:
        """A page labelled with its own kind, as ``ui.navigate`` opens one."""
        return cls(kind=page, label=page, params=params or {})


class Panel(BaseModel):
    """The panel a window shows."""

    kind: str
    params: dict[str, Any]


class PreviewTarget(BaseModel):
    """The file a window's preview shows."""

    path: str


class Sidebar(BaseModel):
    """A window's sidebar: open or not, its width and its section."""

    model_config = ConfigDict(extra="ignore")

    open: bool
    width: int | None = None
    section: str | None = None

    @classmethod
    def merged(
        cls, current: dict[str, Any], *, open: bool, width: int | None, section: str | None
    ) -> Sidebar:
        """The sidebar opened or closed, keeping what is not named."""
        now = cls.model_validate(current)
        return cls(
            open=open,
            width=now.width if width is None else width,
            section=now.section if section is None else section,
        )


class Composer(BaseModel):
    """A window's draft and its mode."""

    text: str
    mode: str

    @classmethod
    def with_attachments(
        cls, text: str, attachments: list[str], mode: str | None, current_mode: str | None
    ) -> Composer:
        """``text``, with an ``@path`` reference added for each attachment
        it does not mention yet; the mode named, else the one there."""
        for path in attachments:
            if path not in references(text):
                sep = "" if not text or text.endswith((" ", "\n")) else " "
                text = f"{text}{sep}@{path} "
        return cls(text=text, mode=mode or current_mode or "chat")


class ToastRequest(BaseModel):
    """A toast an extension shows. The window gives it its id."""

    text: str
    kind: str
    body: str | None
    #: A ``Page``, as the schema validated it; the window normalises it.
    target: Any
    source: str


class ScrollPosition(BaseModel):
    """Where a window's transcript is scrolled to."""

    message_id: str | None
    at_bottom: bool

    @classmethod
    def to(cls, target: str) -> ScrollPosition:
        """One message, or ``bottom``."""
        if target == "bottom":
            return cls(message_id=None, at_bottom=True)
        return cls(message_id=target, at_bottom=False)


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
    "Composer",
    "DialogButton",
    "DialogInput",
    "DialogSpec",
    "ExtensionDialog",
    "ExternalLink",
    "Page",
    "Panel",
    "PanelKind",
    "PreviewTarget",
    "ScrollPosition",
    "Sidebar",
    "ToastKind",
    "ToastRequest",
]
