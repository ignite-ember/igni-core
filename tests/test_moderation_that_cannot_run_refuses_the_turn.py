"""guardrails.moderation with no OpenAI key refuses the turn, not lets it through.

Agno's pre-hook runner logs any exception other than ``InputCheckError``
and runs the turn: with moderation on and no ``OPENAI_API_KEY`` every turn
logged a traceback and reached the model unmoderated (campaign R086,
R157).
"""

from __future__ import annotations

import pytest
from agno.exceptions import InputCheckError
from agno.run.agent import RunInput

from igni_core.core.config.schemas import GuardrailsConfig
from igni_core.core.config.settings import Settings
from igni_core.core.session.agent_factory import create_guardrails


def _moderation():
    (hook,) = create_guardrails(
        Settings(guardrails=GuardrailsConfig(pii_detection=False, moderation=True))
    )
    return hook


def test_no_key_refuses_the_turn_sync(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(InputCheckError, match="could not be moderated"):
        _moderation().check(RunInput(input_content="hello"))


@pytest.mark.asyncio
async def test_no_key_refuses_the_turn_async(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(InputCheckError, match="OPENAI_API_KEY"):
        await _moderation().async_check(RunInput(input_content="hello"))


@pytest.mark.asyncio
async def test_a_flagged_message_still_raises_the_moderation_error(monkeypatch):
    from agno.guardrails.openai import OpenAIModerationGuardrail

    flagged = InputCheckError("flagged")

    async def boom(self, run_input):
        raise flagged

    monkeypatch.setattr(OpenAIModerationGuardrail, "async_check", boom)
    with pytest.raises(InputCheckError) as info:
        await _moderation().async_check(RunInput(input_content="x"))
    assert info.value is flagged
