"""Values back to JSON-plain data, at the seams where they leave the host.

A member may answer with a model, a signal value may be one, and a
validated parameter is one. Each is flattened here, once, before it is
sent: a pure leaf with no subject of its own.
"""

from __future__ import annotations

from typing import Any


def plain(value: Any) -> Any:
    """``value`` with every model in it dumped to plain JSON data."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    return value


__all__ = ["plain"]
