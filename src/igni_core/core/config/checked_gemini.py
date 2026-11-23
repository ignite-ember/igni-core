"""Gemini, with a reply that did not finish raised as an error.

Agno's :class:`~agno.models.google.Gemini` reads a candidate's
``finish_reason`` only to retry a malformed function call. A reply cut at
``MAX_TOKENS``, stopped by ``SAFETY`` or ``RECITATION``, or a stream that
ended with no reason at all reached igni as a finished answer, as it did
for OpenAI-compatible servers before ``reply_ending.py``. This class
applies the same rule (:class:`ReplyEnding` with the :data:`GEMINI`
words).

It also offers Gemini every tool: :class:`GeminiToolSchemas` gives each
array in a tool's schema the ``items`` Gemini requires and other
providers do not (see :mod:`igni_core.core.config.gemini_schemas`).

Importing this module imports the ``google-genai`` SDK, so only
:class:`~igni_core.core.config.provider_builders.GeminiBuilder` imports
it, and only inside a try: an install without the SDK still starts.
"""

from __future__ import annotations

from agno.models.google import Gemini

from igni_core.core.config.gemini_schemas import GeminiToolSchemas
from igni_core.core.config.reply_ending import GEMINI, ReplyCutRetries, ReplyEnding
from igni_core.core.config.tool_names import ToolNameBoundary


class CheckedGemini(ToolNameBoundary, GeminiToolSchemas, ReplyCutRetries, Gemini):
    """Agno's ``Gemini``, failing a reply that did not finish and sent arrays it accepts."""

    def invoke_stream(self, *args, **kwargs):
        ending = self._reply_ending()
        with ending.watching():
            yield from super().invoke_stream(*args, **kwargs)
        ending.check()

    async def ainvoke_stream(self, *args, **kwargs):
        ending = self._reply_ending()
        with ending.watching():
            async for chunk in super().ainvoke_stream(*args, **kwargs):
                yield chunk
        ending.check()

    def _parse_provider_response_delta(self, response_delta, **kwargs):
        ReplyEnding.note_chunk(response_delta)
        return super()._parse_provider_response_delta(response_delta, **kwargs)

    def _parse_provider_response(self, response, **kwargs):
        ReplyEnding.check_completion(response, self.id, self.max_output_tokens, GEMINI)
        return super()._parse_provider_response(response, **kwargs)

    def _reply_ending(self) -> ReplyEnding:
        return ReplyEnding(self.id, self.max_output_tokens, GEMINI)
