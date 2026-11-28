"""Raised when igni closes the connection while a scripted turn streams."""

from __future__ import annotations


class StreamClosedError(Exception):
    """igni closed the connection while the turn was streaming."""
