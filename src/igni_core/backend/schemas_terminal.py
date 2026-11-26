"""The interactive terminal's calls: their arguments and answers.

A window opens a shell on igni's machine, in the project folder, types
into it, resizes it and closes it. What the shell prints comes back on
``terminal_output``; its end on ``terminal_exited``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from igni_core.backend.rpc_binding import RpcArgs


class TerminalOpenArgs(RpcArgs):
    """Arguments for opening a terminal."""

    cols: int = Field(ge=1, le=1000, description="How many columns wide the terminal is.")
    rows: int = Field(ge=1, le=1000, description="How many rows tall the terminal is.")


class TerminalInputArgs(RpcArgs):
    """Arguments for typing into a terminal."""

    terminal_id: str = Field(description="The terminal to type into, as `terminal_open` named it.")
    data: str = Field(
        description="What to type, exactly as the keyboard sends it: text, `\\r` for Enter, "
        "`\\u0003` for Ctrl-C, escape sequences for the arrow keys."
    )


class TerminalResizeArgs(RpcArgs):
    """Arguments for resizing a terminal."""

    terminal_id: str = Field(description="The terminal to resize.")
    cols: int = Field(ge=1, le=1000, description="Its new width, in columns.")
    rows: int = Field(ge=1, le=1000, description="Its new height, in rows.")


class TerminalCloseArgs(RpcArgs):
    """Arguments for closing a terminal."""

    terminal_id: str = Field(description="The terminal to close.")


class TerminalOpened(BaseModel):
    """A terminal that was opened: its id, its shell and where it started."""

    terminal_id: str = Field(
        description="The terminal's id, which its `terminal_output` and `terminal_exited` "
        "pushes carry and every later call names."
    )
    shell: str = Field(description="The shell it runs, as a path or a command name.")
    cwd: str = Field(description="The folder it started in: the project folder.")


class TerminalAck(BaseModel):
    """The call was done to the terminal it names."""

    terminal_id: str = Field(description="The terminal the call was done to.")


__all__ = [
    "TerminalAck",
    "TerminalCloseArgs",
    "TerminalInputArgs",
    "TerminalOpenArgs",
    "TerminalOpened",
    "TerminalResizeArgs",
]
