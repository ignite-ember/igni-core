"""Typed view models for the ``/model`` slash command's chat output.

Extracted from :mod:`igni_core.backend.command_handler` — the
old ``_cmd_model`` inline body assembled ``"Switched to model: X"``
and the "unknown model" error string procedurally. Both now flow
through :class:`ModelSwitchResult`, the typed return of the new
public :meth:`Session.set_default_model` method.
"""

from __future__ import annotations

from pydantic import BaseModel

from igni_core.backend.command_result import CommandResult
from igni_core.protocol.messages import CommandAction, CommandResultKind


class ModelSwitchResult(BaseModel):
    """Pattern-3 result envelope returned by
    :meth:`Session.set_default_model`.

    Callers stop try/except-ing on unknown model names — they
    branch on :attr:`ok` and let :meth:`to_command_result` produce
    the right :class:`CommandResult` (info + ``model_switched``
    action on success, error on failure).
    """

    ok: bool
    model_name: str
    available: list[str] = []
    error: str | None = None

    @property
    def failure_text(self) -> str:
        """The user-facing refusal for an ``ok=False`` result."""
        avail = ", ".join(sorted(self.available))
        reason = self.error or f"Unknown model: '{self.model_name}'."
        return f"{reason} Available: {avail}"

    def to_command_result(self) -> CommandResult:
        if not self.ok:
            return CommandResult.error(self.failure_text)
        # ``action="model_switched"`` tells the FE to refresh the
        # status-bar model slot. Without it the bar showed the OLD
        # model after ``/model <name>`` direct switches — nothing
        # else triggers a refresh on that code path. (Live cross-
        # cutting constraint — preserved verbatim from the pre-
        # refactor implementation.)
        return CommandResult(
            kind=CommandResultKind.INFO,
            content=f"Switched to model: {self.model_name}",
            action=CommandAction.MODEL_SWITCHED,
        )


__all__ = ["ModelSwitchResult"]
