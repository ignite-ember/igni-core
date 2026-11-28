"""Raised by a cell that broke exactly the invariants a known finding breaks."""

from __future__ import annotations


class KnownFindingError(AssertionError):
    """The cell fails only as its known finding says; ``xfail(raises=...)`` expects this."""
