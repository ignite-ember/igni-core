"""One live call through ``provider: anthropic``, to Anthropic's own API.

``test_anthropic_provider.py`` covers the provider against a stub; this
checks the stub has not drifted from the real thing: a turn with a tool
call, streamed, through the builder igni uses. Skips unless
``ANTHROPIC_API_KEY`` is set, since every run costs money — and unless
Anthropic's own API accepts that key, since the key may belong to an
Anthropic-compatible gateway instead, which the provider never talks to.
"""

from __future__ import annotations

import os

import httpx
import pytest
from agno.agent import Agent

from igni_core.core.agents.schemas import AgentConstructorArgs
from igni_core.core.config.llm_call_logger import LlmCallLogger
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.provider_builders import AnthropicBuilder


def _anthropic_key() -> str | None:
    """The key for Anthropic's own API, or ``None`` when there is none.

    ``ANTHROPIC_API_KEY`` is also the variable Claude Code exports, and the
    key it exports is for its own gateway rather than for
    ``api.anthropic.com``: Anthropic refuses it with a 401. A live call
    against such a key would fail for a reason that has nothing to do
    with the code under test, and it would spend the run to find that
    out. Anthropic's own keys all start with ``sk-ant-``.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return key if key.startswith("sk-ant-") else None


pytestmark = [
    pytest.mark.skipif(
        not _anthropic_key(),
        reason=(
            "no key for Anthropic's own API (export ANTHROPIC_API_KEY, an sk-ant- "
            "key, to run the live Anthropic test)"
        ),
    ),
    pytest.mark.reaches_the_internet,
]

#: Where the provider always sends its requests — its own API, whatever
#: the machine's environment names.
ANTHROPIC_API = "https://api.anthropic.com/v1/models"


@pytest.fixture
def _anthropic_accepts_the_key() -> None:
    """Skip unless Anthropic's own API accepts the key in the environment.

    ``ANTHROPIC_API_KEY`` may name a key of an Anthropic-compatible
    gateway rather than of Anthropic. The provider always talks to
    Anthropic's own API, and such a key is refused there, so the live
    call would fail on the credentials rather than on the provider.
    Listing the models costs nothing and answers a key Anthropic holds
    with a list.
    """
    response = httpx.get(
        ANTHROPIC_API,
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
        timeout=10.0,
    )
    if response.status_code != 200:
        pytest.skip(
            "Anthropic's own API does not accept this key "
            f"(HTTP {response.status_code}), so the live call cannot be made"
        )


async def test_a_streamed_tool_call_against_anthropic(
    _anthropic_accepts_the_key: None,
) -> None:
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
