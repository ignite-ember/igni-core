"""Why a terminal could not be opened."""

from __future__ import annotations


class TerminalUnavailable(Exception):
    """This machine cannot run a terminal: no PTY support (``pywinpty`` on Windows)."""


class TerminalLimitReached(Exception):
    """The connection already has as many terminals open as it may."""


__all__ = ["TerminalLimitReached", "TerminalUnavailable"]
