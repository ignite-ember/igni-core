"""Fuzz the decoder every stream transport runs on each frame it reads.

``MessageRegistry.deserialize`` takes one line from the WebSocket or the
NDJSON stream — text from whichever client is on the other end — and
promises to return a message or ``None``: a bad frame is skipped, it
never wedges the read loop. So any exception out of it is a crash here.
A frame it accepts has to be one of the registered wire types. Pure:
JSON and pydantic in memory, no socket.
"""

from __future__ import annotations

import logging
import sys

import atheris

with atheris.instrument_imports(include=["igni_core"]):
    from igni_core.protocol.messages import Message
    from igni_core.protocol.registry import MessageRegistry

logging.disable(logging.CRITICAL)

_REGISTRY = MessageRegistry()
_KNOWN = frozenset(_REGISTRY.known_types())


def _text(data: bytes) -> str | None:
    """The input as the UTF-8 text a transport or settings file would hold.

    Decoded directly rather than through ``FuzzedDataProvider`` so the seed
    corpus is read as written: the provider spends the first byte choosing
    an encoding, which turns every seed into something else.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def TestOneInput(data: bytes) -> None:
    line = _text(data)
    if line is None:
        return

    message = _REGISTRY.deserialize(line)

    if message is not None:
        assert isinstance(message, Message)
        assert message.type in _KNOWN, message.type


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
