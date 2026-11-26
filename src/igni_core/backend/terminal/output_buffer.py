"""A terminal's output between the PTY and the push that carries it."""

from __future__ import annotations

import codecs


class TerminalOutputBuffer:
    """Decodes a terminal's bytes as UTF-8 and holds the text until it is sent.

    The decoder is incremental, so a character split across two reads is
    held back until its last byte arrives; bytes that are not UTF-8 become
    U+FFFD. :meth:`drain` hands the text out in chunks of at most
    ``max_chunk`` characters, one push each.
    """

    MAX_CHUNK = 64 * 1024

    def __init__(self, max_chunk: int = MAX_CHUNK) -> None:
        if max_chunk < 1:
            raise ValueError("max_chunk must be at least 1")
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._max_chunk = max_chunk
        self._parts: list[str] = []
        self._size = 0

    @property
    def size(self) -> int:
        """How many characters wait to be sent."""
        return self._size

    def feed(self, data: bytes) -> None:
        """Add bytes read from the PTY."""
        self._add(self._decoder.decode(data))

    def finish(self) -> None:
        """No more bytes come: a partial character left over becomes U+FFFD."""
        self._add(self._decoder.decode(b"", final=True))

    def drain(self) -> list[str]:
        """Everything decoded so far, in chunks of at most ``max_chunk`` characters."""
        text = "".join(self._parts)
        self._parts.clear()
        self._size = 0
        step = self._max_chunk
        return [text[i : i + step] for i in range(0, len(text), step)]

    def _add(self, text: str) -> None:
        if text:
            self._parts.append(text)
            self._size += len(text)
