"""What a window's model holds: the values behind each of its fields.

These are the host schema's own types (:mod:`.definition`), written out
as models — a window's page stack is a list of :class:`Page`, its panel
is a :class:`Panel`, its draft a :class:`Composer`. The model keeps
models; only the wire turns them back into plain data.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from igni_core.extensions.host.window_errors import WindowModelError

#: The composer's modes.
MODES = ("chat", "command", "shell")

#: How many toasts a window holds; the oldest goes first.
MAX_TOASTS = 6

#: An ``@path`` reference in a draft — what ``attachments`` lists. The
#: composer's inline code pills (``@code:<id>``) are not files.
_REFERENCE = re.compile(r"(?:^|(?<=\s))@(?!code:)(\S+)")


def references(text: str) -> list[str]:
    """The ``@path`` references in a draft, in order, without repeats."""
    seen: list[str] = []
    for match in _REFERENCE.finditer(text):
        path = match.group(1)
        if path not in seen:
            seen.append(path)
    return seen


def text_params(params: Any) -> dict[str, str]:
    """A page's parameters as the model keeps them: keys and values as text."""
    if not isinstance(params, dict):
        return {}
    return {str(key): str(value) for key, value in params.items()}


class Page(BaseModel):
    """One level of a window's page stack."""

    kind: str
    label: str = ""
    params: dict[str, str] = Field(default_factory=dict)

    @field_validator("params", mode="before")
    @classmethod
    def _params(cls, value: Any) -> Any:
        """A page without parameters has none, not ``None``."""
        return {} if value is None else value

    @classmethod
    def of(cls, page: dict[str, Any]) -> Page:
        """A page as the model keeps it: labelled with its own kind when
        it has no label, its parameters as text."""
        kind = page.get("kind")
        if not isinstance(kind, str):
            raise WindowModelError(f"a page has no kind: {page!r}")
        return cls(
            kind=kind,
            label=str(page.get("label") or kind),
            params=text_params(page.get("params")),
        )

    @classmethod
    def named(cls, page: str, params: Any) -> Page:
        """A page labelled with its own kind, as ``ui.navigate`` opens one."""
        return cls(kind=page, label=page, params=text_params(params))


class Panel(BaseModel):
    """The panel open over a window's chat. One at a time."""

    model_config = ConfigDict(extra="allow")

    kind: str
    params: dict[str, Any] = Field(default_factory=dict)


class Preview(BaseModel):
    """The file a window's preview shows."""

    model_config = ConfigDict(extra="allow")

    path: str


class Selection(BaseModel):
    """What is selected in a window. Each part is independent; ``None`` for nothing."""

    model_config = ConfigDict(extra="allow")

    message_id: str | None = None
    text: str | None = None
    file: str | None = None


class Sidebar(BaseModel):
    """A window's sidebar: open or not, its width and its section."""

    open: bool = False
    width: int | None = None
    section: str | None = None

    @classmethod
    def merged(
        cls, current: Sidebar, *, open: bool, width: int | None, section: str | None
    ) -> Sidebar:
        """The sidebar opened or closed, keeping what is not named."""
        return cls(
            open=open,
            width=current.width if width is None else width,
            section=current.section if section is None else section,
        )


class Composer(BaseModel):
    """A window's composer: its draft, the files it names and its mode."""

    text: str = ""
    attachments: list[str] = Field(default_factory=list)
    mode: str = "chat"

    @field_validator("attachments", mode="before")
    @classmethod
    def _attachments(cls, value: Any) -> Any:
        """A draft with no attachments has none, not ``None``."""
        return [] if value is None else value

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


class Scroll(BaseModel):
    """Where a window's transcript is scrolled to."""

    model_config = ConfigDict(extra="allow")

    message_id: str | None = None
    at_bottom: bool = True

    @classmethod
    def to(cls, target: str) -> Scroll:
        """One message, or ``bottom``."""
        if target == "bottom":
            return cls(message_id=None, at_bottom=True)
        return cls(message_id=target, at_bottom=False)


class Toast(BaseModel):
    """A transient message on screen."""

    model_config = ConfigDict(extra="allow")

    #: The host's id — two writers cannot collide.
    id: str = ""
    text: str
    kind: str
    body: str | None = None
    target: Page | None = None
    #: The extension that showed it; ``None`` for igni's own.
    source: str | None = None


class Dialog(BaseModel):
    """A dialog on screen: igni's own or an extension's. One at a time."""

    id: str
    #: What the dialog shows: igni's own spec, or an extension's.
    spec: Any = None
    #: The extension that asked; ``None`` for igni's own.
    source: str | None = None


class WindowInfo(BaseModel):
    """One igni window, as the schema's ``WindowInfo`` names it."""

    id: str
    kind: str = "web"
    connected: bool = True
    focused: bool = False
    session_id: str | None = None
    project_dir: str | None = None


class Transient(BaseModel):
    """The fields of one window that live in this process only."""

    panel: Panel | None = None
    preview: Preview | None = None
    selection: Selection = Field(default_factory=Selection)
    toasts: list[Toast] = Field(default_factory=list)
    dialog: Dialog | None = None
    scroll: Scroll = Field(default_factory=Scroll)
    focused: bool = False


class WindowModelState(BaseModel):
    """A window's whole model: every field, as a window hydrates from it."""

    session: str = ""
    project_dir: str = ""
    route: list[Page] = Field(default_factory=list)
    panel: Panel | None = None
    sidebar: Sidebar = Field(default_factory=Sidebar)
    preview: Preview | None = None
    selection: Selection = Field(default_factory=Selection)
    composer: Composer = Field(default_factory=Composer)
    toasts: list[Toast] = Field(default_factory=list)
    dialog: Dialog | None = None
    scroll: Scroll = Field(default_factory=Scroll)
    focused: bool = False


__all__ = [
    "MAX_TOASTS",
    "MODES",
    "Composer",
    "Dialog",
    "Page",
    "Panel",
    "Preview",
    "Scroll",
    "Selection",
    "Sidebar",
    "Toast",
    "Transient",
    "WindowInfo",
    "WindowModelState",
    "references",
    "text_params",
]
