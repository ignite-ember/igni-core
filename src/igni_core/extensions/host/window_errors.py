"""The two failures a window's model can have.

They are told apart on purpose: a refusal is the writer's fault and the
answer says so; a state failure is the backend's, and the window is told
so it does not keep showing a state that was never saved.
"""

from __future__ import annotations


class WindowModelError(ValueError):
    """A write the model refuses: an unknown field, or a value that does
    not fit the field's schema type."""


class WindowStateError(RuntimeError):
    """The state a window keeps across reloads could not be read or
    written — the store behind it failed.

    Not a refusal: the write was allowed and it went wrong. The window
    is told, so it does not keep showing a state that was never saved.
    """


__all__ = ["WindowModelError", "WindowStateError"]
