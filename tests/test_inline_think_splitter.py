"""Regression tests for :class:`InlineThinkSplitter`.

The parser itself (``core.text_tags``) is unit-tested separately. These
tests pin the splitter's wiring — specifically the "pass the original
message through unchanged" fast paths, which must NOT fire when the
parser stripped a split-tag remainder from the text.

The bug these guard against: a ``</think>`` that splits across two
content events (``…astronomy.\\n</thi`` then ``nk>\\n\\nStars…``) had its
``nk>`` remainder leak into the visible bubble, because the fast path
compared only the segment *role* to the original, not its *text*, and
re-emitted the original (untrimmed) message.
"""

from __future__ import annotations

from igni_core.backend.inline_think_splitter import InlineThinkSplitter
from igni_core.protocol.schemas.be_events import ContentDelta


def _drive(splitter: InlineThinkSplitter, run_id: str, text: str, is_thinking: bool):
    return splitter.split(ContentDelta(text=text, is_thinking=is_thinking), run_id)


def test_split_close_tag_does_not_leak_remainder():
    """``</think>`` split as ``</thi`` + ``nk>`` must not leak ``nk>``
    into the visible bubble."""
    splitter = InlineThinkSplitter()
    run = "run-stars"

    # Open + reasoning in one event.
    _drive(splitter, run, "<think>\nStars have colors because of temperature.", False)
    # Reasoning continues, then the close tag begins but splits.
    e2 = _drive(splitter, run, " Blackbody physics.\n</thi", False)
    # The rest of the close tag + the visible answer.
    e3 = _drive(splitter, run, "nk>\n\nStars appear different colors...", False)

    all_out = e2 + e3
    for m in all_out:
        assert "nk>" not in m.text, f"leaked tag remainder: {m.text!r}"
        assert "</thi" not in m.text
        assert "think>" not in m.text

    # The visible answer must be present and tagged non-thinking.
    visible = "".join(m.text for m in all_out if not m.is_thinking)
    assert "Stars appear different colors" in visible
    # The reasoning must be tagged thinking and not contain the answer.
    thinking = "".join(m.text for m in all_out if m.is_thinking)
    assert "Blackbody physics" in thinking
    assert "Stars appear different colors" not in thinking


def test_unchanged_visible_text_passes_through_identity():
    """When nothing is stripped, the fast path still returns the exact
    same message instance (identity memoisation on the FE)."""
    msg = ContentDelta(text="just a plain answer", is_thinking=False)
    out = InlineThinkSplitter().split(msg, "run-x")
    assert len(out) == 1
    assert out[0] is msg


def test_reasoning_delta_passes_through_untouched():
    """Native reasoning (is_thinking=True) bypasses the parser entirely."""
    msg = ContentDelta(text="some reasoning", is_thinking=True)
    out = InlineThinkSplitter().split(msg, "run-y")
    assert len(out) == 1
    assert out[0] is msg


def test_inline_block_in_single_event_splits_to_two_bubbles():
    """A complete ``<think>…</think>`` block followed by the answer in
    one event yields a thinking segment and a clean visible segment."""
    out = _drive(
        InlineThinkSplitter(),
        "run-z",
        "<think>reasoning here</think>\n\nThe answer is 42.",
        False,
    )
    thinking = "".join(m.text for m in out if m.is_thinking)
    visible = "".join(m.text for m in out if not m.is_thinking)
    assert "reasoning here" in thinking
    assert "The answer is 42" in visible
    for m in out:
        assert "think>" not in m.text


def test_an_ended_run_leaves_no_open_block_behind():
    """A block a run left open does not swallow the next run's answer."""
    splitter = InlineThinkSplitter()
    _drive(splitter, "run-a", "<think>cut off mid-thought", False)

    splitter.end_run("run-a")

    (out,) = _drive(splitter, "run-a", "a visible answer", False)
    assert (out.text, out.is_thinking) == ("a visible answer", False)
