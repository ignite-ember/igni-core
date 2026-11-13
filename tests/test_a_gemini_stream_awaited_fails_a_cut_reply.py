"""``CheckedGemini``'s async stream raises on a reply that did not finish,
as its sync stream does (see test_every_provider_fails_a_cut_reply.py)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from agno.models.google import Gemini
from google.genai import types as genai

from igni_core.core.config.checked_gemini import CheckedGemini
from igni_core.core.config.reply_ending import ModelReplyCut


def _chunk(reason: Any) -> genai.GenerateContentResponse:
    return genai.GenerateContentResponse(
        candidates=[
            genai.Candidate(
                content=genai.Content(role="model", parts=[genai.Part(text="hi")]),
                finish_reason=reason,
            )
        ]
    )


async def _drain(events: list[Any]) -> list[Any]:
    model = CheckedGemini(id="gemini-x", api_key="k", max_output_tokens=512)

    async def ainvoke_stream(self, *args: Any, **kwargs: Any):
        for event in events:
            yield self._parse_provider_response_delta(event)

    with (
        patch.object(Gemini, "ainvoke_stream", ainvoke_stream),
        patch.object(Gemini, "_parse_provider_response_delta", lambda self, e, *a, **k: e),
    ):
        return [chunk async for chunk in model.ainvoke_stream([], MagicMock())]


async def test_a_reply_cut_at_max_tokens_raises() -> None:
    with pytest.raises(ModelReplyCut, match=r"length limit \(512 tokens\)"):
        await _drain([_chunk(None), _chunk(genai.FinishReason.MAX_TOKENS)])


async def test_a_stream_with_no_finish_reason_raises() -> None:
    with pytest.raises(ModelReplyCut, match="cut off"):
        await _drain([_chunk(None)])


async def test_a_finished_reply_comes_through_unchanged() -> None:
    chunks = [_chunk(None), _chunk(genai.FinishReason.STOP)]
    assert await _drain(chunks) == chunks
