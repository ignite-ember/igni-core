"""Message sanitizer — converts multimodal content arrays to plain
text for non-vision models.

Extracted from the free ``_sanitize_messages`` function in the old
``models.py`` so the behavior lives on a real collaborator that the
``LoggingModel`` holds by composition instead of a state-mutating
free function. Same fix pattern as the audit's Rule 1 remediation
(free functions taking a state object as first arg → methods on a
class).
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel


class MessageSanitizer:
    """Rewrites multimodal message content into plain text.

    When a non-vision model receives messages from a session that
    previously used a vision model, ``content`` may be a list of
    dicts (text + image_url + file). Extracts only the ``text``
    parts, joined with newlines. Vision-capable models see the
    original content untouched.

    Agno calls ``invoke`` / ``ainvoke`` / ``invoke_stream`` /
    ``ainvoke_stream`` with keyword arguments only (``self.invoke(**kwargs)``),
    so :meth:`sanitize_call` reads ``messages`` from the keywords and falls back
    to the first positional argument. The messages handed in are the run's own
    history, which Agno stores with the session: they are never changed. A
    message that needs flattening is copied, so a later vision model still
    finds its images.
    """

    def sanitize_call(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        vision: bool,
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """``(args, kwargs)`` with the messages a non-vision model may read.

        A vision model gets both back as they are. Otherwise ``messages`` is
        taken from the keywords when it is there, or from ``args[0]``.
        """
        if vision:
            return args, kwargs
        if "messages" in kwargs:
            return args, {**kwargs, "messages": self._sanitize_messages(kwargs["messages"])}
        return self.sanitize_if_needed(args, vision=vision), kwargs

    def sanitize_if_needed(
        self,
        args: tuple[Any, ...],
        *,
        vision: bool,
    ) -> tuple[Any, ...]:
        """``args`` with ``args[0]``, the messages, made readable without vision.

        No-op fast paths:
        * ``vision=True`` — pass through so image content survives.
        * empty ``args`` — nothing to sanitize.
        """
        if vision or not args:
            return args
        sanitized = self._sanitize_messages(args[0])
        return (sanitized, *args[1:])

    def _sanitize_messages(self, messages: list[Any]) -> list[Any]:
        """A new list in which each message whose content is a list of parts is
        replaced by a copy holding their text; the others are the same objects.
        """
        return [
            self._with_content(msg, self._flatten_to_text(content))
            if isinstance(content := self._read_content(msg), list)
            else msg
            for msg in messages
        ]

    @staticmethod
    def _read_content(msg: Any) -> Any:
        if isinstance(msg, dict):
            return msg.get("content")
        return getattr(msg, "content", None)

    @staticmethod
    def _with_content(msg: Any, new_content: str) -> Any:
        if isinstance(msg, dict):
            return {**msg, "content": new_content}
        if isinstance(msg, BaseModel):
            return msg.model_copy(update={"content": new_content})
        duplicate = copy.copy(msg)
        duplicate.content = new_content
        return duplicate

    @staticmethod
    def _flatten_to_text(content: list[Any]) -> str:
        text_parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
            elif isinstance(part, str):
                text_parts.append(part)
        return "\n".join(text_parts) if text_parts else ""
