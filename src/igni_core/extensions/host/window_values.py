"""What a window's model holds: the values behind each of its fields.

These are the host schema's own types (:mod:`.definition`), written out
as models — a window's page stack is a list of :class:`Page`, its panel
is a :class:`Panel`, its draft a :class:`Composer`. The model keeps
models; only the wire turns them back into plain data.

Every field says what it holds, because these models are what a client
is handed: the whole model comes back from ``window_model``, and a
change to one of its fields arrives on a push. A field whose shape the
window does not fix — a panel's own parameters, a dialog's contents —
is any JSON value, said as such.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from igni_core.extensions.host.window_errors import WindowModelError
from igni_core.protocol.json_value import JSON_VALUE_SCHEMA, WireJson

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

    kind: str = Field(
        description="What kind of page this is: `plugins`, `extensions`, or an extension's own kind."
    )
    label: str = Field(default="", description="The name shown for this page.")
    params: dict[str, str] = Field(
        default_factory=dict,
        description="The page's parameters, as text: what the page was opened with.",
    )

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

    model_config = ConfigDict(
        extra="allow", json_schema_extra={"additionalProperties": JSON_VALUE_SCHEMA}
    )

    kind: str = Field(
        description="What kind of panel this is: `notifications`, `search`, `login` or `dir-picker`."
    )
    params: dict[str, WireJson] = Field(
        default_factory=dict,
        description="The panel's own parameters: what the panel shows, in the panel's own shape.",
    )


class Preview(BaseModel):
    """The file a window's preview shows."""

    model_config = ConfigDict(
        extra="allow", json_schema_extra={"additionalProperties": JSON_VALUE_SCHEMA}
    )

    path: str = Field(description="The path of the file being previewed.")


class Selection(BaseModel):
    """What is selected in a window. Each part is independent; ``None`` for nothing."""

    model_config = ConfigDict(
        extra="allow", json_schema_extra={"additionalProperties": JSON_VALUE_SCHEMA}
    )

    message_id: str | None = Field(
        default=None,
        description="The message the selection starts in; null when nothing is selected.",
    )
    text: str | None = Field(default=None, description="The selected text; null when none is.")
    file: str | None = Field(
        default=None, description="The file the selection is in; null when none is."
    )


class Sidebar(BaseModel):
    """A window's sidebar: open or not, its width and its section."""

    open: bool = Field(default=False, description="Whether the sidebar is open.")
    width: int | None = Field(
        default=None, description="The sidebar's width in pixels; null for igni's own sidebar."
    )
    section: str | None = Field(
        default=None, description="The section the sidebar shows; null for none."
    )

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

    text: str = Field(default="", description="The draft's text, as the person wrote it.")
    attachments: list[str] = Field(
        default_factory=list, description="The files the draft names, in order."
    )
    mode: str = Field(
        default="chat",
        description="What the composer is in: `chat`, `command` (a slash command) or `shell`.",
    )

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

    model_config = ConfigDict(
        extra="allow", json_schema_extra={"additionalProperties": JSON_VALUE_SCHEMA}
    )

    message_id: str | None = Field(
        default=None,
        description="The message the transcript is scrolled to; null when it is at the end.",
    )
    at_bottom: bool = Field(
        default=True, description="Whether the transcript is scrolled to its end."
    )

    @classmethod
    def to(cls, target: str) -> Scroll:
        """One message, or ``bottom``."""
        if target == "bottom":
            return cls(message_id=None, at_bottom=True)
        return cls(message_id=target, at_bottom=False)


class Toast(BaseModel):
    """A transient message on screen."""

    model_config = ConfigDict(
        extra="allow", json_schema_extra={"additionalProperties": JSON_VALUE_SCHEMA}
    )

    #: What kind of thing this is — a toast, told from a dialog.
    type: Literal["toast"] = Field(default="toast", description="Always `toast`: what this is.")
    #: The host's id — two writers cannot collide.
    id: str = Field(default="", description="The toast's own id: two writers cannot collide.")
    text: str = Field(description="The toast's first line.")
    kind: str = Field(description="How it is styled: `info`, `success`, `warning` or `error`.")
    body: str | None = Field(default=None, description="A second line; null when there is none.")
    target: Page | None = Field(
        default=None, description="Where clicking the toast goes; null when it goes nowhere."
    )
    #: The extension that showed it; ``None`` for igni's own.
    source: str | None = Field(
        default=None, description="The extension that showed it; null for igni's own."
    )


class Dialog(BaseModel):
    """A dialog on screen: igni's own or an extension's. One at a time."""

    #: What kind of thing this is — a dialog, told from a toast.
    type: Literal["dialog"] = Field(default="dialog", description="Always `dialog`: what this is.")
    id: str = Field(description="The dialog's own id: two writers cannot collide.")
    #: What the dialog shows: igni's own spec, or an extension's.
    spec: WireJson | None = Field(
        default=None, description="What the dialog shows: igni's own spec, or an extension's."
    )
    #: The extension that asked; ``None`` for igni's own.
    source: str | None = Field(
        default=None, description="The extension that asked for it; null for igni's own."
    )


class WindowInfo(BaseModel):
    """One igni window, as the schema's ``WindowInfo`` names it."""

    id: str = Field(description="The window's own id: it survives a reload.")
    kind: str = Field(default="web", description="What kind of window this is: `tauri` or `web`.")
    connected: bool = Field(default=True, description="Whether the window is connected now.")
    focused: bool = Field(default=False, description="Whether the person is using this window.")
    session_id: str | None = Field(default=None, description="The session it shows; null for none.")
    project_dir: str | None = Field(default=None, description="The folder it shows; null for none.")


class Transient(BaseModel):
    """The fields of one window that live in this process only."""

    panel: Panel | None = Field(default=None, description="The panel open over the chat.")
    preview: Preview | None = Field(default=None, description="The file being previewed.")
    selection: Selection = Field(default_factory=Selection, description="What is selected.")
    toasts: list[Toast] = Field(default_factory=list, description="The toasts on screen.")
    dialog: Dialog | None = Field(default=None, description="The dialog on screen.")
    scroll: Scroll = Field(default_factory=Scroll, description="Where the transcript is scrolled.")
    focused: bool = Field(default=False, description="Whether this window has the focus.")


class WindowModelState(BaseModel):
    """A window's whole model: every field, as a window hydrates from it."""

    session: str = Field(default="", description="The session the window shows.")
    project_dir: str = Field(default="", description="The folder the window shows.")
    route: list[Page] = Field(default_factory=list, description="The window's page stack.")
    panel: Panel | None = Field(default=None, description="The panel open over the chat.")
    sidebar: Sidebar = Field(default_factory=Sidebar, description="The window's sidebar.")
    preview: Preview | None = Field(default=None, description="The file being previewed.")
    selection: Selection = Field(default_factory=Selection, description="What is selected.")
    composer: Composer = Field(default_factory=Composer, description="The window's composer.")
    toasts: list[Toast] = Field(default_factory=list, description="The toasts on screen.")
    dialog: Dialog | None = Field(default=None, description="The dialog on screen.")
    scroll: Scroll = Field(default_factory=Scroll, description="Where the transcript is scrolled.")
    focused: bool = Field(default=False, description="Whether this window has the focus.")


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
