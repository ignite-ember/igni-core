"""Split inline ``<think>`` blocks out of a stream's visible content.

Some providers emit literal ``<think>…</think>`` tags inside their visible
content stream. A block routinely spans many events, so the parser
(:func:`igni_core.core.text_tags.split_inline_think_tags`) threads state
across them; this class keeps that state per run, for one stream.

Event handlers stay stateless so one Agno event can be tested without a
session; carry state is per stream and per run, so it lives here and the
multiplexer owns one.
"""

from __future__ import annotations

from igni_core.core.text_tags import (
    INITIAL_STATE,
    ThinkParseState,
    split_inline_think_tags,
)
from igni_core.protocol import messages as msg

#: Key for content whose event carried no run id.
_UNKNOWN_RUN = "_unknown_"


class InlineThinkSplitter:
    """Per-run parser state for inline ``<think>`` tags in one stream."""

    def __init__(self) -> None:
        self._states: dict[str, ThinkParseState] = {}

    def split(self, message: msg.Message, run_id: str | None) -> list[msg.Message]:
        """``message`` as the content deltas its inline tags split it into.

        Anything but visible content passes through: native reasoning
        already arrives as ``is_thinking`` with its wrapper stripped, and
        reparsing tag-free reasoning would reclassify it as visible.

        The original instance is returned wherever a segment is exactly
        it, so the chat's identity-based memoisation holds on the happy
        path. Text is compared, not just the role: a ``</think>`` split
        across events (``…</thi`` then ``nk>…``) leaves one segment with
        the original role but the ``nk>`` peeled off, and the original
        would leak it.
        """
        if not isinstance(message, msg.ContentDelta) or message.is_thinking:
            return [message]
        key = message.id or run_id or _UNKNOWN_RUN
        segments, self._states[key] = split_inline_think_tags(
            self._states.get(key, INITIAL_STATE), message.text
        )
        if segments == [(message.text, message.is_thinking)]:
            return [message]
        out: list[msg.Message] = []
        for text, is_thinking in segments:
            if not text:
                continue
            if is_thinking == message.is_thinking and text == message.text:
                out.append(message)
                continue
            out.append(
                msg.ContentDelta(
                    text=text,
                    is_thinking=is_thinking,
                    id=message.id,
                    session_id=message.session_id,
                    event_seq=0,  # transport stamps on broadcast
                )
            )
        return out

    def end_run(self, run_id: str) -> None:
        """Drop ``run_id``'s state, so a stray partial tag or unclosed
        block from a cancelled run cannot leak into the next one."""
        self._states.pop(run_id, None)
