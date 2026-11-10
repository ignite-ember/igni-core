"""Wire schemas for the background-process watcher RPCs.

Companion module to :mod:`server_processes` — follows the sibling
``schemas_*.py`` convention (see :mod:`schemas_run`,
:mod:`schemas_hitl`, :mod:`schemas_panels`, etc.) so every RPC
response served by :class:`~igni_core.backend.server_processes.ProcessesController`
is a typed Pydantic model rather than a raw dict.

Contains:

* :class:`ProcessRow` — one row of the running-process list.
* :class:`ProcessTailResult` — typed shape for
  :meth:`~igni_core.backend.server_processes.ProcessesController.read_tail`.
* :class:`StopProcessResult` — typed shape for
  :meth:`~igni_core.backend.server_processes.ProcessesController.stop`.

The controller returns these models directly; the
:class:`~igni_core.backend.server.BackendServer` facade methods
``.model_dump()`` at the wire seam so the JSON-RPC layer keeps
seeing the same dict shapes it always has.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProcessRow(BaseModel):
    """A background process the agent started, running or recently
    finished."""

    # ``is_running`` / ``exit_code`` default to "running" so an older
    # client that predates them — and the test fixtures that build rows
    # by hand — read a listed row the way they always did.
    pid: int = Field(description="Operating-system process id.")
    cmd: str = Field(description="The command line that started the process.")
    elapsed_seconds: float = Field(description="How long the process has run (or ran), in seconds.")
    is_running: bool = Field(default=True, description="`true` while the process is still running.")
    exit_code: int | None = Field(
        default=None,
        description="The process's exit code once it has finished; `null` while running.",
    )


class ProcessTailResult(BaseModel):
    """The latest output of a background process."""

    pid: int = Field(description="Operating-system process id that was asked for.")
    output: str = Field(
        description="The last lines of the process's combined stdout and stderr; empty for an unknown pid."
    )
    is_running: bool = Field(
        description="`true` while the process is still running; `false` for an unknown pid."
    )
    exit_code: int | None = Field(
        description="The exit code once the process has finished; `null` while running or if unknown."
    )


class StopProcessResult(BaseModel):
    """Result of stopping a background process."""

    pid: int = Field(description="Operating-system process id that was asked for.")
    killed: bool = Field(
        description="`true` when the process was stopped; `false` if it was unknown or had already exited."
    )
    message: str = Field(description="Human-readable explanation of what happened.")
