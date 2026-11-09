"""Message-type registry — owns the ``type`` string → ``Message`` class map.

The registry reads the wire contract — the explicit list of message
classes in :mod:`igni_core.protocol.schemas.wire` — and answers two
questions for the transports: which ``type`` strings can arrive, and
what does a given frame become. Both answers come from the same list,
so the contract the frontend is shown and the contract the wire accepts
cannot drift apart.

It is used by every stream-framed transport
(:class:`~igni_core.transport.ndjson_stream.NDJsonStreamTransport` and
:class:`~igni_core.transport.websocket.WebSocketServerTransport`). Each
transport is constructor-injectable, so a caller that wants a registry
of its own hands one in; a caller that does not care lets the transport
build one.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from pydantic import TypeAdapter, ValidationError

from igni_core.protocol.messages import Message
from igni_core.protocol.schemas.wire import (
    WIRE_MESSAGE_TYPES,
    wire_type_of,
    wire_union,
)

logger = logging.getLogger(__name__)


class MessageRegistry:
    """Lookup table from wire ``type`` string to :class:`Message` subclass.

    The wire contract is explicit: :data:`WIRE_MESSAGE_TYPES` names every
    message class that can arrive, and a frame is validated against the
    ``type``-discriminated union built from that list. Nothing is
    discovered by scanning a module namespace, so a message that reaches
    the schemas but not the list is a visible omission rather than a
    silent one.

    A registry is a value, not a singleton: build one, hand it to a
    transport, throw it away. Two registries built from the same
    contract accept the same frames.
    """

    def __init__(self, message_types: Sequence[type[Message]] = WIRE_MESSAGE_TYPES) -> None:
        types = tuple(message_types)
        self._by_type: dict[str, type[Message]] = {wire_type_of(cls): cls for cls in types}
        self._adapter: TypeAdapter[Any] = TypeAdapter(wire_union(types))

    def known_types(self) -> list[str]:
        """Every wire ``type`` string this registry will accept.

        The transports' answer to "what can arrive?", which is the
        thing the generated wire schema has to cover — a message the
        transport accepts but the schema omits is one the frontend can
        receive and no test can check. Asserted in
        ``tests/test_wire_schema_is_current.py``.
        """
        return sorted(self._by_type)

    def deserialize(self, line: str) -> Message | None:
        """Deserialize a JSON line into a :class:`Message`.

        Returns ``None`` (with a warning log) on any of:

        * malformed JSON
        * a frame that is not a JSON object
        * a ``type`` that is missing, is not a string, or is not in the
          registry
        * a payload that fails validation against the resolved class

        The transports treat ``None`` as "skip this frame" so a bad
        message never wedges the read loop.
        """
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to deserialize message: %s", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("Frame is not a JSON object: %s", type(data).__name__)
            return None
        msg_type = data.get("type")
        if not isinstance(msg_type, str):
            logger.warning("Frame carries no string 'type' field")
            return None
        if msg_type not in self._by_type:
            logger.warning("Unknown message type: %s", msg_type)
            return None
        try:
            return self._adapter.validate_python(data)
        except ValidationError as exc:
            logger.warning("Failed to deserialize message: %s", exc)
            return None
