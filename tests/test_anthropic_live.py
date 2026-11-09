"""One live call through ``provider: anthropic``, to Anthropic's own API.

``test_anthropic_provider.py`` covers the provider against a stub; this
checks the stub has not drifted from the real thing: a turn with a tool
call, streamed, through the builder igni uses. Skips unless the machine
holds a key Anthropic's own API takes, since every run costs money.
"""

from __future__ import annotations

import os

import pytest
from agno.agent import Agent

from igni_core.core.agents.schemas import AgentConstructorArgs
from igni_core.core.config.llm_call_logger import LlmCallLogger
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.provider_builders import AnthropicBuilder

#: Where Anthropic's own API lives.
ANTHROPIC_API = "https://api.anthropic.com"


def _a_key_for_anthropic() -> bool:
    """Whether this machine holds a key Anthropic's own API would take.

    ``ANTHROPIC_BASE_URL`` names the endpoint the machine's key belongs to.
    igni always calls Anthropic's API — it never reads that variable, so a
    request cannot be sent anywhere else — which means a machine pointed at
    another provider's gateway holds a key that API will refuse, and this
    test would fail on the credential rather than on igni. A machine with
    no base URL set holds a key for Anthropic, or none at all.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    base = os.environ.get("ANTHROPIC_BASE_URL", ANTHROPIC_API).rstrip("/")
    return base.startswith(ANTHROPIC_API)

pytestmark = [
    pytest.mark.skipif(
        not _a_key_for_anthropic(),
        reason=(
            "no key for Anthropic's own API (export ANTHROPIC_API_KEY, and leave "
            "ANTHROPIC_BASE_URL pointing at Anthropic, to run the live test)"
        ),
    ),
    pytest.mark.reaches_the_internet,
]


async def test_a_streamed_tool_call_against_anthropic() -> None:
    calls: list[tuple[int, int]] = []

    def add_numbers(a: int, b: int) -> str:
        """Add two integers and return the sum."""
        calls.append((a, b))
        return str(a + b)

    entry = ModelRegistryEntry(
        provider="anthropic",
        model_id="claude-opus-5-5",
        api_key_env="ANTHROPIC_API_KEY",
        max_tokens=4096,
        effort="low",
    )
    model = AnthropicBuilder().build(entry, cloud_token=None, llm_logger=LlmCallLogger())
    args = AgentConstructorArgs(
        name="live-claude",
        model=model,
        description="",
        instructions=["Use the add_numbers tool for arithmetic, then state the result."],
        tools=[add_numbers],
        num_history_runs=1,
        retries=0,
    )
    text = ""
    async for event in Agent(**args.to_agno_kwargs()).arun("What is 1234 + 4321?", stream=True):
        if getattr(event, "event", "") == "RunContent" and isinstance(event.content, str):
            text += event.content

    assert calls == [(1234, 4321)]
    assert "5555" in text.replace(",", "")
