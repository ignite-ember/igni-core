"""How an RPC handler's return value goes out on the wire."""

from __future__ import annotations

from typing import Any


class RpcResult:
    """The JSON-safe shape of an RPC handler's return value.

    Every route that answers an RPC — a window's call through the
    dispatcher, an extension's through the host backend — puts the
    handler's value into :class:`RPCResponse.result` through
    :meth:`serialize`, so both answer with the same shape.
    """

    @classmethod
    def serialize(cls, value: Any) -> Any:
        """Convert ``value`` into a JSON-safe shape.

        Order of branches matters:

        1. ``None`` → ``None`` (distinct from empty string / empty dict).
        2. Scalars (``str``, ``int``, ``float``, ``bool``) — passed as-is.
           ``bool`` MUST branch here before any ``isinstance(_, int)``
           fallback because ``isinstance(True, int)`` is ``True`` in
           Python and we don't want ``True`` coerced to ``1``.
        3. Sequences / mappings — recurse element-wise.
        4. Pydantic models — ``model_dump``.
        5. Anything else — ``str()`` as a last resort so an exotic type
           doesn't crash the JSON encoder downstream.
        """
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (list, tuple)):
            return [cls.serialize(v) for v in value]
        if isinstance(value, dict):
            return {str(k): cls.serialize(v) for k, v in value.items()}
        if hasattr(value, "model_dump"):
            return value.model_dump()
        return str(value)
