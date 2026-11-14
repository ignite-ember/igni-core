"""Response text extraction.

Normalizes an Agno response object (``RunOutput``, ``RunResponse``,
plain ``str``, or any object with a ``.content`` / ``.messages``
attribute) into the assistant-visible text. Falls back to
``str(response)`` when no text field is present.

The public surface is intentionally two-tier:

* :class:`ResponseTextExtractor` — the OOP entry point. Wraps the
  response and exposes :meth:`text`. Prefer this in new code.
* :func:`extract_response_text` — a one-line delegator kept for
  back-compat. It is ALSO the live monkeypatch surface used by
  ``patch("igni_core.core.session.core.extract_response_text", ...)``
  in the test suite; do not inline or rename it (see
  ``session/core.py`` for the ``# noqa: F401 — test-patch target``
  companion import).
"""

from __future__ import annotations

from typing import Any, Protocol

# Sentinel distinguishing "attribute missing" from "attribute is None".
# Load-bearing for the ``.content = None`` case, which must still
# dispatch through the content branch (returning ``"None"``) rather
# than falling through to ``.messages`` / ``str(response)``.
_MISSING: Any = object()


class AgnoResponseLike(Protocol):
    """Structural type for Agno-style response objects.

    Names the two attributes :class:`ResponseTextExtractor` probes.
    Both attributes are declared ``Any`` because Agno's real types
    (``RunOutput.content: str | BaseModel | ...``, ``messages: list[Message]``)
    are polymorphic — the Protocol advertises attribute *presence*,
    not shape.
    """

    content: Any
    messages: Any


class ResponseTextExtractor:
    """Extracts the assistant-visible text from an Agno response.

    Dispatch order (pinned by ``tests/test_response.py``):

    1. ``str`` input → return verbatim.
    2. ``.content`` present → stringify (even if ``None``).
    3. ``.messages`` present → walk in reverse and return
       ``str(msg.content)`` for the last message whose ``content``
       is truthy.
    4. Fallback → ``str(response)``.
    """

    def __init__(self, response: str | AgnoResponseLike) -> None:
        self._response = response

    def text(self) -> str:
        response = self._response
        if isinstance(response, str):
            return response

        content = getattr(response, "content", _MISSING)
        if content is not _MISSING:
            return str(content)

        messages = getattr(response, "messages", _MISSING)
        if messages is not _MISSING:
            extracted = self._from_messages(messages)
            if extracted is not None:
                return extracted

        return str(response)

    @staticmethod
    def _from_messages(messages: Any) -> str | None:
        if messages is None:
            return None
        for msg in reversed(messages):
            candidate = getattr(msg, "content", None)
            if candidate:
                return str(candidate)
        return None


def extract_response_text(response: str | AgnoResponseLike) -> str:
    """Return the assistant-visible text for an Agno response.

    Falls back to ``str(response)`` when no text field is present.
    Thin delegator over :class:`ResponseTextExtractor` — kept as a
    module-level symbol because the test suite patches this name
    directly via ``patch("igni_core.core.session.core.extract_response_text")``.
    """
    return ResponseTextExtractor(response).text()


def extract_reply_text(response: str | AgnoResponseLike) -> str:
    """The reply a person reads: :func:`extract_response_text`, with the run's
    separate assistant texts kept apart.

    Agno builds a run's ``content`` by appending each assistant message of
    its tool loop to the last with nothing between them, so a model that
    says "OK", calls a tool and then answers was printed as
    ``OKOK — noted``, and "I'll look up ticket T-1001." ran into its answer.
    When ``content`` is exactly those texts end to end, they are joined
    with a blank line instead.

    A run that paused at a tool call and was continued (the permission
    policy settling the pause, ``message_pause_policy``) starts its
    ``content`` again from the continuation, so it holds only the texts
    after the pause and the text before the call was dropped:
    "Checking the notes now." never printed. ``content`` that is the
    last of those texts, or the last few end to end, is read the same
    way: every text of the run is shown. Any other content is left as it is. Only what is
    shown changes: the history the model reads is the messages.
    """
    text = extract_response_text(response)
    if isinstance(response, str):
        return text
    parts = _assistant_texts(getattr(response, "messages", None))
    if len(parts) > 1 and any("".join(parts[start:]) == text for start in range(len(parts))):
        return "\n\n".join(part.strip() for part in parts)
    return text


def _assistant_texts(messages: Any) -> list[str]:
    """This run's assistant texts, in order (history and empty ones left out)."""
    if not messages:
        return []
    texts: list[str] = []
    for msg in messages:
        if getattr(msg, "role", None) != "assistant" or getattr(msg, "from_history", False):
            continue
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content:
            texts.append(content)
    return texts
